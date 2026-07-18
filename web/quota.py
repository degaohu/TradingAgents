"""Per-user report-generation quota.

Each user has a remaining number of analyses they may run. It's consumed
when a run actually produces a report (on successful completion, not on
start — a cancelled or failed run costs nothing), and topped up by an admin
from the admin panel.

Persisted in SQLite on the same data dir as history/activity
(TRADINGAGENTS_WEB_DATA_DIR, a mounted volume in production) so balances
survive deploys and restarts. Defaults are virtual: a user with no row yet
is treated as having DEFAULT_QUOTA remaining, and a row is only written the
first time their balance actually changes.
"""

from __future__ import annotations

import os
import sqlite3
import threading

_DATA_DIR = os.environ.get("TRADINGAGENTS_WEB_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".tradingagents")
_DB_PATH = os.path.join(_DATA_DIR, "web_quota.db")
_write_lock = threading.Lock()

# Starting balance for a user who has never had their quota touched.
DEFAULT_QUOTA = int(os.environ.get("TRADINGAGENTS_DEFAULT_QUOTA") or 10)

# At/below this many remaining, the UI shows a low-balance warning.
LOW_QUOTA_THRESHOLD = int(os.environ.get("TRADINGAGENTS_LOW_QUOTA_THRESHOLD") or 3)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota (
    username  TEXT PRIMARY KEY,
    remaining INTEGER NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def _remaining(conn: sqlite3.Connection, username: str) -> int:
    row = conn.execute("SELECT remaining FROM quota WHERE username = ?", (username,)).fetchone()
    return row[0] if row is not None else DEFAULT_QUOTA


def get_remaining(username: str) -> int:
    """This user's remaining reports (DEFAULT_QUOTA if never touched)."""
    with _connect() as conn:
        return _remaining(conn, username)


def all_remaining(usernames: list[str]) -> dict[str, int]:
    with _connect() as conn:
        return {u: _remaining(conn, u) for u in usernames}


def consume(username: str) -> bool:
    """Deduct one report if the user has any left. Returns True if a unit was
    consumed, False if the balance was already zero (never goes negative)."""
    with _write_lock, _connect() as conn:
        cur = _remaining(conn, username)
        if cur <= 0:
            conn.execute(
                "INSERT INTO quota (username, remaining) VALUES (?, 0) "
                "ON CONFLICT(username) DO UPDATE SET remaining = 0",
                (username,),
            )
            return False
        conn.execute(
            "INSERT INTO quota (username, remaining) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET remaining = excluded.remaining",
            (username, cur - 1),
        )
        return True


def add(username: str, delta: int) -> int:
    """Add `delta` (may be negative) to a user's balance, clamped at 0.
    Returns the new remaining."""
    with _write_lock, _connect() as conn:
        new = max(0, _remaining(conn, username) + delta)
        conn.execute(
            "INSERT INTO quota (username, remaining) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET remaining = excluded.remaining",
            (username, new),
        )
        return new


def set_remaining(username: str, value: int) -> int:
    """Set an absolute balance (clamped at 0). Returns the new remaining."""
    value = max(0, value)
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO quota (username, remaining) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET remaining = excluded.remaining",
            (username, value),
        )
        return value
