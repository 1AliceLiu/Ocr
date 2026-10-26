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
import io
import os
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import confirmations as confirmation_store

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
CONFIRM_RESULTS = {"confirmed", "false_positive"}

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


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _row_id(row: dict[str, Any]) -> str:
    index = row.get("index")
    return f"{row.get('time', '')}|{'' if index is None else index}|{row.get('channelCode', '')}"


def _span_index_for_time(
    spans: list[dict[str, Any]], starts: list[str], time_text: str,
) -> int | None:
    position = bisect.bisect_right(starts, time_text) - 1
    if position < 0:
        return None
    if time_text > spans[position]["end"]:
        if (position + 1 < len(spans)
                and spans[position + 1]["start"] <= time_text):
            position += 1
        else:
            return None
    return position


def _auto_confirm_rows(
    date: str,
    groups: list[dict[str, Any]],
    confirmations: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Confirm the next round when the same channel was confirmed last round.

    Only the immediately previous round counts.  The previous confirmation may
    be manual or auto, so a confirmed stream keeps propagating through
    consecutive MISMATCH rounds until the chain breaks.
    """
    spans = _load_round_spans()
    if not spans:
        return confirmations
    starts = [span["start"] for span in spans]
    previous_date = (
        datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    records = list(confirmations.values()) + confirmation_store.list_for_date(previous_date)
    confirmed_index: dict[tuple[str, int], dict[str, Any]] = {}
    for record in records:
        if record.get("result") != "confirmed":
            continue
        position = _span_index_for_time(spans, starts, record.get("time", ""))
        if position is None:
            continue
        confirmed_index[(record["channel_code"], position)] = record

    rows: list[dict[str, Any]] = []
    for group in groups:
        for row in group["rows"]:
            if (row.get("status") or "").upper().startswith("MISMATCH"):
                rows.append(row)
    rows.sort(key=lambda row: (row["time"], row["index"] or 0))

    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        key = _row_id(row)
        existing = confirmations.get(key)
        if existing is not None and existing.get("result") is not None:
            continue
        position = _span_index_for_time(spans, starts, row["time"])
        if position is None or position == 0:
            continue
        channel_code = row.get("channelCode") or ""
        if (channel_code, position - 1) not in confirmed_index:
            continue
        record = confirmation_store.save(
            row_key=key,
            confirm_date=date,
            time=row["time"],
            channel_code=channel_code,
            channel_name=row.get("name") or "",
            result="confirmed",
            note="",
            confirmed_at=now,
            actor="system",
            source="auto",
        )
        if record is None:
            continue
        confirmation_store.log_event(
            row_key=key,
            action="auto-confirm",
            result="confirmed",
            note="",
            at=now,
            confirm_date=date,
            channel_code=channel_code,
            actor="system",
            source="auto",
        )
        confirmations[key] = record
        confirmed_index[(channel_code, position)] = record
    return confirmations


def _done_windows() -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    for block in _load_live_blocks():
        done = block.get("done") or {}
        start = _parse_ts(block.get("startedAt"))
        duration = done.get("durationSeconds")
        if start is None or duration is None:
            continue
        windows.append((start, start + timedelta(seconds=float(duration))))
    return windows


def _span_has_done(span: dict[str, Any],
                   windows: list[tuple[datetime, datetime]]) -> bool:
    start = _parse_ts(span.get("start"))
    end = _parse_ts(span.get("end"))
    if start is None or end is None:
        return False
    for window_start, window_end in windows:
        if (abs((start - window_start).total_seconds()) <= 300
                and abs((end - window_end).total_seconds()) <= 300):
            return True
    return False


def _report_ready(date: str) -> tuple[bool, str]:
    today = today_str()
    if date > today:
        return False, "不能查询未来日期"
    if date == today:
        return False, "当天所有轮次尚未结束，日报将在最后一轮完成后生成"
    spans = _load_round_spans()
    date_spans = [span for span in spans if span["start"][:10] == date]
    if not date_spans:
        return False, "该日期没有轮次数据"
    last = date_spans[-1]
    if any(span["start"] > last["start"] for span in spans):
        return True, ""
    if last["end"][:10] <= date:
        return True, ""
    if _span_has_done(last, _done_windows()):
        return True, ""
    return False, "该日期最后一轮尚未结束"


def _stop_restart(
    events: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    date: str,
) -> list[dict[str, Any]]:
    """Three complete rounds without a confirmed stream = stopped.

    A later confirmed stream in the day is a restart.  If the last stream is
    followed by three rounds on that date, it is a stop with no restart.
    """
    if not events or not spans:
        return []
    starts = [span["start"] for span in spans]
    by_span: dict[int, list[str]] = {}
    for event in events:
        position = bisect.bisect_right(starts, event["time"]) - 1
        if position < 0:
            continue
        if (event["time"] > spans[position]["end"]
                and position + 1 < len(spans)
                and spans[position + 1]["start"] <= event["time"]):
            position += 1
        by_span.setdefault(position, []).append(event["time"])

    sequence = sorted(by_span)
    result: list[dict[str, Any]] = []
    for left, right in zip(sequence, sequence[1:]):
        missing = right - left - 1
        if missing >= 3:
            result.append({
                "stoppedAt": max(by_span[left]),
                "restartedAt": min(by_span[right]),
            })
    if sequence:
        last_position = sequence[-1]
        tail = [
            index for index, span in enumerate(spans)
            if index > last_position and span["start"][:10] == date
        ]
        if len(tail) >= 3:
            result.append({
                "stoppedAt": max(by_span[last_position]),
                "restartedAt": None,
            })
    return result


def _detected_by_row(groups: list[dict[str, Any]]) -> dict[str, str]:
    detected: dict[str, str] = {}
    for group in groups:
        for row in group["rows"]:
            value = (row.get("detected") or "").strip()
            if value:
                detected[_row_id(row)] = value
    return detected


def _value_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"detected": value, "count": count}
        for value, count in sorted(
            counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _value_text(values: list[dict[str, Any]]) -> str:
    if not values:
        return "—"
    return "、".join(
        f"{item['detected']}（{item['count']} 次）" for item in values)


def build_daily_report(date: str) -> dict[str, Any]:
    """Build the completed-day report and its TXT representation."""
    ready, reason = _report_ready(date)
    today = today_str()
    if date == today:
        groups, _meta = _live_groups(date)
    else:
        groups, _meta = _history_groups(date)
    confirmations = {
        row["row_key"]: row
        for row in confirmation_store.list_for_date(date)
    }
    _auto_confirm_rows(date, groups, confirmations)
    records = confirmation_store.list_for_date(date)
    confirmed = [row for row in records if row.get("result") == "confirmed"]
    false_positive = [
        row for row in records if row.get("result") == "false_positive"]
    checked = len(confirmed) + len(false_positive)
    accuracy = (len(confirmed) / checked * 100.0) if checked else None

    previous_date = (
        datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    if previous_date == today:
        previous_groups, _previous_meta = _live_groups(previous_date)
    else:
        previous_groups, _previous_meta = _history_groups(previous_date)
    previous_confirmations = {
        row["row_key"]: row
        for row in confirmation_store.list_for_date(previous_date)
    }
    _auto_confirm_rows(previous_date, previous_groups, previous_confirmations)
    previous = [
        row for row in confirmation_store.list_for_date(previous_date)
        if row.get("result") == "confirmed"
    ]

    today_detected = _detected_by_row(groups)
    previous_detected = _detected_by_row(previous_groups)
    today_counts = Counter(
        (row["channel_code"], row["channel_name"]) for row in confirmed)
    previous_counts = Counter(
        (row["channel_code"], row["channel_name"]) for row in previous)
    today_values: dict[tuple[str, str], Counter[str]] = {}
    previous_values: dict[tuple[str, str], Counter[str]] = {}
    for row in confirmed:
        pair = (row["channel_code"], row["channel_name"])
        value = today_detected.get(row["row_key"], "")
        if value:
            today_values.setdefault(pair, Counter())[value] += 1
    for row in previous:
        pair = (row["channel_code"], row["channel_name"])
        value = previous_detected.get(row["row_key"], "")
        if value:
            previous_values.setdefault(pair, Counter())[value] += 1

    events_by_channel: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in confirmed:
        events_by_channel.setdefault(
            (row["channel_code"], row["channel_name"]), []).append(row)

    spans = _load_round_spans()
    channels: list[dict[str, Any]] = []
    pairs = set(today_counts) | set(previous_counts)
    for code, name in sorted(
            pairs, key=lambda pair: (-today_counts[pair], pair[0], pair[1])):
        pair = (code, name)
        today_count = today_counts[pair]
        previous_count = previous_counts[pair]
        today_value_rows = _value_rows(today_values.get(pair, Counter()))
        previous_value_rows = _value_rows(previous_values.get(pair, Counter()))
        same_values = sorted(
            {item["detected"] for item in today_value_rows}
            & {item["detected"] for item in previous_value_rows}
        )
        events = sorted(
            events_by_channel.get(pair, []),
            key=lambda row: row["time"],
        )
        channels.append({
            "channelCode": code,
            "channelName": name,
            "today": today_count,
            "previous": previous_count,
            "change": today_count - previous_count,
            "todayValues": today_value_rows,
            "previousValues": previous_value_rows,
            "sameValues": same_values,
            "stops": _stop_restart(events, spans, date),
        })

    lines = [f"{date} 串流日报", f"生成时间：{now_beijing():%Y-%m-%d %H:%M:%S}", ""]
    lines.append(f"已确认记录：{checked} 条")
    lines.append(f"确认真实串流：{len(confirmed)} 条")
    lines.append(f"确认误报：{len(false_positive)} 条")
    lines.append(
        "算法检测平均准确率（人工确认）："
        + (f"{accuracy:.1f}%" if accuracy is not None else "—")
    )
    lines.append("")
    if not channels:
        lines.append("当日没有人工确认的串流记录。")
    for channel in channels:
        lines.append(
            f"通道：{channel['channelCode']} / {channel['channelName'] or '—'}")
        lines.append(
            f"今日真实串流：{channel['today']} 次 · 检测值："
            f"{_value_text(channel['todayValues'])}")
        lines.append(
            f"前日真实串流：{channel['previous']} 次 · 检测值："
            f"{_value_text(channel['previousValues'])}")
        lines.append(
            "相同检测值："
            + ("、".join(channel["sameValues"]) if channel["sameValues"] else "无")
        )
        lines.append(f"变化：{channel['change']:+d} 次")
        for stop in channel["stops"]:
            lines.append(
                f"停止：{stop['stoppedAt']}（最后确认串流；之后连续 3 轮未确认）")
            if stop.get("restartedAt"):
                lines.append(f"重新串流：{stop['restartedAt']}")
        if not channel["stops"]:
            lines.append("停止/重新串流：未判定（不足连续 3 轮）")
        lines.append("")
    lines.append("说明：只统计人工确认为真实串流的记录；误报不计入通道串流次数。")

    return {
        "date": date,
        "available": ready,
        "reason": reason,
        "accuracy": accuracy,
        "checked": checked,
        "confirmed": len(confirmed),
        "falsePositive": len(false_positive),
        "channels": channels,
        "text": "\n".join(lines).rstrip() + "\n",
    }


def round_csv(date: str, round_key: str) -> dict[str, str]:
    """CSV for one round, containing only human-confirmed rows."""
    today = today_str()
    if date > today:
        raise ValueError("不能查询未来日期")
    if date == today:
        groups, _meta = _live_groups(date)
    else:
        groups, _meta = _history_groups(date)
    confirmations = {
        row["row_key"]: row
        for row in confirmation_store.list_for_date(date)
    }
    confirmations = _auto_confirm_rows(date, groups, confirmations)
    group = next((item for item in groups if item.get("key") == round_key), None)
    if group is None:
        raise ValueError("该轮次不存在")
    confirmations = {
        key: row for key, row in confirmations.items()
        if row.get("result") in CONFIRM_RESULTS
    }
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "时间", "编号", "通道编码", "通道名称", "期望", "识别",
        "置信度", "算法状态", "确认结果", "确认时间",
    ])
    for row in sorted(
            group["rows"], key=lambda item: (item["time"], item["index"] or 0)):
        confirmation = confirmations.get(_row_id(row))
        if confirmation is None:
            continue
        writer.writerow([
            row["time"],
            row["index"] if row["index"] is not None else "",
            row["channelCode"],
            row["name"],
            row["expected"],
            row["detected"],
            "" if row["confidence"] is None else f"{row['confidence']:.3f}",
            row["status"],
            "确认" if confirmation["result"] == "confirmed" else "误报",
            confirmation.get("confirmed_at") or "",
        ])
    safe_key = re.sub(r"[^0-9A-Za-z_-]+", "_", round_key)
    return {
        "filename": f"osd_round_{date}_{safe_key}.csv",
        "content": output.getvalue(),
    }


def save_confirmation(
    row_key: str,
    result: str | None,
    *,
    source: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Create/update/clear one today's row confirmation."""
    result = (result or "").strip() or None
    if result is not None and result not in CONFIRM_RESULTS:
        raise ValueError("无效的确认结果")

    today = today_str()
    target: dict[str, Any] | None = None
    groups, _meta = _live_groups(today)
    for group in groups:
        for row in group["rows"]:
            if _row_id(row) == row_key:
                target = row
                break
        if target is not None:
            break
    if target is None:
        raise ValueError("只能确认当天的异常记录")
    if not (target.get("status") or "").upper().startswith("MISMATCH"):
        raise ValueError("只能确认 MISMATCH 记录")

    previous = confirmation_store.get(row_key)
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    if result is None:
        if previous is not None:
            confirmation_store.delete(row_key)
            confirmation_store.delete_auto_after(
                target["channelCode"], today, target["time"])
            confirmation_store.log_event(
                row_key=row_key, action="clear", at=now,
                confirm_date=today, channel_code=target["channelCode"],
                actor=actor, source=source,
            )
        return {
            "rowKey": row_key,
            "result": None,
            "confirmedAt": "",
        }

    record = confirmation_store.save(
        row_key=row_key,
        confirm_date=today,
        time=target["time"],
        channel_code=target["channelCode"],
        channel_name=target.get("name") or "",
        result=result,
        note="",
        confirmed_at=now,
        actor=actor,
        source=source,
    )
    confirmation_store.delete_auto_after(
        target["channelCode"], today, target["time"])
    confirmation_store.log_event(
        row_key=row_key,
        action="confirm",
        result=result,
        note="",
        at=now,
        confirm_date=today,
        channel_code=target["channelCode"],
        actor=actor,
        source=source,
    )
    return {
        "rowKey": row_key,
        "result": record.get("result") if record else result,
        "confirmedAt": record.get("confirmed_at") if record else now,
    }


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
    channel_name: str = "",
    order: str = "desc",
) -> dict[str, Any]:
    """Return one page of anomalies, grouped by detection round."""
    today = today_str()
    date = date or today
    channel_name = (channel_name or "").strip()
    is_today = date == today

    if date > today:
        raise ValueError("不能查询未来日期")
    if channel_name and not is_today:
        raise ValueError("通道名称搜索仅支持当天结果")

    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    order = "asc" if order == "asc" else "desc"

    if is_today:
        groups, meta = _live_groups(date)
    else:
        groups, meta = _history_groups(date)

    confirmations = {
        row["row_key"]: row
        for row in confirmation_store.list_for_date(date)
    }
    confirmations = _auto_confirm_rows(date, groups, confirmations)

    total = sum(len(group["rows"]) for group in groups)
    round_count = len(groups)

    if channel_name:
        needle = channel_name.casefold()
        filtered: list[dict[str, Any]] = []
        for group in groups:
            matched_rows = [
                row for row in group["rows"]
                if needle in (row["name"] or "").casefold()
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

            key = _row_id(row)
            confirmation = confirmations.get(key)
            row["rowKey"] = key
            row["confirmResult"] = (confirmation.get("result")
                                    if confirmation else None)
            row["confirmSource"] = (confirmation.get("source")
                                    if confirmation else None)
            row["confirmAt"] = (confirmation.get("confirmed_at", "")
                                if confirmation else "")
            row["confirmEditable"] = bool(
                is_today
                and (row["status"] or "").upper().startswith("MISMATCH"))

    return {
        "date": date,
        "today": today,
        "isToday": is_today,
        "search": channel_name or None,
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
