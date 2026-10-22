"""Read-only data access for the OSD result dashboard demo.

The dashboard never writes to the monitor's files.  It reads:

* ``output/osd_error.txt``      today's anomalies, with ``# round`` markers
* ``Logs/*_osd_error.csv``      archived anomalies from previous days
* ``output/osd_rounds.csv``     optional; used to infer round boundaries for
                                archived rows (the archive itself has no round
                                markers)

Only the standard library is used here so the parsing can be tested without
starting the web service.
"""
from __future__ import annotations

import bisect
import csv
import os
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BEIJING_TZ = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[1]

OSD_ERROR_FILE = Path(os.getenv(
    "OSD_ERROR_FILE", str(PROJECT_ROOT / "output" / "osd_error.txt")))
OSD_ROUNDS_FILE = Path(os.getenv(
    "OSD_ROUNDS_FILE", str(PROJECT_ROOT / "output" / "osd_rounds.csv")))
CHANNELS_FILE = Path(os.getenv(
    "CHANNELS_FILE", str(PROJECT_ROOT / "out" / "channels.csv")))
ERROR_IMAGE_DIR = Path(os.getenv(
    "ERROR_IMAGE_DIR", str(PROJECT_ROOT / "snapshots" / "osd_error")))
ARCHIVE_DIR = Path(os.getenv(
    "ARCHIVE_DIR", str(PROJECT_ROOT / "Logs")))

HISTORY_ROUNDS = os.getenv("HISTORY_ROUNDS", "1").strip().lower() not in (
    "0", "false", "no", "off")
DEFAULT_PAGE_SIZE = int(os.getenv("DASHBOARD_PAGE_SIZE", "50"))
MAX_PAGE_SIZE = int(os.getenv("DASHBOARD_MAX_PAGE_SIZE", "200"))
STREAM_TOP_N = max(3, int(os.getenv("STREAM_TOP_N", "50")))

