"""Read-only API for the OSD mismatch results in output.txt.

Environment:
    OUTPUT_FILE        path to output.txt (default /work/output.txt)
    API_KEY            required X-API-Key value; empty disables auth
    MONITOR_INTERVAL   monitor interval in seconds, used for the stale check
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

OUTPUT_FILE = Path(os.getenv("OUTPUT_FILE", "/work/output.txt"))
API_KEY = os.getenv("API_KEY", "")
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
BEIJING_TZ = timezone(timedelta(hours=8))

app = FastAPI(
    title="OSD Mismatch API",
    version="1.0.0",
    description="Read-only access to the mismatch results written by rtsp_osd_monitor.py",
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


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"name": "OSD Mismatch API", "docs": "/docs", "health": "/health"}
