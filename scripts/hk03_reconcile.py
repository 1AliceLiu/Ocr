"""Verify today's old-platform OSD alerts against HK03 camera frames.

The HK03 platform aggregates the same cameras under two NVRs (EVS1/EVS2).
A camera is identified by the old ``设备名称`` (e.g. 024) and the new
``通道名称`` (also 024), so the mapping is:

    old channel.deviceName == new channel.channelName

Two commands:

    python scripts/hk03_reconcile.py prepare
        Read today's device-name mismatches from output/osd_error.txt, map
        them to HK03 channels, and write temp/hk03/alert_channels.txt for
        evo_osd_monitor.py.

    python scripts/hk03_reconcile.py apply
        Read temp/hk03/new_rounds.csv and decide which old alerts can be
        cancelled.  Without --write it only writes the decision CSVs.

Cancellation rule:
  * old OSD already equals the old device name -> not an alert;
  * otherwise, if the HK03 frame reads its own channel name (and not
    another HK03 channel name), the old alert is cancelled;
  * if HK03 also reads a wrong value, the old alert stays and both frames
    are shown.

Only today's rows are touched.  With --write the matching rows are removed
from output/osd_error.txt and their dashboard confirmations are deleted.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from channel_filter import is_blocked, load_blocked_prefixes

ROOT = Path(__file__).resolve().parents[1]
OLD_CHANNELS = ROOT / "out" / "channels.csv"
OLD_ERROR = ROOT / "output" / "osd_error.txt"
NEW_DIR = ROOT / "out-hk03"
NEW_CHANNELS = NEW_DIR / "channels.csv"
NEW_STREAMS = NEW_DIR / "streams.txt"
WORK_DIR = ROOT / "temp" / "hk03"
FRAME_SOURCE_DIR = WORK_DIR / "frames"
COMPARE_DIR = WORK_DIR / "compare"
BACKUP_DIR = WORK_DIR / "backup"
SUBSET = WORK_DIR / "alert_channels.txt"
NEW_CSV = WORK_DIR / "new_rounds.csv"
SNAPSHOT_DIR = ROOT / "snapshots" / "osd_error"
DB_PATH = ROOT / "state" / "dashboard.db"
SUPPRESS_FILE = ROOT / "state" / "hk03_cancelled.json"
BLOCKED_FILE = ROOT / "config" / "blocked_channels.txt"
BEIJING = timezone(timedelta(hours=8))

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
NON_ALNUM = re.compile(r"[^0-9A-Za-z]")
SAFE = re.compile(r"[^0-9A-Za-z@_-]+")
ROUND_HEADER_RE = re.compile(
    r"^#\s*round\s+(\d+)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
ROUND_DONE_RE = re.compile(
    r"^#\s*round\s+(\d+)\s+done:\s+(\d+)/(\d+) OK,\s+(\d+) anomaly"
    r"( logged)?,\s+(?:(\d+) capture timeout skipped,\s+)?([\d.]+)s")


def today_str() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d")


def now_text() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def canonical(text: str) -> str:
    return NON_ALNUM.sub("", text or "").upper()


def safe_name(index: int | str, code: str) -> str:
    return f"{int(index):04d}_{SAFE.sub('_', code)}.jpg"


def round_folder(round_no: int | str | None,
                  started_at: str | None) -> str | None:
    if not round_no or not started_at:
        return None
    stamp = started_at.replace("-", "").replace(":", "").replace(" ", "_")
    return f"round_{int(round_no):04d}_{stamp}"


def cleanup_old_hk03_frames(today: str) -> int:
    """Drop HK03 frames captured on earlier dates, keeping round folders."""
    deleted = 0
    if not SNAPSHOT_DIR.is_dir():
        return 0
    for folder in SNAPSHOT_DIR.glob("round_*"):
        if not folder.is_dir():
            continue
        match = re.match(r"round_\d+_(\d{8})_\d{6}$", folder.name)
        folder_date = match.group(1) if match else ""
        for path in folder.glob("hk03_*.jpg"):
            try:
                captured = datetime.fromtimestamp(
                    path.stat().st_mtime, BEIJING).strftime("%Y-%m-%d")
                if captured >= today:
                    continue
                path.unlink()
                deleted += 1
            except OSError:
                continue
        try:
            # Never remove today's empty folder: the monitor creates it at
            # round start and writes anomaly frames into it later.
            if (folder_date and folder_date < today.replace("-", "")
                    and not any(folder.iterdir())):
                folder.rmdir()
        except OSError:
            pass
    if FRAME_SOURCE_DIR.is_dir():
        for path in FRAME_SOURCE_DIR.glob("*.jpg"):
            try:
                captured = datetime.fromtimestamp(
                    path.stat().st_mtime, BEIJING).strftime("%Y-%m-%d")
                if captured < today:
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
    return deleted


def label_matches_name(label: str, name: str) -> bool:
    """Does an OSD label identify this camera name?

    Exact numbers only: '1' must not match '2671'.  Leading zeros and
    separators are ignored, so '024' == '24' and 'IPC-048' == 'IPC048'.
    """
    d = (label or "").strip()
    n = (name or "").strip()
    if not d or not n:
        return False
    if d == n:
        return True
    cd, cn = canonical(d), canonical(n)
    if cd and cn and cd == cn:
        return True
    if d.isdigit() and n.isdigit():
        return int(d) == int(n)
    if n.isdigit() and re.match(rf"^{re.escape(n)}(?:[\s\-_/]|{CJK.pattern})", d):
        return True
    return False


def load_channels(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("通道编码") or "").strip()
            if not code:
                continue
            rows[code] = {
                "code": code,
                "channelName": (row.get("通道名称") or "").strip(),
                "deviceName": (row.get("设备名称") or "").strip(),
                "deviceCode": (row.get("设备编码") or "").strip(),
            }
    return rows


def load_streams(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            rows[parts[1]] = {
                "index": parts[0],
                "channelCode": parts[1],
                "deviceCode": parts[2],
                "channelName": parts[3],
                "seq": parts[4],
                "org": parts[5],
                "status": parts[6],
                "url": parts[7],
            }
    return rows


def read_alerts(date: str) -> list[dict]:
    """Today's MISMATCH rows, with round number and original line."""
    alerts: list[dict] = []
    if not OLD_ERROR.exists():
        return alerts
    round_no = None
    round_started = None
    for line_no, raw in enumerate(
            OLD_ERROR.read_text(encoding="utf-8", errors="replace").splitlines(),
            1):
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("#"):
            header = ROUND_HEADER_RE.match(line)
            if header:
                round_no = int(header.group(1))
                round_started = header.group(2)
            continue
        parts = line.split("\t")
        if len(parts) < 8 or parts[0][:10] != date:
            continue
        if not parts[7].upper().startswith("MISMATCH"):
            continue
        alerts.append({
            "lineNo": line_no,
            "round": round_no,
            "roundStartedAt": round_started,
            "raw": line,
            "time": parts[0],
            "index": parts[1],
            "channelCode": parts[2],
            "channelName": parts[3],
            "detected": parts[5],
            "status": parts[7],
        })
    return alerts


