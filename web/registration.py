"""Pending phone-verification registrations.

A user who submits the public registration form doesn't get a real account
immediately — they get a row here, keyed by phone number, until they enter
the SMS code Twilio Verify sent them (see web/smsverify.py and the
/api/register + /api/register/verify-code routes in web/routes.py). Twilio
Verify owns the actual one-time code — generation, expiry, delivery — this
table only bridges "submitted the form" to "entered the right code": the
username/password the code unlocks once Twilio confirms it.

Persisted in SQLite on the same data dir as users/quota/history
(TRADINGAGENTS_WEB_DATA_DIR, a mounted volume in production) so a pending
registration survives a redeploy between "submitted the form" and "entered
the code".
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

_DATA_DIR = os.environ.get("TRADINGAGENTS_WEB_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".tradingagents")
_DB_PATH = os.path.join(_DATA_DIR, "web_pending_registrations.db")
_write_lock = threading.Lock()

# How long a pending registration waits for its code to be entered. Shorter
# than an email link's TTL would be — SMS codes are meant to be entered
# within minutes, and Twilio's own code expires well before this anyway.
TTL_SECONDS = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_registrations (
    phone         TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_username ON pending_registrations(username);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def create(username: str, phone: str, password_hash: str, ttl_seconds: int = TTL_SECONDS) -> None:
    """Create (or replace) the pending registration for this phone number.

    Any previous pending row for this username or phone is removed first —
    only the most recent registration (or resend) attempt is live, so an
    old unconfirmed attempt can't be completed with a stale password after
    a new one is submitted.
    """
    now = time.time()
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM pending_registrations WHERE username = ? OR phone = ?", (username, phone))
        conn.execute(
            "INSERT INTO pending_registrations (phone, username, password_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (phone, username, password_hash, now, now + ttl_seconds),
        )


def get(phone: str) -> dict | None:
    """Peek at the pending registration for this phone without consuming
    it — used for "resend code" and right after Twilio confirms a code, to
    look up what account to create. Returns None if missing or expired."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, password_hash, expires_at FROM pending_registrations WHERE phone = ?",
            (phone,),
        ).fetchone()
    if row is None:
        return None
    username, password_hash, expires_at = row
    if time.time() > expires_at:
        return None
    return {"username": username, "phone": phone, "password_hash": password_hash}


def consume(phone: str) -> dict | None:
    """Fetch and delete a pending registration in one step (single-use,
    atomic so two near-simultaneous verify-code calls can't both succeed).
    Returns None if missing or expired."""
    with _write_lock, _connect() as conn:
        row = conn.execute(
            "SELECT username, password_hash, expires_at FROM pending_registrations WHERE phone = ?",
            (phone,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM pending_registrations WHERE phone = ?", (phone,))
        username, password_hash, expires_at = row
        if time.time() > expires_at:
            return None
        return {"username": username, "phone": phone, "password_hash": password_hash}