ROUND_HEADER_RE = re.compile(
    r"^#\s*round\s+(\d+)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
ROUND_DONE_RE = re.compile(
    r"^#\s*round\s+(\d+)\s+done:\s+(\d+)/(\d+) OK,\s+(\d+) anomaly"
    r"(?: logged)?,\s+(?:(\d+) capture timeout skipped,\s+)?([\d.]+)s")
ROUND_UNCERTAIN_RE = re.compile(
    r"^#\s*round\s+(\d+)\s+uncertain:\s+(\d+)")
CSV_HEADER_NAMES = {"时间", "time"}

_lock = threading.Lock()
_live_cache: tuple[tuple[int, int], list[dict[str, Any]]] | None = None
_archive_cache: dict[Path, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
_round_spans_cache: tuple[tuple[int, int], list[dict[str, Any]]] | None = None
_channel_cache: (
    tuple[tuple[Any, ...], dict[str, Any]] | None
) = None
_streaming_cache: (
    tuple[tuple[Any, ...], dict[str, Any]] | None
) = None


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def today_str() -> str:
    return now_beijing().strftime("%Y-%m-%d")


def display_path(path: Path) -> str:
    """Short, stable path for the UI (output/... rather than /work/output/...)."""
    for base in (Path("/work"), PROJECT_ROOT):
        try:
            return str(path.resolve().relative_to(base.resolve()))
        except ValueError:
            continue
    return path.name


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_row(parts: list[str]) -> dict[str, Any] | None:
    if len(parts) < 8:
        return None
    p = [item.strip() for item in parts[:8]]
    if not p[0] or not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", p[0]):
        return None
    try:
        confidence: float | None = float(p[6])
    except ValueError:
        confidence = None
    return {
        "time": p[0],
        "index": int(p[1]) if p[1].isdigit() else None,
        "channelCode": p[2],
        "name": p[3],
        "expected": p[4],
        "detected": p[5],
        "confidence": confidence,
        "status": p[7],
    }


def parse_live_blocks(path: Path = OSD_ERROR_FILE) -> list[dict[str, Any]]:
    """Parse osd_error.txt into round blocks.

    A block is created for every ``# round N <time>`` header.  Rows written
    before the first header (or after a malformed header) are kept in a
    synthetic block so they are never silently lost.
    """
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8", errors="replace")
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue

        if line.startswith("#"):
            header = ROUND_HEADER_RE.match(line)
            if header:
                current = {
                    "number": int(header.group(1)),
                    "startedAt": header.group(2),
                    "rows": [],
                    "done": None,
                    "uncertain": None,
                    "sequence": len(blocks) + 1,
                }
                blocks.append(current)
                continue

            done = ROUND_DONE_RE.match(line)
            if done:
                matching = current
                if matching is None or matching["number"] != int(done.group(1)):
                    matching = next(
                        (b for b in reversed(blocks)
                         if b["number"] == int(done.group(1))), None)
                if matching is not None:
                    matching["done"] = {
                        "ok": int(done.group(2)),
                        "channels": int(done.group(3)),
                        "anomalies": int(done.group(4)),
                        "timeoutsSkipped": (int(done.group(5))
                                            if done.group(5) is not None else None),
                        "durationSeconds": float(done.group(6)),
                    }
                    current = matching
                continue

            uncertain = ROUND_UNCERTAIN_RE.match(line)
            if uncertain:
                matching = current
                if matching is None or matching["number"] != int(uncertain.group(1)):
                    matching = next(
                        (b for b in reversed(blocks)
                         if b["number"] == int(uncertain.group(1))), None)
                if matching is not None:
                    matching["uncertain"] = int(uncertain.group(2))
                continue
            continue

        row = _parse_row(line.split("\t"))
        if row is None:
            continue
        if current is None:
            current = {
                "number": None,
                "startedAt": None,
                "rows": [],
                "done": None,
                "uncertain": None,
                "sequence": len(blocks) + 1,
            }
            blocks.append(current)
        current["rows"].append(row)

    return blocks


def _load_live_blocks() -> list[dict[str, Any]]:
    global _live_cache
    if not OSD_ERROR_FILE.exists():
        return []
    stat = OSD_ERROR_FILE.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _live_cache is not None and _live_cache[0] == key:
            return _live_cache[1]
        blocks = parse_live_blocks(OSD_ERROR_FILE)
        _live_cache = (key, blocks)
        return blocks


def _read_archive(path: Path) -> list[dict[str, Any]]:
    """Read one UTF-8-BOM archive CSV, cached by file version."""
    if not path.exists():
        return []
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        cached = _archive_cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]

    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8-sig", newline="", errors="replace") as handle:
            for row in csv.reader(handle):
                if not row:
                    continue
                if row[0].strip() in CSV_HEADER_NAMES:
                    continue
                parsed = _parse_row(row)
                if parsed is not None:
                    rows.append(parsed)
    except OSError:
        return []

    with _lock:
        _archive_cache[path] = (key, rows)
    return rows


def _archive_files() -> list[Path]:
    if not ARCHIVE_DIR.exists():
        return []
    return sorted(ARCHIVE_DIR.glob("*_osd_error.csv"))


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["time"], row["index"], row["channelCode"], row["name"],
        row["expected"], row["detected"], row["confidence"], row["status"],
    )


def _unique_sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = _row_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: (r["time"], r["index"] or 0))
    return unique


def _archive_rows_for_date(date: str) -> tuple[list[dict[str, Any]], list[Path]]:
    rows: list[dict[str, Any]] = []
    used: list[Path] = []
    for path in _archive_files():
        file_rows = [r for r in _read_archive(path) if r["time"][:10] == date]
        if file_rows:
            used.append(path)
            rows.extend(file_rows)

    return _unique_sorted(rows), used


# ---------------------------------------------------------------------------
# "Crossover" (串流) statistics for the chart on the daily page.
#
# osd_error.txt does not store the monitor's internal reason=CROSSOVER flag.
# We reproduce the check from the channel table: the detected OSD code matches
# another channel's expected code.  Exact canonical/numeric equality is used
# (leading zeros ignored) instead of the monitor's looser substring rule, which
# would count too many short numeric fragments as crossovers.
# ---------------------------------------------------------------------------

def _frame_filename(index: int | None, channel_code: str) -> str | None:
    """Mirror evo_osd_monitor.safe_name(): 0007_1002484_1_0_0.jpg."""
    if index is None:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z@_-]+", "_", channel_code or "")
    if not cleaned:
        return None
    return f"{int(index):04d}_{cleaned}.jpg"