def read_active_alerts(date: str) -> list[dict]:
    alerts = read_alerts(date)
    blocked = load_blocked_prefixes(BLOCKED_FILE)
    if not blocked:
        return alerts
    return [
        alert for alert in alerts
        if not is_blocked(alert["channelCode"], blocked)
    ]


def name_index(channels: dict[str, dict[str, str]],
               field: str) -> tuple[dict, dict, dict]:
    """canonical / integer / exact indexes for one name field."""
    by_canon: dict[str, list[str]] = defaultdict(list)
    by_int: dict[int, list[str]] = defaultdict(list)
    by_exact: dict[str, list[str]] = defaultdict(list)
    for code, row in channels.items():
        name = row[field]
        if not name:
            continue
        by_exact[name].append(code)
        canon = canonical(name)
        if canon:
            by_canon[canon].append(code)
        if name.isdigit():
            by_int[int(name)].append(code)
    return by_canon, by_int, by_exact


def matching_codes(label: str, channels: dict[str, dict[str, str]],
                   by_canon: dict, by_int: dict, by_exact: dict,
                   field: str) -> list[str]:
    hits: list[str] = []
    d = (label or "").strip()
    if not d:
        return hits
    canon = canonical(d)
    if canon:
        hits.extend(by_canon.get(canon, []))
    if d.isdigit():
        hits.extend(by_int.get(int(d), []))
    hits.extend(by_exact.get(d, []))
    # Deduplicate while keeping channels whose name really matches.
    result = []
    seen = set()
    for code in hits:
        if code in seen:
            continue
        seen.add(code)
        row = channels.get(code)
        if row and label_matches_name(d, row[field]):
            result.append(code)
    return result


