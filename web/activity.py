"""Persistent activity log for the admin panel — who logged in, who ran
what analysis, and admin actions like maintenance toggles.

A single SQLite file under ``~/.tradingagents`` by default, or under
``TRADINGAGENTS_WEB_DATA_DIR`` when set — point that at a mounted volume
in production (e.g. Railway's container filesystem is wiped on every
deploy/restart, so without a real volume this data never survives one).
Deliberately not the in-memory access log (``routes.py``'s ``_access_log``,
a rolling deque wiped on restart): this is a small, low-frequency append-only
log, so a plain per-call sqlite3 connection is simple enough and needs no
connection-pool machinery.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime

_DATA_DIR = os.environ.get("TRADINGAGENTS_WEB_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".tradingagents")
_DB_PATH = os.path.join(_DATA_DIR, "web_admin.db")
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    username TEXT,
    action   TEXT NOT NULL,
    detail   TEXT,
    ip       TEXT
);
CREATE TABLE IF NOT EXISTS ip_regions (
    ip     TEXT PRIMARY KEY,
    region TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts DESC);
CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(username);
"""

# Actions counted as a "login" / "analysis" for the per-user summary.
_LOGIN_ACTION = "login"
_ANALYZE_ACTION = "analyze_start"


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def get_cached_region(ip: str) -> str | None:
    try:
        with sqlite3.connect(_DB_PATH, timeout=2.0) as conn:
            row = conn.execute("SELECT region FROM ip_regions WHERE ip = ?", (ip,)).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def set_cached_region(ip: str, region: str) -> None:
    try:
        with _write_lock, sqlite3.connect(_DB_PATH, timeout=5.0) as conn:
            conn.execute("INSERT OR REPLACE INTO ip_regions (ip, region) VALUES (?, ?)", (ip, region))
    except Exception:
        pass


def get_ip_region(ip: str) -> str:
    if not ip or ip in ("127.0.0.1", "::1", "localhost", "unknown", ""):
        return "本地"
    if ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("172."):
        return "局域网"

    cached = get_cached_region(ip)
    if cached:
        return cached

    import urllib.request
    import json
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("status") == "success":
                region = f"{data.get('country', '')} {data.get('regionName', '')} {data.get('city', '')}".strip()
                if region:
                    set_cached_region(ip, region)
                    return region
    except Exception:
        pass

    return "未知"


def log_activity(username: str | None, action: str, detail: str = "", ip: str = "") -> None:
    """Append one activity event. Never raises — a logging failure must not
    break the request that triggered it."""
    try:
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        with _write_lock, _connect() as conn:
            conn.execute(
                "INSERT INTO activity (ts, username, action, detail, ip) VALUES (?, ?, ?, ?, ?)",
                (ts, username, action, detail, ip),
            )
        # Prefetch and cache region in background thread so it doesn't block the request
        if ip and ip not in ("127.0.0.1", "::1", "localhost", "unknown"):
            threading.Thread(target=get_ip_region, args=(ip,), daemon=True).start()
    except Exception:
        pass


def recent_activity(limit: int = 100) -> list[dict]:
    """Most recent activity events, newest first."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, username, action, detail, ip FROM activity ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def user_activity_summary(usernames: list[str]) -> list[dict]:
    """Per-user summary: last time seen, total logins, total analyses run.

    One query per metric across all users rather than N queries per user —
    the table is small (single-machine admin log) so this is plenty fast.
    """
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        last_seen = dict(
            conn.execute(
                "SELECT username, MAX(ts) AS last_ts FROM activity "
                "WHERE username IS NOT NULL GROUP BY username",
            ).fetchall(),
        )
        login_counts = dict(
            conn.execute(
                "SELECT username, COUNT(*) AS n FROM activity WHERE action = ? GROUP BY username",
                (_LOGIN_ACTION,),
            ).fetchall(),
        )
        analyze_counts = dict(
            conn.execute(
                "SELECT username, COUNT(*) AS n FROM activity WHERE action = ? GROUP BY username",
                (_ANALYZE_ACTION,),
            ).fetchall(),
        )
    return [
        {
            "username": u,
            "last_seen": last_seen.get(u),
            "login_count": login_counts.get(u, 0),
            "analyze_count": analyze_counts.get(u, 0),
        }
        for u in usernames
    ]
