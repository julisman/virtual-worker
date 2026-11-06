"""CCR store — originals of compressed tool results (PRD-17).

When ``agent.crusher`` drops bytes from a tool result, the *original* lands
here, keyed by its content hash, so the model can recover it on demand via the
``retrieve_full`` tool. Rows carry a TTL and the table is size-capped: this is
a recovery buffer, not an archive. Local-only, like everything in ``app.db``.
"""

from __future__ import annotations

import os
import sqlite3
from storage import _sqlite
import threading
from datetime import datetime, timedelta, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()

TTL_HOURS = float(os.environ.get("VW_CCR_TTL_HOURS", "24"))
MAX_MB = float(os.environ.get("VW_CCR_MAX_MB", "100"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ccr_originals (
            hash        TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            tool_name   TEXT,
            agent_id    TEXT,
            session_id  TEXT,
            size_chars  INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ccr_expires ON ccr_originals(expires_at)")
    conn.mark_initialized()
    return conn


def put(hash: str, content: str, tool_name: str = "", agent_id: str = "",
        session_id: str = "", ttl_hours: float | None = None) -> str:
    """Store (or refresh) an original under its content hash. Returns the hash.
    Same content ⇒ same hash ⇒ idempotent upsert (TTL is extended)."""
    now = _now()
    expires = now + timedelta(hours=ttl_hours if ttl_hours is not None else TTL_HOURS)
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO ccr_originals
               (hash, content, tool_name, agent_id, session_id, size_chars, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(hash) DO UPDATE SET
                 content=excluded.content, tool_name=excluded.tool_name,
                 agent_id=excluded.agent_id, session_id=excluded.session_id,
                 size_chars=excluded.size_chars, expires_at=excluded.expires_at""",
            (hash, content, tool_name, agent_id, session_id, len(content),
             now.isoformat(), expires.isoformat()))
    return hash


def get(hash: str) -> dict | None:
    """Fetch a live original by hash; None if unknown or expired."""
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM ccr_originals WHERE hash=?", (hash,)).fetchone()
    if r is None or r["expires_at"] < _now().isoformat():
        return None
    return {
        "hash": r["hash"],
        "content": r["content"],
        "toolName": r["tool_name"] or "",
        "agentId": r["agent_id"] or "",
        "sessionId": r["session_id"] or "",
        "sizeChars": r["size_chars"],
        "createdAt": r["created_at"],
        "expiresAt": r["expires_at"],
    }


def purge_expired() -> int:
    """Drop expired rows, then enforce the size cap (oldest evicted first).
    Called from the maintenance loop; best-effort by design."""
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM ccr_originals WHERE expires_at < ?",
                           (_now().isoformat(),))
        removed = cur.rowcount
        total = conn.execute("SELECT COALESCE(SUM(size_chars),0) FROM ccr_originals").fetchone()[0]
        budget = int(MAX_MB * 1024 * 1024)
        if total > budget:
            for r in conn.execute(
                    "SELECT hash, size_chars FROM ccr_originals ORDER BY created_at").fetchall():
                if total <= budget:
                    break
                conn.execute("DELETE FROM ccr_originals WHERE hash=?", (r["hash"],))
                total -= r["size_chars"]
                removed += 1
    return removed


def usage() -> dict:
    """Store footprint, for Debug/observability."""
    with _lock, _conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size_chars),0) chars FROM ccr_originals").fetchone()
    return {"rows": r["n"], "mb": round(r["chars"] / 1048576, 2)}