def old_crossover(alert: dict, channels: dict[str, dict[str, str]],
                  index) -> dict | None:
    row = channels.get(alert["channelCode"])
    if not row:
        return None
    by_canon, by_int, by_exact = index
    for code in matching_codes(alert["detected"], channels, by_canon,
                               by_int, by_exact, "deviceName"):
        other = channels[code]
        # Same device (multi-lens) or the same duplicated camera name is not
        # evidence of a crossover; only a different name is.
        if (other["deviceCode"] != row["deviceCode"]
                and other["deviceName"] != row["deviceName"]):
            return {"channelCode": code, "deviceName": other["deviceName"]}
    return None


def new_crossover(label: str, own_code: str,
                  channels: dict[str, dict[str, str]], index) -> dict | None:
    by_canon, by_int, by_exact = index
    own_name = channels.get(own_code, {}).get("channelName", "")
    for code in matching_codes(label, channels, by_canon, by_int,
                               by_exact, "channelName"):
        other = channels[code]
        if code != own_code and other["channelName"] != own_name:
            return {"channelCode": code,
                    "channelName": other["channelName"]}
    return None


def new_match(old: dict, new_channels: dict[str, dict[str, str]],
              by_name: dict[str, list[str]]) -> tuple[str | None, str]:
    """Map by old deviceName == new channelName.

    A duplicated channel name is disambiguated with the old channel name
    (e.g. ``2438-全景`` belongs to camera 2438); if that still leaves more
    than one candidate the row is reported as ambiguous and is not cancelled.
    """
    candidates = list(by_name.get(old["deviceName"], []))
    if len(candidates) > 1:
        stem = re.sub(r"_\d+$", "", old["channelName"])
        narrowed = [
            code for code in candidates
            if (new_channels[code]["channelName"] in stem
                or stem.startswith(new_channels[code]["channelName"]))
        ]
        if narrowed:
            candidates = narrowed
    if len(candidates) == 1:
        return candidates[0], "ok"
    if len(candidates) > 1:
        return None, "ambiguous"
    return None, "not_found"


def write_csv(path: Path, rows: list[dict], fields: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in fields])
        for row in rows:
            writer.writerow([row.get(key, "") for key, _ in fields])
    print(f"wrote {path} ({len(rows)} rows)")


