"""Daily HK03 intersection suppression.

After the daily refresh has updated both channel tables, find every device
whose old-platform ``设备名称`` equals an HK03 ``通道名称``.  The operator's
rule is intentionally simple: every intersection device is put in the daily
suppression list.  osd20 then skips those channel codes entirely.

``check`` writes the list; ``apply`` writes it to
``state/hk03_cancelled.json`` and removes today's old rows.  ``apply`` must run
after osd20 is stopped.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import hk03_reconcile as h  # noqa: E402
from channel_filter import is_blocked, load_blocked_prefixes  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
TODAY = lambda: datetime.now(BEIJING).strftime('%Y-%m-%d')
NOW = lambda: datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')

OLD_CHANNELS = ROOT / 'out' / 'channels.csv'
NEW_DIR = ROOT / 'out-hk03'
NEW_CHANNELS = NEW_DIR / 'channels.csv'
NEW_STREAMS = NEW_DIR / 'streams.txt'
BLOCKED = ROOT / 'config' / 'blocked_channels.txt'
OSD_ERROR = ROOT / 'output' / 'osd_error.txt'
STATE = ROOT / 'state' / 'hk03_cancelled.json'
DB = ROOT / 'state' / 'dashboard.db'
WORK = ROOT / 'temp' / 'hk03'
FRAMES = WORK / 'frames'
CHECK_JSON = WORK / 'daily_suppress.json'
AUDIT_CSV = WORK / 'daily_suppress.csv'
SUBSET = WORK / 'daily_intersection_channels.txt'
EXPECTED = WORK / 'daily_expected_channels.csv'
RESULTS = WORK / 'daily_intersection_results.csv'
OUTPUT = ROOT / 'output-hk03.txt'
RUN_LOG = WORK / 'daily_intersection_run.log'
BACKUP = WORK / 'backup'


def log(message: str) -> None:
    print(f'{NOW()} {message}', flush=True)


def write_csv(path: Path, rows: list[dict], fields: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _, label in fields])
        for row in rows:
            writer.writerow([row.get(key, '') for key, _ in fields])


def scan_intersection(limit: int = 0) -> tuple[list[dict], dict[str, dict]]:
    old = h.load_channels(OLD_CHANNELS)
    new = h.load_channels(NEW_CHANNELS)
    blocked = load_blocked_prefixes(BLOCKED)

    by_name: dict[str, list[str]] = defaultdict(list)
    for code, row in new.items():
        name = row['channelName']
        if name:
            by_name[name].append(code)

    items: list[dict] = []
    for old_code, row in old.items():
        device = row['deviceName']
        if not device or is_blocked(old_code, blocked):
            continue
        codes = by_name.get(device, [])
        if not codes:
            continue
        new_code = codes[0] if len(codes) == 1 else ''
        new_name = (new[new_code]['channelName'] if new_code else
                    ';'.join(new[c]['channelName'] for c in codes))
        items.append({
            'oldCode': old_code, 'oldName': row['channelName'],
            'deviceName': device, 'newCode': new_code,
            'newName': new_name, 'status': 'INTERSECTION', 'reason': '',
        })
    if limit:
        items = items[:limit]
    return items, {}


def run_hk03_check(args, subset: dict[str, dict]) -> dict[str, dict]:
    if not subset:
        return {}
    for path in (RESULTS, OUTPUT, RUN_LOG):
        path.unlink(missing_ok=True)
    # The monitor expects the old-platform 设备名称 in this file; for HK03
    # the equivalent expected value is 通道名称.
    with EXPECTED.open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(['通道编码', '设备名称'])
        for code in sorted(subset):
            writer.writerow([code, subset[code]['newName']])
    cmd = [
        sys.executable, str(ROOT / 'scripts' / 'evo_osd_monitor.py'),
        '--base', args.base,
        '--user', args.user,
        '--password', args.password,
        '--input', str(SUBSET),
        '--channels-file', str(EXPECTED),
        '--blocked-file', str(BLOCKED),
        '--output', str(OUTPUT),
        '--csv', str(RESULTS),
        '--suppress-file', str(WORK / 'no-suppress.json'),
        '--no-save',
        '--stream-type', '2',
        '--workers', str(args.workers),
        '--frames', str(args.frames),
        '--frame-source', 'key',
        '--limit', '0',
        '--once',
        '--timeout', str(args.timeout),
        '--osd-profile-file', str(ROOT / 'config' / 'osd_profiles.json'),
        '--cell-reader', 'cells',
        '--cell-decoder', 'beamsearch',
    ]
    if args.gpu:
        cmd.append('--gpu')
    log(f"daily HK03 check: {len(subset)} channels")
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open('w', encoding='utf-8') as log_fh:
        proc = subprocess.run(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                              timeout=args.job_timeout)
    log(f"daily HK03 check rc={proc.returncode}; log={RUN_LOG}")
    rows: dict[str, dict] = {}
    if RESULTS.exists():
        with RESULTS.open(encoding='utf-8-sig', newline='') as fh:
            for row in csv.DictReader(fh):
                code = (row.get('通道编码') or '').strip()
                if code:
                    rows[code] = row
    return rows


def check(args) -> int:
    items, _subset = scan_intersection(limit=args.limit)
    decision: dict[str, dict] = {}
    channels: dict[str, list[str]] = {}
    for item in items:
        # The operator's current rule is deliberately simple: every device
        # present in both tables is suppressed; no HK03 OCR is required.
        item.update({
            'detected': '',
            'status': 'INTERSECTION',
            'reason': '交集设备，按规则全部抑制',
            'suppress': True,
        })
        decision[item['oldCode']] = item
        channels[item['oldCode']] = [item.get('newName') or item['deviceName']]

    payload = {
        'date': TODAY(),
        'created': NOW(),
        'channels': channels,
        'details': list(decision.values()),
    }
    CHECK_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')
    audit_rows = [{**item, 'decision': '抑制'} for item in decision.values()]
    write_csv(AUDIT_CSV, audit_rows, [
        ('oldCode', '旧通道编码'), ('oldName', '旧通道名称'),
        ('deviceName', '设备名称'), ('newCode', 'HK03通道编码'),
        ('newName', 'HK03通道名称'), ('detected', 'HK03识别'),
        ('status', 'HK03状态'), ('decision', '结论'), ('reason', '说明'),
    ])
    log(f"intersection={len(items)} suppress={len(channels)} audit={AUDIT_CSV}")
    return 0


def remove_rows(codes: set[str]) -> tuple[list[dict], Path | None]:
    if not codes or not OSD_ERROR.exists():
        return [], None
    today = TODAY()
    lines = OSD_ERROR.read_text(encoding='utf-8', errors='replace').splitlines()
    removed: list[dict] = []
    kept: list[str] = []
    removed_rounds: set[int] = set()
    current_round: int | None = None
    for raw in lines:
        line = raw.rstrip('\r\n')
        header = h.ROUND_HEADER_RE.match(line)
        if header:
            current_round = int(header.group(1))
        parts = line.split('\t')
        if (len(parts) >= 8 and parts[0][:10] == today
                and parts[2] in codes):
            removed.append({
                'time': parts[0], 'index': parts[1],
                'channelCode': parts[2],
            })
            if current_round is not None:
                removed_rounds.add(current_round)
            continue
        kept.append(raw)
    if not removed:
        return [], None

    BACKUP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime('%Y%m%d_%H%M%S')
    backup = BACKUP / f'osd_error_{stamp}_daily_suppress.txt'
    shutil.copy2(OSD_ERROR, backup)

    counts: Counter[int] = Counter()
    current_round = None
    for raw in kept:
        header = h.ROUND_HEADER_RE.match(raw)
        if header:
            current_round = int(header.group(1))
            continue
        if raw.startswith('#') or not raw.strip():
            continue
        if current_round is not None:
            counts[current_round] += 1

    rewritten: list[str] = []
    for raw in kept:
        done = h.ROUND_DONE_RE.match(raw)
        if done and int(done.group(1)) in removed_rounds:
            number = int(done.group(1))
            raw = h.ROUND_DONE_RE.sub(
                lambda m: (
                    f'# round {m.group(1)} done: {m.group(2)}/{m.group(3)} OK, '
                    f'{counts[number]} anomaly{m.group(5) or ""}, '
                    f'{m.group(6) + " capture timeout skipped, " if m.group(6) else ""}'
                    f'{m.group(7)}s'
                ),
                raw,
            )
        rewritten.append(raw)
    rewritten.append(f'# daily HK03 suppression: removed {len(removed)} rows '
                     f'({NOW()})')
    tmp = OSD_ERROR.with_name(OSD_ERROR.name + '.daily.tmp')
    tmp.write_text('\n'.join(rewritten) + '\n', encoding='utf-8')
    os.replace(tmp, OSD_ERROR)
    return removed, backup


def apply(args) -> int:
    if not CHECK_JSON.exists():
        raise SystemExit(f'missing {CHECK_JSON}; run check first')
    BACKUP.mkdir(parents=True, exist_ok=True)
    payload = json.loads(CHECK_JSON.read_text(encoding='utf-8'))
    if payload.get('date') != TODAY():
        raise SystemExit(f"check date {payload.get('date')} != today {TODAY()}")
    channels = payload.get('channels') or {}
    codes = set(channels)
    removed, backup = remove_rows(codes)
    if removed:
        log(f'removed {len(removed)} old rows; backup={backup}')
    else:
        log('no old rows to remove')

    if DB.exists():
        BACKUP.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(BEIJING).strftime('%Y%m%d_%H%M%S')
        db_backup = ROOT / 'temp' / 'backups' / f'dashboard-before-daily-suppress-{stamp}.db'
        db_backup.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(db_backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        conn = sqlite3.connect(DB, timeout=10)
        try:
            conn.execute('PRAGMA busy_timeout=5000')
            for row in removed:
                row_key = f"{row['time']}|{row['index']}|{row['channelCode']}"
                conn.execute('DELETE FROM row_confirmations WHERE row_key = ?', (row_key,))
                conn.execute(
                    'INSERT INTO confirmation_events '
                    '(row_key, action, result, note, at, confirm_date, channel_code, actor, source) '
                    "VALUES (?, 'cancel-hk03-daily', NULL, ?, ?, ?, ?, 'system', 'hk03-daily')",
                    (row_key, '每日HK03交集抑制', NOW(), TODAY(), row['channelCode']),
                )
            conn.commit()
        finally:
            conn.close()
        log(f'dashboard backup={db_backup}')

    state = {
        'date': TODAY(),
        'created': NOW(),
        'channels': {
            code: list(values) if isinstance(values, list) else [str(values)]
            for code, values in channels.items()
        },
    }
    if STATE.exists():
        shutil.copy2(STATE, BACKUP / f'hk03_cancelled_before_daily_{datetime.now(BEIJING):%Y%m%d_%H%M%S}.json')
    tmp = STATE.with_name(STATE.name + '.daily.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    os.replace(tmp, STATE)
    log(f'suppression={STATE} channels={len(state["channels"])}')
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'apply'])
    parser.add_argument('--base', default=os.getenv('HK03_BASE', ''))
    parser.add_argument('--user', default=os.getenv('HK03_USER', ''))
    parser.add_argument('--password', default=os.getenv('HK03_PASSWORD', ''))
    parser.add_argument('--limit', type=int, default=0,
                        help='testing only: check at most N HK03 channels')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--frames', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--job-timeout', type=float, default=4 * 3600)
    parser.add_argument('--gpu', action='store_true')
    args = parser.parse_args()
    if args.action == 'check':
        return check(args)
    return apply(args)


if __name__ == '__main__':
    sys.exit(main())
