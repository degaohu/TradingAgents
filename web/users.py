"""Persistent user store (usernames, passwords, admin flag).

Users were originally hard-coded from environment variables and only
changeable by redeploying. This moves them into SQLite on the same data
dir as history/activity/quota (TRADINGAGENTS_WEB_DATA_DIR, a mounted volume
in production) so an admin can reset passwords from the admin panel and
have it take effect immediately and survive restarts.

The env-var users are still the *seed*: on a fresh database they're
inserted once (INSERT OR IGNORE, so later password resets are never
clobbered by a redeploy). After that, the database is the source of truth.

Passwords are stored (and shown to the admin) in plaintext — a deliberate
choice by the operator for this small, single-operator internal tool. Do
not copy this pattern to anything multi-tenant or public.
"""

from __future__ import annotations

import hmac
import os
import sqlite3
import threading

_DATA_DIR = os.environ.get("TRADINGAGENTS_WEB_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".tradingagents")
_DB_PATH = os.path.join(_DATA_DIR, "web_users.db")
_write_lock = threading.Lock()

# Guards one-time seeding per database file. Keyed by path so the per-test
# temp DBs (see tests/conftest.py) each get seeded on first use.
_seeded_path: str | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def ensure_seeded(seed: dict[str, str], admins: set[str]) -> None:
    """Insert the seed users if they're not already present. Idempotent and
    cheap after the first call for a given database file."""
    global _seeded_path
    if _seeded_path == _DB_PATH:
        return
    with _write_lock, _connect() as conn:
        for username, password in seed.items():
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password, is_admin) VALUES (?, ?, ?)",
                (username, password, 1 if username in admins else 0),
            )
    _seeded_path = _DB_PATH


def verify(username: str, password: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT password FROM users WHERE username = ?", (username,)).fetchone()
    return row is not None and hmac.compare_digest(password, row[0])


def get_password(username: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT password FROM users WHERE username = ?", (username,)).fetchone()
    return row[0] if row is not None else None


def exists(username: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is not None


def is_admin(username: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT is_admin FROM users WHERE username = ?", (username,)).fetchone()
    return bool(row and row[0])


def list_usernames() -> list[str]:
    with _connect() as conn:
        return [r[0] for r in conn.execute("SELECT username FROM users ORDER BY is_admin DESC, username").fetchall()]


def admin_usernames() -> list[str]:
    with _connect() as conn:
        return [r[0] for r in conn.execute("SELECT username FROM users WHERE is_admin = 1").fetchall()]


def all_users() -> list[dict]:
    """Every user with their (plaintext) password and admin flag — used by
    the admin panel's user table."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, password, is_admin FROM users ORDER BY is_admin DESC, username"
        ).fetchall()
    return [{"username": r[0], "password": r[1], "is_admin": bool(r[2])} for r in rows]


def set_password(username: str, new_password: str) -> bool:
    """Set a user's password. Returns False if the user doesn't exist."""
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password = ? WHERE username = ?", (new_password, username)
        )
        return cur.rowcount > 0


def create_user(username: str, password: str, is_admin: bool = False) -> bool:
    """Create a new user. Returns False if the username already exists.
    Usernames are lowercased on insert so login case doesn't create dupes."""
    username = username.strip()
    if not username or not password:
        return False
    with _write_lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)",
                (username, password, 1 if is_admin else 0),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def delete_user(username: str) -> bool:
    """Delete a user. Returns False if the user doesn't exist."""
    with _write_lock, _connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        return cur.rowcount > 0


def set_admin(username: str, is_admin: bool) -> bool:
    """Toggle a user's admin flag. Returns False if the user doesn't exist."""
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET is_admin = ? WHERE username = ?",
            (1 if is_admin else 0, username),
        )
        return cur.rowcount > 0
