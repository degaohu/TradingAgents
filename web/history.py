"""Persistent per-user analysis history.

Previously the "history" list lived only in each browser's localStorage,
so it went blank on a new device, a different browser, or cleared site
data — and never existed at all if the page wasn't open to catch the
completion event. This makes each completed run durable server-side, keyed
by the user who ran it, independent of which device looks at it later.

Same SQLite-file pattern as activity.py: a single file under
~/.tradingagents by default, or under TRADINGAGENTS_WEB_DATA_DIR when set
(point that at a mounted volume in production — see activity.py's
docstring for why this matters on a platform with an ephemeral container
filesystem), a per-call connection (low-frequency writes — one per
completed analysis).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

_DATA_DIR = os.environ.get("TRADINGAGENTS_WEB_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".tradingagents")
_DB_PATH = os.path.join(_DATA_DIR, "web_history.db")
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    username    TEXT,
    ticker      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    decision    TEXT,
    result_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_user_ticker_date
    ON history(username, ticker, trade_date);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def save_history(username: str | None, ticker: str, trade_date: str, decision: str, result: dict) -> None:
    """Record one completed analysis for a user. Re-running the same
    ticker/date replaces the earlier entry (and bumps it back to most
    recent) rather than accumulating duplicates — matching the old
    localStorage behavior. Never raises: a logging failure must not break
    the job that triggered it."""
    try:
        # Microsecond precision: two runs finishing within the same second
        # (easily hit in tests, and not impossible in production) would
        # otherwise tie under ORDER BY ts DESC and defy insertion order.
        ts = datetime.now(UTC).isoformat(timespec="microseconds")
        with _write_lock, _connect() as conn:
            conn.execute(
                "INSERT INTO history (ts, username, ticker, trade_date, decision, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(username, ticker, trade_date) DO UPDATE SET "
                "ts=excluded.ts, decision=excluded.decision, result_json=excluded.result_json",
                (ts, username, ticker, trade_date, decision, json.dumps(result)),
            )
    except Exception:
        pass


def list_history(username: str | None, limit: int = 20) -> list[dict]:
    """This user's past analyses, most recently (re-)run first. Excludes
    the full result payload — the History tab only needs enough to render
    the list; fetch get_history_result() for a specific entry on demand."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, trade_date, decision, ts FROM history "
            "WHERE username IS ? ORDER BY ts DESC LIMIT ?",
            (username, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_history_result(username: str | None, ticker: str, trade_date: str) -> dict[str, Any] | None:
    """Full stored result payload for one history entry, for re-viewing."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT result_json FROM history WHERE username IS ? AND ticker = ? AND trade_date = ?",
            (username, ticker, trade_date),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["result_json"])
