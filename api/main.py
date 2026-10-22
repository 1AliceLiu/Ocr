"""Read-only API for the OSD results written by the monitor.

Environment:
    OUTPUT_FILE        path to output.txt (default /work/output.txt)
    OSD_ERROR_FILE     path to output/osd_error.txt (default
                       /work/output/osd_error.txt)
    API_KEY            required X-API-Key value; empty disables auth
    MONITOR_INTERVAL   monitor interval in seconds, used for the stale check

Endpoints under /api/v1/osd-error* serve output/osd_error.txt (anomalies
only). The older /api/v1/errors* endpoints still serve output.txt.
"""
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

OUTPUT_FILE = Path(os.getenv("OUTPUT_FILE", "/work/output.txt"))
OSD_ERROR_FILE = Path(os.getenv("OSD_ERROR_FILE", "/work/output/osd_error.txt"))
API_KEY = os.getenv("API_KEY", "")
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
BEIJING_TZ = timezone(timedelta(hours=8))
PAGE_LIMIT = 5000          # default and max page size

app = FastAPI(
    title="OSD Mismatch API",
    version="1.1.0",
    description="Read-only access to output.txt and output/osd_error.txt "
                "written by evo_osd_monitor.py",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def read_errors() -> list[dict]:
    """Parse output.txt: <time>\\t<expected>\\t<detected>."""
    if not OUTPUT_FILE.exists():
        return []
    items = []
    for line in OUTPUT_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 3:
            continue
        items.append({
            "time": parts[0],
            "expected": parts[1],
            "detected": parts[2],
        })
    return items


def file_meta() -> dict:
    if not OUTPUT_FILE.exists():
        return {"exists": False, "size": 0, "updated_at": None, "age_seconds": None}
    stat = OUTPUT_FILE.stat()
    updated = datetime.fromtimestamp(stat.st_mtime, BEIJING_TZ)
    return {
        "exists": True,
        "size": stat.st_size,
        "updated_at": updated.strftime("%Y-%m-%d %H:%M:%S"),
        "age_seconds": int((now_beijing() - updated).total_seconds()),
    }


# ---------------------------------------------------------------------------
# output/osd_error.txt  (anomalies only, written by evo_osd_monitor.py)
#
#   # round 12 2026-10-20 15:41:00
#   2026-10-20 15:41:02  108  1002931$1$0$0  2411_1  2411  1411  0.504  MISMATCH
#   # round 12 done: 1200/1521 OK, 45 anomaly logged, 276 capture timeout skipped, 1382.4s
#
# Fields: time, index, channelCode, name, expected, detected, confidence, status
# ---------------------------------------------------------------------------

def osd_error_meta() -> dict:
    if not OSD_ERROR_FILE.exists():
        return {"file": str(OSD_ERROR_FILE), "exists": False, "size": 0,
                "updated_at": None, "age_seconds": None}
    stat = OSD_ERROR_FILE.stat()
    updated = datetime.fromtimestamp(stat.st_mtime, BEIJING_TZ)
    return {
        "file": str(OSD_ERROR_FILE),
        "exists": True,
        "size": stat.st_size,
        "updated_at": updated.strftime("%Y-%m-%d %H:%M:%S"),
        "age_seconds": int((now_beijing() - updated).total_seconds()),
    }


def read_osd_error() -> list[dict]:
    """Parse osd_error.txt, keeping the round number of each record."""
    if not OSD_ERROR_FILE.exists():
        return []
    items: list[dict] = []
    current_round: int | None = None
    text = OSD_ERROR_FILE.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("#"):
            match = re.match(r"#\s*round\s+(\d+)\b", line)
            if match:
                current_round = int(match.group(1))
            continue
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        try:
            confidence = float(parts[6])
        except ValueError:
            confidence = None
        items.append({
            "time": parts[0],
            "index": int(parts[1]) if parts[1].isdigit() else None,
            "channelCode": parts[2],
            "name": parts[3],
            "expected": parts[4],
            "detected": parts[5],
            "confidence": confidence,
            "status": parts[7],
            "round": current_round,
        })
    return items


def read_osd_rounds() -> list[dict]:
    """Round headers + 'done' footers from osd_error.txt."""
    if not OSD_ERROR_FILE.exists():
        return []
    rounds: dict[int, dict] = {}
    text = OSD_ERROR_FILE.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(
            r"#\s*round\s+(\d+)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if match:
            number = int(match.group(1))
            rounds.setdefault(number, {"round": number, "anomalies": 0})
            rounds[number]["started_at"] = match.group(2)
            continue
        match = re.match(
            r"#\s*round\s+(\d+)\s+done:\s+(\d+)/(\d+) OK,\s+(\d+) anomaly"
            r"(?: logged)?,\s+(?:(\d+) capture timeout skipped,\s+)?([\d.]+)s",
            line)
        if match:
            number = int(match.group(1))
            entry = rounds.setdefault(number, {"round": number, "anomalies": 0})
            entry.update({
                "ok": int(match.group(2)),
                "channels": int(match.group(3)),
                "anomalies": int(match.group(4)),
                "timeouts_skipped": (int(match.group(5))
                                     if match.group(5) is not None else None),
                "duration_seconds": float(match.group(6)),
            })
    return sorted(rounds.values(), key=lambda r: r["round"])


def paginate(items: list[dict], limit: int, offset: int, order: str) -> dict:
    """Sort + slice, and describe the page so a client can walk all records."""
    ordered = sorted(items, key=lambda i: i["time"], reverse=(order == "desc"))
    total = len(ordered)
    page_items = ordered[offset:offset + limit]
    total_pages = (total + limit - 1) // limit if total and limit else 0
    next_offset = offset + len(page_items)
    return {
        "count": len(page_items),
        "total_matched": total,
        "limit": limit,
        "offset": offset,
        "page": (offset // limit + 1) if limit else 1,
        "total_pages": total_pages,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "order": order,
        "items": page_items,
    }


@app.get("/health", summary="Liveness probe (no auth)")
def health() -> dict:
    return {"status": "ok", "time": now_beijing().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/v1/errors", dependencies=[Depends(require_key)],
         summary="Recent mismatches, newest first")
def errors(
    limit: int = Query(100, ge=1, le=1000),
    expected: str | None = Query(None, description="filter by expected code"),
    detected: str | None = Query(None, description="filter by detected code"),
    since: str | None = Query(None, description="time lower bound, e.g. 2026-10-20 00:00:00"),
) -> dict:
    all_items = read_errors()
    items = all_items
    if expected:
        items = [i for i in items if i["expected"] == expected]
    if detected:
        items = [i for i in items if i["detected"] == detected]
    if since:
        items = [i for i in items if i["time"] >= since]

    newest = list(reversed(items))[:limit]
    meta = file_meta()
    return {
        "count": len(newest),
        "total_matched": len(items),
        "total_errors": len(all_items),
        "updated_at": meta["updated_at"],
        "items": newest,
    }


@app.get("/api/v1/summary", dependencies=[Depends(require_key)],
         summary="Aggregate mismatches by expected code")
def summary() -> dict:
    all_items = read_errors()
    grouped: dict[str, dict] = {}
    for item in all_items:
        key = item["expected"]
        entry = grouped.setdefault(key, {
            "expected": key,
            "count": 0,
            "last_time": None,
            "detected": {},
        })
        entry["count"] += 1
        entry["last_time"] = item["time"]
        entry["detected"][item["detected"]] = entry["detected"].get(item["detected"], 0) + 1

    meta = file_meta()
    return {
        "total_errors": len(all_items),
        "updated_at": meta["updated_at"],
        "items": sorted(grouped.values(), key=lambda e: e["count"], reverse=True),
    }


@app.get("/api/v1/status", dependencies=[Depends(require_key)],
         summary="Monitor freshness and output.txt metadata")
def status() -> dict:
    meta = file_meta()
    total = len(read_errors())
    age = meta["age_seconds"]
    stale = age is None or age > MONITOR_INTERVAL * 3
    return {
        "status": "stale" if stale else "ok",
        "monitor_interval_seconds": MONITOR_INTERVAL,
        "output_file": str(OUTPUT_FILE),
        "total_errors": total,
        **meta,
    }


@app.get("/api/v1/osd-error", dependencies=[Depends(require_key)],
         summary="Anomaly records from osd_error.txt (newest first)")
def osd_error(
    limit: int = Query(PAGE_LIMIT, ge=1, le=PAGE_LIMIT,
                       description="page size, max 5000"),
    offset: int = Query(0, ge=0),
    status: str | None = Query(
        None, description="status prefix, e.g. OK / MISMATCH / UNCERTAIN / "
                          "NO_OSD / CAPTURE_FAILED"),
    channel_code: str | None = Query(
        None, description="channel code, substring match"),
    name: str | None = Query(None, description="channel name, substring match"),
    expected: str | None = Query(None, description="expected code, exact match"),
    round_no: int | None = Query(None, alias="round", description="round number"),
    since: str | None = Query(
        None, description="time >= this, e.g. 2026-10-20 00:00:00"),
    until: str | None = Query(
        None, description="time <= this, inclusive"),
    order: str = Query("desc", pattern="^(asc|desc)$",
                       description="sort by time"),
) -> dict:
    all_items = read_osd_error()
    items = all_items
    if status:
        wanted = status.upper()
        items = [i for i in items if i["status"].upper().startswith(wanted)]
    if channel_code:
        items = [i for i in items if channel_code in i["channelCode"]]
    if name:
        items = [i for i in items if name in i["name"]]
    if expected:
        items = [i for i in items if i["expected"] == expected]
    if round_no is not None:
        items = [i for i in items if i["round"] == round_no]
    if since:
        items = [i for i in items if i["time"] >= since]
    if until:
        items = [i for i in items if i["time"] <= until]

    page = paginate(items, limit, offset, order)
    meta = osd_error_meta()
    return {
        "total_records": len(all_items),
        "updated_at": meta["updated_at"],
        "age_seconds": meta["age_seconds"],
        **page,
    }


@app.get("/api/v1/osd-error/summary", dependencies=[Depends(require_key)],
         summary="Counts by status / channel and the latest rounds")
def osd_error_summary() -> dict:
    items = read_osd_error()
    rounds = read_osd_rounds()
    per_round = Counter(i["round"] for i in items if i["round"] is not None)
    for entry in rounds:
        entry["records_in_file"] = per_round.get(entry["round"], 0)
    by_status = Counter(i["status"].split("(")[0] for i in items)
    by_channel = Counter(i["channelCode"] for i in items)
    meta = osd_error_meta()
    return {
        "total": len(items),
        "by_status": dict(by_status.most_common()),
        "top_channels": [{"channelCode": code, "count": count}
                         for code, count in by_channel.most_common(10)],
        "latest_round": rounds[-1] if rounds else None,
        "recent_rounds": rounds[-5:],
        **meta,
    }


@app.get("/api/v1/osd-error/rounds", dependencies=[Depends(require_key)],
         summary="Round headers and 'done' summaries")
def osd_error_rounds() -> dict:
    rounds = read_osd_rounds()
    return {"count": len(rounds), "items": rounds}


@app.get("/api/v1/osd-error/channels/{channel_code}",
         dependencies=[Depends(require_key)],
         summary="Every anomaly of one channel")
def osd_error_channel(
    channel_code: str,
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    all_items = [i for i in read_osd_error() if i["channelCode"] == channel_code]
    if not all_items:
        raise HTTPException(status_code=404,
                            detail=f"no anomaly for channel {channel_code}")
    items = sorted(all_items, key=lambda i: i["time"], reverse=(order == "desc"))
    return {
        "channelCode": channel_code,
        "count": len(items[offset:offset + limit]),
        "total_matched": len(items),
        "items": items[offset:offset + limit],
    }


@app.get("/api/v1/osd-error/status", dependencies=[Depends(require_key)],
         summary="Freshness and size of osd_error.txt")
def osd_error_status() -> dict:
    items = read_osd_error()
    meta = osd_error_meta()
    age = meta["age_seconds"]
    stale = age is None or age > MONITOR_INTERVAL * 3
    return {
        "status": "stale" if stale else "ok",
        "stale_after_seconds": MONITOR_INTERVAL * 3,
        "total_records": len(items),
        **meta,
    }


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "name": "OSD Mismatch API",
        "docs": "/docs",
        "health": "/health",
        "output_txt": ["/api/v1/errors", "/api/v1/summary", "/api/v1/status"],
        "osd_error_txt": ["/api/v1/osd-error", "/api/v1/osd-error/summary",
                          "/api/v1/osd-error/rounds",
                          "/api/v1/osd-error/channels/{channelCode}",
                          "/api/v1/osd-error/status"],
    }