def _round_folder(round_no: int | None, started_at: str | None) -> str | None:
    """Mirror evo_osd_monitor.Monitor.start_round()."""
    if round_no is None or not started_at:
        return None
    stamp = started_at.replace("-", "").replace(":", "").replace(" ", "_")
    return f"round_{int(round_no):04d}_{stamp}"


def frame_path(index: int | None, channel_code: str,
               frame_dir: str | None = None) -> Path | None:
    name = _frame_filename(index, channel_code)
    if not name:
        return None

    if frame_dir:
        if not re.fullmatch(r"round_\d{4,}_\d{8}_\d{6}", frame_dir):
            return None
        round_dir = ERROR_IMAGE_DIR / frame_dir
        if round_dir.is_dir():
            path = round_dir / name
            return path if path.is_file() else None
        # Legacy rows before per-round folders existed: fall back to the flat
        # filename (the old overwrite-in-place layout).
    path = ERROR_IMAGE_DIR / name
    return path if path.is_file() else None


def frame_exists(index: int | None, channel_code: str,
                 frame_dir: str | None = None) -> bool:
    return frame_path(index, channel_code, frame_dir) is not None


def _load_channel_index() -> dict[str, Any]:
    """channelCode -> channel/device names, cached by file version."""
    global _channel_cache
    empty: dict[str, Any] = {"channels": {}}
    if not CHANNELS_FILE.exists():
        return empty

    channel_stat = CHANNELS_FILE.stat()
    channel_key = (channel_stat.st_mtime_ns, channel_stat.st_size)
    with _lock:
        if _channel_cache is not None and _channel_cache[0] == channel_key:
            return _channel_cache[1]

    channels: dict[str, dict[str, str]] = {}
    try:
        with CHANNELS_FILE.open(
                encoding="utf-8-sig", newline="", errors="replace") as handle:
            for row in csv.DictReader(handle):
                code = (row.get("通道编码") or "").strip()
                if not code:
                    continue
                channel_name = (row.get("通道名称") or "").strip()
                channels[code] = {
                    "channelName": channel_name,
                    "deviceName": ((row.get("设备名称") or "").strip()
                                   or channel_name or code),
                }
    except OSError:
        return empty

    index = {"channels": channels}
    with _lock:
        _channel_cache = (channel_key, index)
    return index


def _build_streaming(rows: list[dict[str, Any]],
                     index: dict[str, Any]) -> dict[str, Any]:
    source_counts: Counter[str] = Counter()
    source_names: dict[str, str] = {}
    mismatch_count = 0

    for row in rows:
        if not (row["status"] or "").upper().startswith("MISMATCH"):
            continue
        mismatch_count += 1
        channel_code = (row["channelCode"] or "").strip() or "未知通道"
        source_counts[channel_code] += 1
        if channel_code not in source_names:
            info = index["channels"].get(channel_code)
            source_names[channel_code] = (
                (info["deviceName"] if info else "")
                or row["name"] or ""
            )

    source = [
        {
            "name": channel_code,
            "count": count,
            "deviceName": source_names.get(channel_code, ""),
        }
        for channel_code, count in sorted(
            source_counts.items(), key=lambda item: (-item[1], item[0])
        )[:STREAM_TOP_N]
    ]

    return {
        "totalRows": len(rows),
        "mismatchCount": mismatch_count,
        "topN": STREAM_TOP_N,
        "source": source,
    }


def get_streaming_summary(date: str | None = None) -> dict[str, Any]:
    """MISMATCH counts by channel/device for one day (bar charts)."""
    global _streaming_cache
    date = date or today_str()
    today = today_str()
    index = _load_channel_index()

    if date == today:
        try:
            live_stat = OSD_ERROR_FILE.stat()
            live_key = (live_stat.st_mtime_ns, live_stat.st_size)
        except OSError:
            live_key = (0, 0)
        try:
            channel_stat = CHANNELS_FILE.stat()
            channel_key = (channel_stat.st_mtime_ns, channel_stat.st_size)
        except OSError:
            channel_key = (0, 0)

        key = (date, live_key, channel_key, STREAM_TOP_N)
        with _lock:
            if _streaming_cache is not None and _streaming_cache[0] == key:
                return _streaming_cache[1]
        rows = [
            row for block in _load_live_blocks() for row in block["rows"]
            if row["time"][:10] == date
        ]
        summary = _build_streaming(rows, index)
        with _lock:
            _streaming_cache = (key, summary)
        return summary

    rows, _files = _archive_rows_for_date(date)
    live_rows = [
        row for block in _load_live_blocks() for row in block["rows"]
        if row["time"][:10] == date
    ]
    if live_rows:
        rows = _unique_sorted(rows + live_rows)
    return _build_streaming(rows, index)


