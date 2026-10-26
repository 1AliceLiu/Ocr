"""Human confirmations for detected stream rows.

Only the dashboard's own state database is written; the monitor's files stay
read-only.  A row is keyed by ``time|index|channelCode`` so the same row in
the live file and in an archive maps to one confirmation.  Remarks are
generated from the confirmations, never entered by hand.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv(
    "CONFIRM_DB", str(PROJECT_ROOT / "state" / "dashboard.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS row_confirmations (
    row_key TEXT PRIMARY KEY,
    confirm_date TEXT NOT NULL,
    time TEXT NOT NULL,
    channel_code TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    result TEXT,
    note TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_row_confirmations_date
    ON row_confirmations(confirm_date, result);
CREATE TABLE IF NOT EXISTS confirmation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_key TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT,
    note TEXT,
    at TEXT NOT NULL,
    confirm_date TEXT NOT NULL,
    channel_code TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def get(row_key: str) -> dict[str, Any] | None:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM row_confirmations WHERE row_key = ?",
                (row_key,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return dict(row) if row else None


def list_for_date(date: str) -> list[dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM row_confirmations WHERE confirm_date = ? "
                "ORDER BY time, channel_code",
                (date,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    return [dict(row) for row in rows]


def save(
    *,
    row_key: str,
    confirm_date: str,
    time: str,
    channel_code: str,
    channel_name: str,
    result: str | None,
    note: str,
    confirmed_at: str,
    actor: str = "",
    source: str = "",
) -> dict[str, Any] | None:
    if result is None and not note:
        delete(row_key)
        return None
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO row_confirmations (
                row_key, confirm_date, time, channel_code, channel_name,
                result, note, confirmed_at, actor, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(row_key) DO UPDATE SET
                confirm_date = excluded.confirm_date,
                time = excluded.time,
                channel_code = excluded.channel_code,
                channel_name = excluded.channel_name,
                result = excluded.result,
                note = excluded.note,
                confirmed_at = excluded.confirmed_at,
                actor = excluded.actor,
                source = excluded.source
            """,
            (row_key, confirm_date, time, channel_code, channel_name,
             result, note, confirmed_at, actor, source),
        )
    return get(row_key)


def delete(row_key: str) -> dict[str, Any] | None:
    row = get(row_key)
    if row is None:
        return None
    with _connect() as conn:
        conn.execute(
            "DELETE FROM row_confirmations WHERE row_key = ?", (row_key,))
    return row


def delete_auto_after(channel_code: str, date: str, after_time: str) -> list[str]:
    """Drop derived auto-confirmations after a manually changed row.

    The next read recomputes the chain from the new manual state.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT row_key FROM row_confirmations "
                "WHERE channel_code = ? AND confirm_date = ? "
                "AND time > ? AND source = 'auto'",
                (channel_code, date, after_time),
            ).fetchall()
            keys = [row["row_key"] for row in rows]
            if keys:
                conn.executemany(
                    "DELETE FROM row_confirmations WHERE row_key = ?",
                    [(key,) for key in keys],
                )
    except (OSError, sqlite3.Error):
        return []
    return keys


def log_event(
    *,
    row_key: str,
    action: str,
    at: str,
    confirm_date: str,
    channel_code: str,
    result: str | None = None,
    note: str = "",
    actor: str = "",
    source: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO confirmation_events (
                row_key, action, result, note, at, confirm_date,
                channel_code, actor, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row_key, action, result, note, at, confirm_date,
             channel_code, actor, source),
        )
