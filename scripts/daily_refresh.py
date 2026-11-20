"""Daily refresh for the old OSD platform and the HK03 platform.

Old platform (default 06:30):
  pull out/streams.txt, out/devices.csv and out/channels.csv, then restart
  osd20 so the monitor reads the new list.

HK03 platform (default 06:00):
  pull out-hk03/streams.txt, out-hk03/devices.csv and out-hk03/channels.csv.
  No restart is needed; also drop HK03 frames captured on earlier dates.

Both jobs run in the same osd-daily container. A failed pull keeps the
current files.

Usage:
    python scripts/daily_refresh.py --watch          # old 06:30 + HK03 06:00
    python scripts/daily_refresh.py --once           # old platform now
    python scripts/daily_refresh.py --once-hk03      # HK03 now
    python scripts/daily_refresh.py --once --dry-run # pull + validate only
    python scripts/daily_refresh.py --once --no-restart
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from prune_osd_error import archive_rows, plan_prune

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BEIJING_TZ = timezone(timedelta(hours=8))
STREAMS_NAME = "streams.txt"
EXTRA_FILES = ("devices.csv", "channels.csv")
DEFAULT_OSD_ERROR = PROJECT_ROOT / "output" / "osd_error.txt"
DEFAULT_ARCHIVE_DIR = PROJECT_ROOT / "Logs"
HK03_FILES = ("streams.txt", "devices.csv", "channels.csv")
SNAPSHOT_DIR = PROJECT_ROOT / "snapshots" / "osd_error"
SOURCE_FRAME_DIR = PROJECT_ROOT / "temp" / "hk03" / "frames"


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def log(message: str) -> None:
    print(f"{now_beijing():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def count_rows(path: Path) -> int:
    if not path.exists():
        return 0
    rows = 0
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            if len(line.rstrip("\r\n").split("\t")) >= 8:
                rows += 1
    return rows


def run_pull(outdir: Path, extra: list[str]) -> int:
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "evo_pull.py"),
           "--outdir", str(outdir), "--with-streams",
           "--stream-mode", "template", *extra]
    log("pull: " + " ".join(cmd))
    return subprocess.run(cmd).returncode


def atomic_replace(fresh: Path, target: Path) -> None:
    """Copy to a temp file next to the target, then rename it into place."""
    tmp_target = target.with_name(target.name + ".tmp")
    shutil.copyfile(fresh, tmp_target)
    os.replace(tmp_target, target)


def run_hk03_pull(outdir: Path, args) -> int:
    cmd = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "evo_pull.py"),
        "--base", args.hk03_base, "--user", args.hk03_user,
        "--password", args.hk03_password, "--outdir", str(outdir),
        "--with-streams", "--stream-mode", "template", "--delay", "0.05",
    ]
    log(f"hk03 pull: {args.hk03_base} -> {outdir}")
    return subprocess.run(cmd).returncode


def cleanup_old_hk03_frames(today: str) -> int:
    """Drop HK03 frames from earlier dates; old-platform frames are pruned
    separately by osd-error-prune."""
    deleted = 0
    if SNAPSHOT_DIR.is_dir():
        for folder in SNAPSHOT_DIR.glob("round_*"):
            if not folder.is_dir():
                continue
            match = re.match(r"round_\d+_(\d{8})_\d{6}$", folder.name)
            folder_date = match.group(1) if match else ""
            for path in folder.glob("hk03_*.jpg"):
                try:
                    captured = datetime.fromtimestamp(
                        path.stat().st_mtime, BEIJING_TZ).strftime("%Y-%m-%d")
                    if captured >= today:
                        continue
                    path.unlink()
                    deleted += 1
                except OSError:
                    continue
            try:
                # Keep today's empty folder; it is the current round.
                if (folder_date and folder_date < today.replace("-", "")
                        and not any(folder.iterdir())):
                    folder.rmdir()
            except OSError:
                pass
    if SOURCE_FRAME_DIR.is_dir():
        for path in SOURCE_FRAME_DIR.glob("*.jpg"):
            try:
                captured = datetime.fromtimestamp(
                    path.stat().st_mtime, BEIJING_TZ).strftime("%Y-%m-%d")
                if captured < today:
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
    return deleted


def refresh_hk03(args) -> bool:
    target_dir = Path(args.hk03_outdir)
    target = target_dir / STREAMS_NAME
    staging = Path(tempfile.mkdtemp(prefix="hk03-refresh-"))
    try:
        rc = run_hk03_pull(staging, args)
        fresh = staging / STREAMS_NAME
        if rc != 0 or not fresh.exists():
            log(f"HK03 pull failed (rc={rc}); keeping current {target}")
            return False
        new_rows = count_rows(fresh)
        old_rows = count_rows(target)
        log(f"HK03 rows: new={new_rows} current={old_rows}")
        if new_rows < args.min_rows:
            log(f"HK03 refusing: fewer than --min-rows={args.min_rows}")
            return False
        if old_rows and new_rows < old_rows * args.min_ratio:
            log(f"HK03 refusing: new list is smaller than "
                f"{args.min_ratio:.0%} of the current one")
            return False
        if args.dry_run:
            log(f"HK03 dry-run: would replace {target} and update "
                f"{', '.join(HK03_FILES[1:])}")
            return True

        for name in HK03_FILES:
            fresh_file = staging / name
            if not fresh_file.exists() or fresh_file.stat().st_size == 0:
                log(f"HK03 missing or empty {name}; keeping current file")
                continue
            target_file = target_dir / name
            try:
                atomic_replace(fresh_file, target_file)
            except OSError as exc:
                log(f"HK03 replace {target_file} failed: "
                    f"{type(exc).__name__}: {exc}")
                continue
            log(f"HK03 replaced {target_file}")
        removed = cleanup_old_hk03_frames(
            now_beijing().strftime("%Y-%m-%d"))
        if removed:
            log(f"HK03 removed {removed} frames from previous dates")
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# Docker API over the unix socket (no docker CLI in the image)
# --------------------------------------------------------------------------

def _decode_chunked(data: bytes) -> bytes:
    """Decode an HTTP/1.1 chunked body (Docker returns chunked JSON)."""
    out = bytearray()
    while True:
        line, sep, rest = data.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        data = rest[size + 2:]  # skip the chunk's trailing CRLF
    return bytes(out)


def docker_request(sock_path: str, method: str, path: str,
                   body: bytes = b"") -> tuple[int, bytes]:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(30)
    conn.connect(sock_path)
    head = (f"{method} {path} HTTP/1.1\r\nHost: docker\r\n"
            f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n")
    conn.sendall(head.encode() + body)
    data = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    conn.close()
    header, _, payload = data.partition(b"\r\n\r\n")
    try:
        status = int(header.split(b" ")[1])
    except (IndexError, ValueError):
        status = 0
    headers = {}
    for line in header.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        headers[key.strip().lower()] = value.strip().lower()
    if headers.get(b"transfer-encoding") == b"chunked":
        payload = _decode_chunked(payload)
    return status, payload


def find_service_container(sock_path: str, service: str) -> str | None:
    filters = json.dumps({"label": [f"com.docker.compose.service={service}"]})
    status, payload = docker_request(
        sock_path, "GET", "/containers/json?all=1&filters=" + quote(filters))
    if status != 200:
        log(f"docker: cannot list containers (HTTP {status})")
        return None
    try:
        containers = json.loads(payload or b"[]")
    except json.JSONDecodeError:
        return None
    if not containers:
        return None
    return containers[0].get("Id")


def restart_service(sock_path: str, service: str, wait_seconds: int = 10) -> bool:
    container_id = find_service_container(sock_path, service)
    if not container_id:
        log(f"docker: no running container for service {service!r}")
        return False
    status, payload = docker_request(
        sock_path, "POST",
        f"/containers/{container_id}/restart?t={wait_seconds}")
    if status in (204, 304):
        log(f"docker: restarted {service} ({container_id[:12]})")
        return True
    log(f"docker: restart failed HTTP {status}: "
        f"{(payload or b'').decode('utf-8', 'replace')[:200]}")
    return False


def stop_service(sock_path: str, service: str, wait_seconds: int = 10) -> bool:
    container_id = find_service_container(sock_path, service)
    if not container_id:
        log(f"docker: no running container for service {service!r}")
        return False
    status, payload = docker_request(
        sock_path, "POST",
        f"/containers/{container_id}/stop?t={wait_seconds}")
    if status in (204, 304):
        log(f"docker: stopped {service} ({container_id[:12]})")
        return True
    log(f"docker: stop failed HTTP {status}: "
        f"{(payload or b'').decode('utf-8', 'replace')[:200]}")
    return False


def start_service(sock_path: str, service: str) -> bool:
    container_id = find_service_container(sock_path, service)
    if not container_id:
        log(f"docker: no container for service {service!r}")
        return False
    status, payload = docker_request(
        sock_path, "POST", f"/containers/{container_id}/start")
    if status in (204, 304):
        log(f"docker: started {service} ({container_id[:12]})")
        return True
    log(f"docker: start failed HTTP {status}: "
        f"{(payload or b'').decode('utf-8', 'replace')[:200]}")
    return False


def run_hk03_suppress(args, action: str) -> bool:
    if action == "check":
        table = Path(args.hk03_outdir) / "channels.csv"
        try:
            table_date = datetime.fromtimestamp(
                table.stat().st_mtime, BEIJING_TZ).strftime("%Y-%m-%d")
        except OSError:
            table_date = ""
        if table_date != now_beijing().strftime("%Y-%m-%d"):
            log(f"HK03 suppression: {table} was not refreshed today; skipped")
            return False
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "daily_hk03_suppress.py"),
        action,
        "--base", args.hk03_base,
        "--user", args.hk03_user,
        "--password", args.hk03_password,
    ]
    log(f"HK03 suppression: {action}")
    return subprocess.run(cmd).returncode == 0


# --------------------------------------------------------------------------
# osd_error archive
# --------------------------------------------------------------------------

def archive_old_osd_error(args) -> Path | None:
    """Archive the rows that prune would delete, without deleting them.

    osd-error-prune owns deletion, but osd-daily also runs once per day; doing
    the same archive here is a safety net if the prune service was unavailable.
    The archive helper de-duplicates rows, so a later prune does not duplicate
    them in the CSV.
    """
    if not getattr(args, "archive", True):
        log("archive: disabled")
        return None
    path = Path(args.osd_error_file)
    _kept, _dropped, dropped_rows = plan_prune(path, keep_days=1)
    if not dropped_rows:
        log(f"archive: no old rows to archive in {path}")
        return None
    try:
        archive_path, count = archive_rows(
            dropped_rows, Path(args.archive_dir),
            day=now_beijing().strftime("%Y%m%d"))
    except Exception as exc:
        # Archiving must not block the streams refresh; prune will retry.
        log(f"archive failed: {type(exc).__name__}: {exc}")
        return None
    log(f"archive: {count} rows -> {archive_path}")
    return archive_path


# --------------------------------------------------------------------------

def refresh(args) -> bool:
    if not args.dry_run:
        archive_old_osd_error(args)
    target_dir = Path(args.outdir)
    target = target_dir / STREAMS_NAME
    # Pull into the system temp dir (not next to out/) so no extra folder is
    # ever created in the project; the only visible changes are the three
    # files inside out/, replaced atomically below.
    staging = Path(tempfile.mkdtemp(prefix="osd-refresh-"))

    ok = False
    try:
        rc = run_pull(staging, args.pull_args)
        fresh = staging / STREAMS_NAME
        if rc != 0 or not fresh.exists():
            log(f"pull failed (rc={rc}); keeping the current {target}")
            return False

        new_rows = count_rows(fresh)
        old_rows = count_rows(target)
        log(f"rows: new={new_rows} current={old_rows}")
        if new_rows < args.min_rows:
            log(f"refusing: fewer than --min-rows={args.min_rows}")
            return False
        if old_rows and new_rows < old_rows * args.min_ratio:
            log(f"refusing: new list is smaller than {args.min_ratio:.0%} "
                f"of the current one")
            return False

        if args.dry_run:
            log(f"dry-run: would replace {target} with {new_rows} rows "
                f"and update {', '.join(EXTRA_FILES)}")
            return True

        # atomic swap: the monitor only reads the file at startup, but a
        # half-written list must never be visible
        atomic_replace(fresh, target)
        log(f"replaced {target} ({new_rows} rows)")

        for name in EXTRA_FILES:
            fresh_extra = staging / name
            if not fresh_extra.exists() or fresh_extra.stat().st_size == 0:
                # devices/channels are auxiliary; never block the streams swap
                log(f"missing or empty {name} in pull output; keeping current file")
                continue
            extra_target = target_dir / name
            try:
                atomic_replace(fresh_extra, extra_target)
            except OSError as exc:
                # streams.txt is already swapped; keep going so osd20 restarts
                log(f"replace {extra_target} failed: {type(exc).__name__}: {exc}")
                continue
            log(f"replaced {extra_target}")
        ok = True
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if ok and args.restart:
        if args.dry_run:
            log("dry-run: would restart osd20")
        elif args.hk03_suppress and not args.no_hk03:
            # The check runs while osd20 is still alive (it only reads the
            # tables).  Stop it briefly only to rewrite osd_error.txt safely.
            check_ok = run_hk03_suppress(args, "check")
            if not check_ok:
                log("HK03 suppression check failed; restarting osd20 without applying")
                restart_service(args.docker_sock, args.service)
                return True
            stopped = stop_service(args.docker_sock, args.service)
            try:
                if stopped:
                    run_hk03_suppress(args, "apply")
                else:
                    log("HK03 suppression: osd20 stop failed; apply skipped")
            finally:
                if stopped:
                    start_service(args.docker_sock, args.service)
        else:
            restart_service(args.docker_sock, args.service)
    return ok


def seconds_until(hhmm: str) -> float:
    hour, minute = (int(part) for part in hhmm.split(":"))
    now = now_beijing()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def watch(args) -> None:
    while True:
        events = [("old", seconds_until(args.time))]
        if not args.no_hk03:
            events.append(("hk03", seconds_until(args.hk03_time)))
        events.sort(key=lambda item: item[1])
        kind, wait = events[0]
        if (len(events) > 1 and abs(events[0][1] - events[1][1]) < 1):
            kind = "both"
        log(f"next refresh: {kind} (old {args.time}, HK03 "
            f"{args.hk03_time}) in {wait / 3600:.2f} h")
        time.sleep(wait)
        try:
            if kind in ("hk03", "both"):
                refresh_hk03(args)
            if kind in ("old", "both"):
                refresh(args)
        except Exception as exc:  # one bad run must not kill the schedule
            log(f"refresh failed: {type(exc).__name__}: {exc}")
        time.sleep(60)  # avoid firing twice within the same minute


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outdir", default=os.getenv("STREAMS_DIR", "/work/out"),
                        help="directory holding streams.txt (default /work/out)")
    parser.add_argument("--time", default="06:30",
                        help="old platform refresh time, Beijing (default 06:30)")
    parser.add_argument("--hk03-outdir",
                        default=os.getenv("HK03_OUTDIR", "/work/out-hk03"),
                        help="HK03 directory (default /work/out-hk03)")
    parser.add_argument("--hk03-time",
                        default=os.getenv("HK03_REFRESH_TIME", "06:00"),
                        help="HK03 refresh time, Beijing (default 06:00)")
    parser.add_argument("--hk03-base",
                        default=os.getenv("HK03_BASE", ""),
                        help="HK03 platform base URL (or HK03_BASE)")
    parser.add_argument("--hk03-user", default=os.getenv("HK03_USER", ""))
    parser.add_argument("--hk03-password", default=os.getenv("HK03_PASSWORD", ""),
                        help="prefer HK03_PASSWORD in the environment")
    parser.add_argument("--no-hk03", action="store_true",
                        help="disable the HK03 job in --watch mode")
    parser.add_argument("--once-hk03", action="store_true",
                        help="refresh HK03 now and exit")
    parser.add_argument("--watch", action="store_true",
                        help="run forever, refresh once a day at --time")
    parser.add_argument("--once", action="store_true",
                        help="refresh now and exit (default without --watch)")
    parser.add_argument("--dry-run", action="store_true",
                        help="pull and validate, but do not replace/restart")
    parser.add_argument("--no-restart", action="store_true",
                        help="update streams.txt only, leave osd20 alone")
    parser.add_argument("--no-hk03-suppress", action="store_true",
                        help="skip the daily old/HK03 intersection suppression job")
    parser.add_argument("--min-rows", type=int, default=100,
                        help="refuse a list with fewer rows (default 100)")
    parser.add_argument("--min-ratio", type=float, default=0.5,
                        help="refuse a list smaller than this fraction of the "
                             "current one (default 0.5)")
    parser.add_argument("--osd-error-file", type=Path, default=DEFAULT_OSD_ERROR,
                        help="osd_error.txt to inspect for old rows")
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR,
                        help=f"archive directory (default: {DEFAULT_ARCHIVE_DIR})")
    parser.add_argument("--no-archive", action="store_true",
                        help="do not archive old osd_error rows")
    parser.add_argument("--docker-sock", default="/var/run/docker.sock")
    parser.add_argument("--service", default="osd20",
                        help="compose service to restart (default osd20)")
    parser.add_argument("pull_args", nargs=argparse.REMAINDER,
                        help="extra args passed to evo_pull.py (after --)")
    args = parser.parse_args()

    args.restart = not args.no_restart
    args.archive = not args.no_archive
    args.hk03_suppress = not args.no_hk03_suppress
    try:
        for value in ((args.time,) if args.no_hk03
                      else (args.time, args.hk03_time)):
            hour, minute = (int(part) for part in value.split(":"))
            assert 0 <= hour < 24 and 0 <= minute < 60
    except (ValueError, AssertionError):
        parser.error("--time/--hk03-time must be HH:MM")

    if args.once_hk03:
        sys.exit(0 if refresh_hk03(args) else 1)
    if args.watch:
        watch(args)
        return

    sys.exit(0 if refresh(args) else 1)


if __name__ == "__main__":
    main()
