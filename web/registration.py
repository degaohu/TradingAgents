"""Pending email-verification registrations.

A user who submits the public registration form doesn't get a real account
immediately — they get a row here, keyed by a random one-time token, until
they click the verification link sent to their email (see the /api/register
and /verify-email routes in web/routes.py). This keeps unverified signups
(typos, someone else's email, abandoned forms) from claiming usernames or
cluttering the real users table.

Persisted in SQLite on the same data dir as users/quota/history
(TRADINGAGENTS_WEB_DATA_DIR, a mounted volume in production) so a pending
registration survives a redeploy between "submitted the form" and "clicked
the email link".
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time

_DATA_DIR = os.environ.get("TRADINGAGENTS_WEB_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".tradingagents")
_DB_PATH = os.path.join(_DATA_DIR, "web_pending_registrations.db")
_write_lock = threading.Lock()

# How long a verification link stays valid.
TOKEN_TTL_SECONDS = 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_registrations (
    token         TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_username ON pending_registrations(username);
CREATE INDEX IF NOT EXISTS idx_pending_email ON pending_registrations(email);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def create(username: str, email: str, password_hash: str, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """Create a pending registration and return its one-time token.

    Any previous pending row for this username or email is removed first —
    only the most recent registration (or resend) attempt has a live token,
    so an old, unused verification link stops working once a new one is
    issued instead of leaving two valid links outstanding.
    """
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _write_lock, _connect() as conn:
        conn.execute("DELETE FROM pending_registrations WHERE username = ? OR email = ?", (username, email))
        conn.execute(
            "INSERT INTO pending_registrations "
            "(token, username, email, password_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token, username, email, password_hash, now, now + ttl_seconds),
        )
    return token


def consume(token: str) -> dict | None:
    """Fetch and delete a pending registration in one step (single-use, and
    atomic so a double-click can't race itself into two accounts). Returns
    None if the token is unknown or has expired."""
    with _write_lock, _connect() as conn:
        row = conn.execute(
            "SELECT username, email, password_hash, expires_at FROM pending_registrations WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM pending_registrations WHERE token = ?", (token,))
        username, email, password_hash, expires_at = row
        if time.time() > expires_at:
            return None
        return {"username": username, "email": email, "password_hash": password_hash}


def find_by_identifier(identifier: str) -> dict | None:
    """Most recent pending registration matching this username or email —
    used to reissue a token when resending the verification email."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, email, password_hash FROM pending_registrations "
            "WHERE username = ? OR email = ? ORDER BY created_at DESC LIMIT 1",
            (identifier, identifier),
        ).fetchone()
    if row is None:
        return None
    return {"username": row[0], "email": row[1], "password_hash": row[2]}