def prepare() -> None:
    date = today_str()
    removed = cleanup_old_hk03_frames(date)
    if removed:
        print(f"removed {removed} HK03 frames from previous dates")
    old_channels = load_channels(OLD_CHANNELS)
    new_channels = load_channels(NEW_CHANNELS)
    new_streams = load_streams(NEW_STREAMS)
    alerts = read_active_alerts(date)
    if not alerts:
        raise SystemExit(f"no {date} MISMATCH rows in {OLD_ERROR}")

    old_index = name_index(old_channels, "deviceName")
    by_new_name: dict[str, list[str]] = defaultdict(list)
    for code, row in new_channels.items():
        by_new_name[row["channelName"]].append(code)

    prepared: list[dict] = []
    subset: dict[str, dict] = {}
    for alert in alerts:
        row = old_channels.get(alert["channelCode"])
        if not row:
            continue
        cross = old_crossover(alert, old_channels, old_index)
        new_code, map_status = new_match(row, new_channels, by_new_name)
        item = {
            "time": alert["time"],
            "index": alert["index"],
            "channelCode": alert["channelCode"],
            "channelName": row["channelName"],
            "deviceName": row["deviceName"],
            "oldDetected": alert["detected"],
            "oldCrossover": "是" if cross else "",
            "oldMatchedDevice": cross["deviceName"] if cross else "",
            "newChannelCode": new_code or "",
            "newChannelName": (new_channels[new_code]["channelName"]
                               if new_code else ""),
            "newDeviceName": (new_channels[new_code]["deviceName"]
                              if new_code else ""),
            "mapStatus": map_status,
            "round": alert["round"] or "",
            "roundStartedAt": alert.get("roundStartedAt") or "",
            "raw": alert["raw"],
        }
        prepared.append(item)
        if not new_code:
            continue
        stream = new_streams.get(new_code)
        if not stream or not stream["url"] or "失败" in stream["status"]:
            item["mapStatus"] = "new_offline"
            continue
        if new_code not in subset:
            subset[new_code] = {
                **stream,
                "index": alert["index"],
            }

    write_csv(
        COMPARE_DIR / "active_alerts.csv", prepared,
        [("time", "时间"), ("index", "编号"), ("channelCode", "旧通道编码"),
         ("channelName", "旧通道名称"), ("deviceName", "旧设备名称"),
         ("oldDetected", "旧识别"), ("oldCrossover", "旧串流"),
         ("oldMatchedDevice", "命中的旧设备"), ("newChannelCode", "新通道编码"),
         ("newChannelName", "新通道名称"), ("newDeviceName", "新设备名称"),
         ("mapStatus", "映射状态"), ("round", "轮次")])

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with SUBSET.open("w", encoding="utf-8", newline="") as handle:
        handle.write("# HK03 channels for today's active old-platform alerts\n")
        handle.write("# 编号\t通道编码\t设备编码\t通道名称\t通道序号\t组织\t状态\tRTSP地址\n")
        for new_code, stream in sorted(subset.items()):
            handle.write("\t".join([
                str(stream["index"]), new_code, stream["deviceCode"],
                stream["channelName"], stream["seq"], stream["org"],
                stream["status"], stream["url"],
            ]) + "\n")
    print(f"wrote {SUBSET} ({len(subset)} channels)")

    print(f"active alerts: {len(prepared)}, "
          f"old crossover: {sum(1 for r in prepared if r['oldCrossover'])}, "
          f"mapped: {sum(1 for r in prepared if r['newChannelCode'])}, "
          f"subset: {len(subset)}")


def read_new_results(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path.exists():
        raise SystemExit(f"missing {path}; run the HK03 OCR first")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("通道编码") or "").strip()
            if code:
                rows[code] = row
    return rows


