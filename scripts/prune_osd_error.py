"""Delete the previous day's anomalies from output/osd_error.txt.

The monitor appends every anomaly with a Beijing timestamp, so "yesterday" is
decided from the timestamp itself (not from the machine timezone).

Rules:
  * keep rows whose date is today (--keep-days 1) or within the last N days;
  * a round block (`# round N <time>` + rows + `# round N done: ...`) is kept as
    a whole when at least one of its rows is kept, so a round that crosses
    midnight does not lose its header/footer;
  * unknown comment lines are kept;
  * rows that are about to be deleted are first archived to
    `Logs/YYYYMMDD_osd_error.csv` (UTF-8 BOM CSV, one row per anomaly);
  * anomaly frames in `snapshots/osd_error/round_<N>_<time>/` that belong to
    deleted rows are deleted too (use --no-images to disable this).

The file is rewritten IN PLACE (truncate + write, same inode). This matters:
the monitor keeps the file open with O_APPEND for a whole round, so replacing
the file (os.replace) would send the rest of the round into an orphaned inode.

Usage:
    python scripts/prune_osd_error.py --once
    python scripts/prune_osd_error.py --watch        # prune at every date flip
    python scripts/prune_osd_error.py --dry-run      # show what would go
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = PROJECT_ROOT / "output" / "osd_error.txt"
DEFAULT_ARCHIVE_DIR = PROJECT_ROOT / "Logs"
DEFAULT_IMAGE_DIR = PROJECT_ROOT / "snapshots" / "osd_error"
BEIJING_TZ = timezone(timedelta(hours=8))

ROUND_HEADER = re.compile(r"^#\s*round\s+(\d+)\s+(\d{4}-\d{2}-\d{2})")
ROUND_HEADER_FULL = re.compile(
    r"^#\s*round\s+(\d+)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
ROUND_DONE = re.compile(r"^#\s*round\s+(\d+)\s+done:")
ROW_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}) ")

# osd_error.txt rows: time index channelCode name expected detected confidence status
CSV_HEADER = ["时间", "编号", "通道编码", "通道名称", "期望", "识别",
              "置信度", "状态"]


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def split_blocks(lines: list[str]) -> list[dict]:
    """Group the file into round blocks: header / rows / footer."""
    blocks: list[dict] = []
    current = {"head": None, "rows": [], "tail": None, "other": []}
    for line in lines:
        stripped = line.rstrip("\r\n")
        header = ROUND_HEADER.match(stripped)
        done = ROUND_DONE.match(stripped)
        row = ROW_DATE.match(stripped)
        if header:
            blocks.append(current)
            current = {"head": line, "rows": [], "tail": None, "other": []}
        elif done:
            current["tail"] = line
            blocks.append(current)
            current = {"head": None, "rows": [], "tail": None, "other": []}
        elif row:
            current["rows"].append((row.group(1), line))
        else:
            current["other"].append(line)
    blocks.append(current)
    return blocks


def plan_prune_details(
        path: Path, keep_days: int
) -> tuple[list[str], list[str], list[str], list[tuple[str, str | None]]]:
    """Return kept/dropped lines and dropped rows with their round header.

    The fourth value is ``(data_row, round_header)`` for every dropped data
    row; it is used to delete the corresponding per-round anomaly frames.
    """
    if not path.exists():
        return [], [], [], []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    cutoff = (now_beijing() - timedelta(days=max(0, keep_days - 1))).strftime("%Y-%m-%d")

    kept: list[str] = []
    dropped: list[str] = []
    dropped_rows: list[str] = []
    dropped_round_rows: list[tuple[str, str | None]] = []
    for block in split_blocks(lines):
        head_date = None
        if block["head"]:
            head_date = ROUND_HEADER.match(block["head"]).group(2)
        keep_rows = [line for date, line in block["rows"] if date >= cutoff]
        keep = bool(keep_rows) or (head_date is not None and head_date >= cutoff)
        if not block["rows"] and head_date is None:
            keep = True  # plain comment / blank line block

        if not keep:
            if block["head"]:
                dropped.append(block["head"])
            dropped.extend(line for _date, line in block["rows"])
            if block["tail"]:
                dropped.append(block["tail"])
            dropped.extend(block["other"])
            dropped_rows.extend(line for _date, line in block["rows"])
            dropped_round_rows.extend(
                (line, block["head"]) for _date, line in block["rows"]
            )
            continue

        if block["head"]:
            kept.append(block["head"])
        kept.extend(keep_rows)
        if block["tail"]:
            kept.append(block["tail"])
        kept.extend(block["other"])
        for date, line in block["rows"]:
            if date < cutoff:
                # A cross-midnight block is kept for its current-day rows;
                # archive (and later remove) its older rows as well.
                dropped.append(line)
                dropped_rows.append(line)
                dropped_round_rows.append((line, block["head"]))
    return kept, dropped, dropped_rows, dropped_round_rows


def plan_prune(path: Path, keep_days: int) -> tuple[list[str], list[str], list[str]]:
    """Backward-compatible wrapper used by daily_refresh.py."""
    kept, dropped, dropped_rows, _ = plan_prune_details(path, keep_days)
    return kept, dropped, dropped_rows


def archive_rows(rows: list[str], archive_dir: Path,
                 day: str | None = None) -> tuple[Path | None, int]:
    """Write dropped tab-separated rows to Logs/YYYYMMDD_osd_error.csv.

    Existing rows are merged and de-duplicated, so osd-daily and
    osd-error-prune can both archive the same rows without creating duplicates.
    """
    parsed: list[list[str]] = []
    for line in rows:
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 8:
            continue
        parsed.append(parts[:8])
    if not parsed:
        return None, 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    day = day or now_beijing().strftime("%Y%m%d")
    path = archive_dir / f"{day}_osd_error.csv"

    existing: list[list[str]] = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            for i, row in enumerate(reader):
                if i == 0 and row and row[0] in ("时间", "time"):
                    continue
                if len(row) >= 8:
                    existing.append(row[:8])

    seen: set[tuple[str, ...]] = set()
    combined: list[list[str]] = []
    for row in existing + parsed:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        combined.append(row)

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(combined)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path, len(parsed)


def _round_folder_from_head(head: str | None) -> str | None:
    if not head:
        return None
    match = ROUND_HEADER_FULL.match(head)
    if not match:
        return None
    stamp = (match.group(2).replace("-", "")
             .replace(":", "").replace(" ", "_"))
    return f"round_{int(match.group(1)):04d}_{stamp}"


def _image_name_from_row(line: str) -> str | None:
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < 3:
        return None
    index_text = parts[1].strip()
    channel_code = parts[2].strip()
    if not index_text.isdigit():
        return None
    cleaned = re.sub(r"[^0-9A-Za-z@_-]+", "_", channel_code)
    if not cleaned:
        return None
    return f"{int(index_text):04d}_{cleaned}.jpg"


def delete_round_images(
        dropped_round_rows: list[tuple[str, str | None]],
        image_dir: Path | None,
        dry_run: bool) -> tuple[int, list[str]]:
    """Delete frames belonging only to rows removed from osd_error.txt."""
    if image_dir is None:
        return 0, []

    deleted = 0
    errors: list[str] = []
    folders: set[Path] = set()
    for line, head in dropped_round_rows:
        folder_name = _round_folder_from_head(head)
        image_name = _image_name_from_row(line)
        if not folder_name or not image_name:
            continue
        folder = image_dir / folder_name
        folders.add(folder)
        path = folder / image_name
        try:
            if path.is_file():
                if not dry_run:
                    path.unlink()
                deleted += 1
        except OSError as exc:
            errors.append(f"{path}: {exc}")

    if not dry_run:
        for folder in folders:
            try:
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            except OSError as exc:
                errors.append(f"{folder}: {exc}")
    return deleted, errors


def prune(path: Path, keep_days: int, dry_run: bool,
          archive_dir: Path | None = None,
          image_dir: Path | None = None
          ) -> tuple[int, int, int, Path | None, int, list[str]]:
    """Keep the last `keep_days` days.

    Returns (kept_lines, dropped_lines, archived_rows, archive_path,
    deleted_images, image_errors).  If archiving fails, the original file is
    not rewritten and no images are deleted.
    """
    kept, dropped, dropped_rows, dropped_round_rows = plan_prune_details(
        path, keep_days)
    archive_path = None
    archived_rows = 0
    if dropped_rows and archive_dir is not None:
        if dry_run:
            archived_rows = len(dropped_rows)
        else:
            # Archive first: a failure here must not lose the rows.
            archive_path, archived_rows = archive_rows(
                dropped_rows, Path(archive_dir),
                day=now_beijing().strftime("%Y%m%d"))

    if dropped and not dry_run:
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(kept)
            handle.flush()
            os.fsync(handle.fileno())

    deleted_images, image_errors = delete_round_images(
        dropped_round_rows, image_dir, dry_run)
    return (len(kept), len(dropped), archived_rows, archive_path,
            deleted_images, image_errors)


def watch(path: Path, keep_days: int, poll_seconds: float,
          archive_dir: Path | None, image_dir: Path | None) -> None:
    last_day = None
    while True:
        day = now_beijing().strftime("%Y-%m-%d")
        if day != last_day:
            try:
                (kept, dropped, archived, archive_path,
                 deleted_images, image_errors) = prune(
                    path, keep_days, dry_run=False, archive_dir=archive_dir,
                    image_dir=image_dir)
                extra = f" archived={archived} -> {archive_path}" if archive_path else ""
                image_text = f" images_deleted={deleted_images}"
                if image_errors:
                    image_text += f" image_errors={len(image_errors)}"
                print(f"{now_beijing():%Y-%m-%d %H:%M:%S} prune {path.name}: "
                      f"kept={kept} dropped={dropped}{extra}{image_text}",
                      flush=True)
                for message in image_errors:
                    print(f"  warn: cannot delete frame {message}", flush=True)
                last_day = day
            except Exception as exc:
                # Do not delete anything if archiving failed; try again on the
                # next poll instead of crashing the long-running service.
                print(f"{now_beijing():%Y-%m-%d %H:%M:%S} prune failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--keep-days", type=int, default=1,
                        help="days to keep, 1 = today only (default)")
    parser.add_argument("--once", action="store_true",
                        help="prune once and exit (default when --watch is absent)")
    parser.add_argument("--watch", action="store_true",
                        help="run forever, prune at every date change")
    parser.add_argument("--poll", type=float, default=300.0,
                        help="seconds between date checks in --watch (default 300)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the counts without touching the file")
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR,
                        help=f"archive directory (default: {DEFAULT_ARCHIVE_DIR})")
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR,
                        help=f"per-round anomaly frame directory "
                             f"(default: {DEFAULT_IMAGE_DIR})")
    parser.add_argument("--no-archive", action="store_true",
                        help="delete without writing an archive CSV")
    parser.add_argument("--no-images", action="store_true",
                        help="do not delete per-round anomaly frames")
    args = parser.parse_args()

    if args.keep_days < 1:
        parser.error("--keep-days must be >= 1")

    archive_dir = None if args.no_archive else args.archive_dir
    image_dir = None if args.no_images else args.image_dir
    if args.watch:
        watch(args.file, args.keep_days, args.poll, archive_dir, image_dir)
        return

    (kept, dropped, archived, archive_path,
     deleted_images, image_errors) = prune(
        args.file, args.keep_days, args.dry_run, archive_dir, image_dir)
    action = "would drop" if args.dry_run else "dropped"
    archive_text = ""
    if archived:
        archive_text = (f" would archive={archived}" if args.dry_run
                        else f" archived={archived} -> {archive_path}")
    image_text = ""
    if deleted_images:
        image_text = (f" would delete-images={deleted_images}"
                      if args.dry_run else f" images_deleted={deleted_images}")
    print(f"{now_beijing():%Y-%m-%d %H:%M:%S} {args.file}: "
          f"kept={kept} {action}={dropped}{archive_text}{image_text}",
          flush=True)
    for message in image_errors:
        print(f"  warn: cannot delete frame {message}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