# ---------------------------------------------------------------------------
# Round grouping
# ---------------------------------------------------------------------------

def _done_view(done: dict[str, Any] | None) -> dict[str, Any] | None:
    if not done:
        return None
    return {
        "ok": done.get("ok"),
        "channels": done.get("channels"),
        "anomalies": done.get("anomalies"),
        "timeoutsSkipped": done.get("timeoutsSkipped"),
        "durationSeconds": done.get("durationSeconds"),
    }


def _live_groups(date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    blocks = _load_live_blocks()
    groups: list[dict[str, Any]] = []
    sequence = 0

    # Only the last block can be "in progress".  Older blocks can lack a done
    # line too when the monitor was restarted mid-round; those are not running.
    in_progress_block = None
    if blocks:
        last = blocks[-1]
        if last["done"] is None and (
                (last["startedAt"] or "")[:10] == date
                or any(r["time"][:10] == date for r in last["rows"])):
            in_progress_block = last

    for block in blocks:
        rows = [r for r in block["rows"] if r["time"][:10] == date]
        if rows:
            sequence += 1
            groups.append({
                "key": f"live-{block['sequence']}",
                "displayNumber": sequence,
                "number": block["number"],
                "startedAt": block["startedAt"],
                "endedAt": max(r["time"] for r in rows),
                "frameDir": _round_folder(
                    block["number"], block["startedAt"]),
                "done": _done_view(block["done"]),
                "uncertain": block["uncertain"],
                "inferred": False,
                "inProgress": block is in_progress_block,
                "crossesMidnight": bool(
                    block["startedAt"] and block["startedAt"][:10] < date),
                "roundTotal": len(rows),
                "rows": rows,
            })

    in_progress = None
    if in_progress_block is not None:
        in_progress = {
            "number": in_progress_block["number"],
            "startedAt": in_progress_block["startedAt"],
            "rowCount": sum(1 for r in in_progress_block["rows"]
                            if r["time"][:10] == date),
        }

    updated_at = None
    age_seconds = None
    if OSD_ERROR_FILE.exists():
        stat = OSD_ERROR_FILE.stat()
        updated = datetime.fromtimestamp(stat.st_mtime, BEIJING_TZ)
        updated_at = updated.strftime("%Y-%m-%d %H:%M:%S")
        age_seconds = max(0, int((now_beijing() - updated).total_seconds()))

    source = {
        "type": "live",
        "label": display_path(OSD_ERROR_FILE),
        "files": [display_path(OSD_ERROR_FILE)],
        "updatedAt": updated_at,
        "ageSeconds": age_seconds,
    }
    return groups, {"source": source, "inProgress": in_progress,
                    "roundSource": "live"}


def _load_round_spans() -> list[dict[str, Any]]:
    """Infer round spans from osd_rounds.csv.

    Every round contains each channel index exactly once, and rows are appended
    as channels complete.  Seeing an index again therefore means a new round
    started.  This recovers the round boundaries that the daily archive CSV
    does not store.
    """
    global _round_spans_cache
    if not HISTORY_ROUNDS or not OSD_ROUNDS_FILE.exists():
        return []
    stat = OSD_ROUNDS_FILE.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _round_spans_cache is not None and _round_spans_cache[0] == key:
            return _round_spans_cache[1]

    spans: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = None
    try:
        with OSD_ROUNDS_FILE.open(
                encoding="utf-8-sig", newline="", errors="replace") as handle:
            for row in csv.reader(handle):
                if len(row) < 8:
                    continue
                time_text = row[0].strip()
                index_text = row[1].strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                                time_text):
                    continue
                if index_text in seen:
                    spans.append(current)  # type: ignore[arg-type]
                    current = None
                    seen = set()
                if current is None:
                    current = {
                        "start": time_text,
                        "end": time_text,
                        "rows": 0,
                    }
                seen.add(index_text)
                if time_text < current["start"]:
                    current["start"] = time_text
                if time_text > current["end"]:
                    current["end"] = time_text
                current["rows"] += 1
    except OSError:
        return []

    if current is not None and current["rows"]:
        spans.append(current)

    spans.sort(key=lambda span: span["start"])
    with _lock:
        _round_spans_cache = (key, spans)
    return spans