def apply(write: bool) -> None:
    date = today_str()
    old_channels = load_channels(OLD_CHANNELS)
    new_channels = load_channels(NEW_CHANNELS)
    alerts = read_active_alerts(date)
    if not alerts:
        raise SystemExit(f"no {date} MISMATCH rows in {OLD_ERROR}")
    old_index = name_index(old_channels, "deviceName")
    new_index = name_index(new_channels, "channelName")
    by_new_name: dict[str, list[str]] = defaultdict(list)
    for code, row in new_channels.items():
        by_new_name[row["channelName"]].append(code)
    results = read_new_results(NEW_CSV)

    decisions: list[dict] = []
    cancelled: list[dict] = []
    for alert in alerts:
        row = old_channels.get(alert["channelCode"])
        if not row:
            continue
        cross = old_crossover(alert, old_channels, old_index)
        new_code, map_status = new_match(row, new_channels, by_new_name)
        new_row = results.get(new_code) if new_code else None
        detected = (new_row.get("识别") or "").strip() if new_row else ""
        status = (new_row.get("状态") or "").strip() if new_row else ""
        reason = ""
        cancel = False
        old_is_alert = not label_matches_name(
            alert["detected"], row["deviceName"])
        if not old_is_alert:
            # The old OSD already equals its device name: there is no alert
            # to verify, even if the monitor compared against channel name.
            cancel = True
            reason = "旧平台OSD等于设备名称，本来就不应报警"
        elif not new_code:
            reason = f"新平台未匹配({map_status})"
        elif new_row is None:
            reason = "新平台无抓帧结果"
        elif not detected or status.startswith(
                ("CAPTURE_FAILED", "ERROR", "UNCERTAIN", "NO_OSD")):
            reason = f"新平台未识别({status or 'EMPTY'})"
        else:
            new_cross = new_crossover(
                detected, new_code, new_channels, new_index)
            own = label_matches_name(
                detected, new_channels[new_code]["channelName"])
            if new_cross:
                reason = f"新平台也串流({new_cross['channelName']})"
            elif not own:
                reason = f"新平台OSD不等于本通道({detected})"
            else:
                # New platform confirms this camera.  Cancel the old alert
                # even when the old OCR value simply matched nothing.
                cancel = True
                reason = ("新平台OSD正常，取消旧串流" if cross
                          else "新平台OSD正常，取消旧警报")

        item = {
            "time": alert["time"],
            "index": alert["index"],
            "channelCode": alert["channelCode"],
            "channelName": row["channelName"],
            "deviceName": row["deviceName"],
            "oldDetected": alert["detected"],
            "oldCrossover": "是" if cross else "",
            "oldMatchedDevice": cross["deviceName"] if cross else "",
            "newChannelCode": new_code or "",
            "newChannelName": (new_channels[new_code]["channelName"]
                               if new_code else ""),
            "newDetected": detected,
            "newStatus": status,
            "decision": "取消" if cancel else "保留",
            "reason": reason,
            "raw": alert["raw"],
            "round": alert["round"] or "",
            "roundStartedAt": alert.get("roundStartedAt") or "",
        }
        decisions.append(item)
        if cancel:
            cancelled.append(item)

    write_csv(
        COMPARE_DIR / "decisions.csv", decisions,
        [("time", "时间"), ("index", "编号"), ("channelCode", "旧通道编码"),
         ("channelName", "旧通道名称"), ("deviceName", "旧设备名称"),
         ("oldDetected", "旧识别"), ("oldCrossover", "旧串流"),
         ("oldMatchedDevice", "命中的旧设备"), ("newChannelCode", "新通道编码"),
         ("newChannelName", "新通道名称"), ("newDetected", "新识别"),
         ("newStatus", "新状态"), ("decision", "结论"), ("reason", "说明"),
         ("round", "轮次")])
    write_csv(
        COMPARE_DIR / "cancel.csv", cancelled,
        [("time", "时间"), ("index", "编号"), ("channelCode", "旧通道编码"),
         ("channelName", "旧通道名称"), ("deviceName", "旧设备名称"),
         ("oldDetected", "旧识别"), ("oldMatchedDevice", "命中的旧设备"),
         ("newChannelCode", "新通道编码"), ("newChannelName", "新通道名称"),
         ("newDetected", "新识别"), ("reason", "说明")])
    write_csv(
        COMPARE_DIR / "keep.csv",
        [row for row in decisions if row["decision"] == "保留"],
        [("time", "时间"), ("index", "编号"), ("channelCode", "旧通道编码"),
         ("channelName", "旧通道名称"), ("deviceName", "旧设备名称"),
         ("oldDetected", "旧识别"), ("oldCrossover", "旧串流"),
         ("newChannelCode", "新通道编码"), ("newChannelName", "新通道名称"),
         ("newDetected", "新识别"), ("newStatus", "新状态"),
         ("reason", "说明")])

    print(f"decisions: {len(decisions)}, cancel: {len(cancelled)}, "
          f"keep: {len(decisions) - len(cancelled)}")

    # Put the HK03 frame next to the old frame in the same round folder,
    # with an ``hk03_`` prefix so both can coexist.  copy2 keeps the capture
    # mtime, so the nightly prune can drop old frames by date.
    copied = 0
    for row in decisions:
        if not row["newChannelCode"]:
            continue
        sources = list(FRAME_SOURCE_DIR.glob(
            f"*_{SAFE.sub('_', row['newChannelCode'])}.jpg"))
        if not sources:
            continue
        old_name = safe_name(row["index"], row["channelCode"])
        folder_name = round_folder(row.get("round"),
                                   row.get("roundStartedAt"))
        if folder_name:
            folders = [SNAPSHOT_DIR / folder_name]
        else:
            folders = [folder for folder in SNAPSHOT_DIR.glob("round_*")
                       if (folder / old_name).is_file()]
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sources[0], folder / f"hk03_{old_name}")
            copied += 1
    print(f"copied {copied} HK03 frames into snapshot round folders")

    if not write:
        print("dry-run: no rows deleted (add --write to apply)")
        return
    if not cancelled:
        print("nothing to cancel")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"osd_error_{stamp}.txt"
    shutil.copy2(OLD_ERROR, backup)
    print(f"backup: {backup}")

    cancel_keys = {
        (row["time"], row["index"], row["channelCode"]) for row in cancelled
    }
    raw_lines = OLD_ERROR.read_text(
        encoding="utf-8", errors="replace").splitlines()
    removed: list[str] = []
    kept: list[str] = []
    removed_by_round: dict[int, set[tuple[str, str, str]]] = defaultdict(set)
    current_round = None
    for raw in raw_lines:
        line = raw.rstrip("\r\n")
        header = ROUND_HEADER_RE.match(line)
        if header:
            current_round = int(header.group(1))
        parts = line.split("\t")
        key = (parts[0], parts[1], parts[2]) if len(parts) >= 3 else None
        if key in cancel_keys:
            removed.append(line)
            if current_round is not None:
                removed_by_round[current_round].add(key)
        else:
            kept.append(raw)

    # Keep the round summary consistent with the rows that remain.
    if removed_by_round:
        current_round = None
        counts: dict[int, int] = defaultdict(int)
        for raw in kept:
            header = ROUND_HEADER_RE.match(raw)
            if header:
                current_round = int(header.group(1))
                continue
            if raw.startswith("#") or not raw.strip():
                continue
            if current_round is not None:
                counts[current_round] += 1
        rewritten = []
        for raw in kept:
            done = ROUND_DONE_RE.match(raw)
            if done and int(done.group(1)) in removed_by_round:
                number = int(done.group(1))
                raw = ROUND_DONE_RE.sub(
                    lambda m: (f"# round {m.group(1)} done: {m.group(2)}/"
                               f"{m.group(3)} OK, {counts[number]} anomaly"
                               f"{m.group(5) or ''}, "
                               f"{m.group(6) + ' capture timeout skipped, ' if m.group(6) else ''}"
                               f"{m.group(7)}s"),
                    raw)
            rewritten.append(raw)
        kept = rewritten

    kept.append(f"# cancelled {len(removed)} rows "
                f"(HK03 no crossover, {now_text()})")
    tmp = OLD_ERROR.with_name(OLD_ERROR.name + ".hk03.tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(tmp, OLD_ERROR)

    audit = COMPARE_DIR / "removed_rows.txt"
    audit.write_text("\n".join(removed) + "\n", encoding="utf-8")
    print(f"removed {len(removed)} rows; audit: {audit}")

    # Dashboard confirmations for deleted rows must not survive in reports.
    deleted_confirmations = 0
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            for row in cancelled:
                row_key = f"{row['time']}|{row['index']}|{row['channelCode']}"
                cur = conn.execute(
                    "DELETE FROM row_confirmations WHERE row_key = ?",
                    (row_key,))
                deleted_confirmations += cur.rowcount
                conn.execute(
                    "INSERT INTO confirmation_events "
                    "(row_key, action, result, note, at, confirm_date, "
                    "channel_code, actor, source) "
                    "VALUES (?, 'cancel-hk03', NULL, ?, ?, ?, ?, 'system', 'hk03')",
                    (row_key, row["reason"], now_text(), date,
                     row["channelCode"]))
            conn.commit()
        finally:
            conn.close()
    print(f"deleted {deleted_confirmations} dashboard confirmations")

    # Keep today's cancelled devices from re-alerting in later old-platform
    # rounds.  The stored values are audit only; suppression is per device.
    channels: dict[str, list[str]] = {}
    if SUPPRESS_FILE.exists():
        try:
            previous = json.loads(SUPPRESS_FILE.read_text(encoding="utf-8"))
            if previous.get("date") == date:
                channels = {
                    key: list(values)
                    for key, values in (previous.get("channels") or {}).items()
                }
        except (OSError, ValueError):
            channels = {}
    for row in cancelled:
        values = set(channels.get(row["channelCode"], []))
        values.add(row["oldDetected"])
        channels[row["channelCode"]] = sorted(values)
    payload = {"date": date, "created": now_text(), "channels": channels}
    tmp = SUPPRESS_FILE.with_name(SUPPRESS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, SUPPRESS_FILE)
    print(f"suppression: {SUPPRESS_FILE} ({len(channels)} channels)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply"])
    parser.add_argument("--write", action="store_true",
                        help="apply: delete rows and confirmations")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    else:
        apply(args.write)


if __name__ == "__main__":
    sys.exit(main())
