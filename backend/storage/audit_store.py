"""Audit log for team mode — one row per mutating API request.

Who did what, when, with which outcome. Written best-effort by the team-auth
middleware after the response (never on the hot path of the request), read by
``GET /api/team/audit`` (admin and above), pruned by the maintenance loop.
Solo mode writes nothing here: there is no identity to attribute.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from config import db_path, ensure_dir
from storage import _sqlite

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            user_id TEXT,
            username TEXT,
            role TEXT,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            status INTEGER,
            client TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
    conn.mark_initialized()
    return conn


def log(*, user: dict | None, method: str, path: str, status: int | None, client: str | None = None) -> None:
    """Append one row. Best-effort: callers wrap in try/except."""
    u = user or {}
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (id, ts, user_id, username, role, method, path, status, client) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], _now(), u.get("id"), u.get("username"), u.get("role"),
             method, path[:500], status, (client or "")[:80]))


def list_recent(*, limit: int = 200, user_id: str | None = None) -> list[dict]:
    q = "SELECT * FROM audit_log"
    args: list = []
    if user_id:
        q += " WHERE user_id=?"; args.append(user_id)
    q += " ORDER BY ts DESC LIMIT ?"; args.append(max(1, min(int(limit), 2000)))
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "userId": r["user_id"], "username": r["username"],
             "role": r["role"], "method": r["method"], "path": r["path"], "status": r["status"],
             "client": r["client"]} for r in rows]


def prune(days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, days))).isoformat()
    with _lock, _conn() as conn:
        return conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,)).rowcount