def _history_groups(date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, files = _archive_rows_for_date(date)

    # A cross-midnight round can still have older rows in the live file when
    # --keep-days is greater than 1 (or before the prune service runs).  Include
    # them so a historical date is not missing records that are still on disk.
    live_rows = [
        row for block in _load_live_blocks() for row in block["rows"]
        if row["time"][:10] == date
    ]
    if live_rows:
        rows = _unique_sorted(rows + live_rows)
        if OSD_ERROR_FILE.exists() and OSD_ERROR_FILE not in files:
            files.append(OSD_ERROR_FILE)

    updated_at = None
    if files:
        latest = max(files, key=lambda p: p.stat().st_mtime)
        updated_at = datetime.fromtimestamp(
            latest.stat().st_mtime, BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

    source = {
        "type": "archive",
        "label": "Logs/*_osd_error.csv",
        "files": [display_path(path) for path in files],
        "updatedAt": updated_at,
        "ageSeconds": None,
    }

    if not rows:
        return [], {"source": source, "inProgress": None,
                    "roundSource": "none"}

    spans = _load_round_spans()
    groups: list[dict[str, Any]] = []
    round_source = "none"

    if spans:
        starts = [span["start"] for span in spans]
        matched: dict[int, list[dict[str, Any]]] = {}
        unmatched: list[dict[str, Any]] = []
        for row in rows:
            pos = bisect.bisect_right(starts, row["time"]) - 1
            if pos >= 0 and row["time"] <= spans[pos]["end"]:
                matched.setdefault(pos, []).append(row)
            else:
                unmatched.append(row)

        sequence = 0
        for pos in sorted(matched, key=lambda i: spans[i]["start"]):
            span = spans[pos]
            matched_rows = matched[pos]
            sequence += 1
            groups.append({
                "key": f"history-span-{pos}",
                "displayNumber": sequence,
                "number": None,
                "startedAt": span["start"],
                "endedAt": max(r["time"] for r in matched_rows),
                "done": None,
                "uncertain": None,
                "inferred": True,
                "inProgress": False,
                "crossesMidnight": span["start"][:10] < date,
                "roundTotal": len(matched_rows),
                "rows": matched_rows,
            })
        if unmatched:
            groups.append({
                "key": "history-unmatched",
                "displayNumber": None,
                "number": None,
                "startedAt": min(r["time"] for r in unmatched),
                "endedAt": max(r["time"] for r in unmatched),
                "done": None,
                "uncertain": None,
                "inferred": False,
                "inProgress": False,
                "crossesMidnight": False,
                "roundTotal": len(unmatched),
                "rows": unmatched,
            })
        if matched:
            round_source = "inferred"
    else:
        groups.append({
            "key": "history-all",
            "displayNumber": None,
            "number": None,
            "startedAt": rows[0]["time"],
            "endedAt": max(r["time"] for r in rows),
            "done": None,
            "uncertain": None,
            "inferred": False,
            "inProgress": False,
            "crossesMidnight": False,
            "roundTotal": len(rows),
            "rows": rows,
        })

    return groups, {"source": source, "inProgress": None,
                    "roundSource": round_source}


# ---------------------------------------------------------------------------
# Public API used by the FastAPI layer
# ---------------------------------------------------------------------------

def get_dates() -> dict[str, Any]:
    """Dates that have anomalies, plus counts for the date picker."""
    today = today_str()
    keys_by_date: dict[str, set[tuple[Any, ...]]] = {today: set()}

    for block in _load_live_blocks():
        for row in block["rows"]:
            keys_by_date.setdefault(row["time"][:10], set()).add(_row_key(row))

    for path in _archive_files():
        for row in _read_archive(path):
            date = row["time"][:10]
            keys_by_date.setdefault(date, set()).add(_row_key(row))

    counts = {date: len(keys) for date, keys in keys_by_date.items()}
    items = [
        {"date": date, "count": count, "isToday": date == today}
        for date, count in sorted(counts.items(), reverse=True)
        if count > 0 or date == today
    ]
    dates = [item["date"] for item in items]
    return {
        "today": today,
        "minDate": min(dates) if dates else today,
        "maxDate": today,
        "items": items,
    }


def get_results(
    date: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    channel_code: str = "",
    order: str = "desc",
) -> dict[str, Any]:
    """Return one page of anomalies, grouped by detection round."""
    today = today_str()
    date = date or today
    channel_code = (channel_code or "").strip()
    is_today = date == today

    if date > today:
        raise ValueError("不能查询未来日期")
    if channel_code and not is_today:
        raise ValueError("通道编码搜索仅支持当天结果")

    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    order = "asc" if order == "asc" else "desc"

    if is_today:
        groups, meta = _live_groups(date)
    else:
        groups, meta = _history_groups(date)

    total = sum(len(group["rows"]) for group in groups)
    round_count = len(groups)

    if channel_code:
        needle = channel_code.casefold()
        filtered: list[dict[str, Any]] = []
        for group in groups:
            matched_rows = [
                row for row in group["rows"]
                if needle in row["channelCode"].casefold()
            ]
            if matched_rows:
                copied = dict(group)
                copied["rows"] = matched_rows
                filtered.append(copied)
        groups = filtered

    # Keep the display number from the original round order, but show newest
    # rounds first by default.
    groups.sort(
        key=lambda g: g["displayNumber"] or 0,
        reverse=(order == "desc"),
    )
    # Groups without round information always go last.
    groups.sort(key=lambda g: g["displayNumber"] is None)
    for group in groups:
        group["rows"].sort(
            key=lambda row: (row["time"], row["index"] or 0),
            reverse=(order == "desc"),
        )

    flat: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for group in groups:
        view = {key: value for key, value in group.items() if key != "rows"}
        view["matchedTotal"] = len(group["rows"])
        view["_flatStart"] = len(flat)
        for row in group["rows"]:
            flat.append((view, row))

    total_matched = len(flat)
    total_pages = ((total_matched + page_size - 1) // page_size
                   if total_matched else 0)
    page = max(1, int(page))
    if total_pages:
        page = min(page, total_pages)
        start = (page - 1) * page_size
        end = min(start + page_size, total_matched)
    else:
        page = 1
        start = end = 0

    page_groups: list[dict[str, Any]] = []
    for group_view, row in flat[start:end]:
        if not page_groups or page_groups[-1]["key"] != group_view["key"]:
            page_groups.append({
                **{key: value for key, value in group_view.items()
                   if key != "_flatStart"},
                "rows": [],
                "_flatStart": group_view["_flatStart"],
            })
        page_groups[-1]["rows"].append(row)

    for group in page_groups:
        group["continued"] = start > group["_flatStart"]
        group["offsetInRound"] = max(0, start - group["_flatStart"])
        del group["_flatStart"]
        frame_dir = group.get("frameDir")
        for row in group["rows"]:
            row["hasImage"] = False
            row.pop("frameDir", None)
            if (is_today
                    and (row["status"] or "").upper().startswith("MISMATCH")):
                row["hasImage"] = frame_exists(
                    row["index"], row["channelCode"], frame_dir)
                if row["hasImage"]:
                    row["frameDir"] = frame_dir

    return {
        "date": date,
        "today": today,
        "isToday": is_today,
        "search": channel_code or None,
        "source": meta["source"],
        "roundSource": meta["roundSource"],
        "updatedAt": meta["source"].get("updatedAt"),
        "ageSeconds": meta["source"].get("ageSeconds"),
        "inProgress": meta.get("inProgress"),
        "total": total,
        "totalMatched": total_matched,
        "roundCount": round_count,
        "matchedRoundCount": len(groups),
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        "hasPrev": page > 1,
        "hasNext": page < total_pages,
        "order": order,
        "streaming": get_streaming_summary(date),
        "groups": page_groups,
    }
