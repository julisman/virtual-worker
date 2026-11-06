"""Per-agent persistent memory — the harness's "Your memory" pillar.

Each agent keeps a list of short memory notes in ``app.db`` that survive
across chat sessions and model switches. The agent writes them with the
``memory_write`` tool and they're injected (compactly) into its system prompt on
every request, so it remembers facts about you and your projects without anything
leaving your machine. Stored locally only — like everything else in Virtual Worker.
"""

from __future__ import annotations

import sqlite3
from storage import _sqlite
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_memory_agent ON agent_memory(agent_id)")
        return conn
    except BaseException:
        conn.close()
        raise


def _row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "agentId": r["agent_id"],
        "content": r["content"],
        "tags": r["tags"] or "",
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def add(agent_id: str, content: str, tags: str = "") -> dict:
    """Store a memory note. Deduplicates exact-content repeats for the agent
    (bumps the timestamp instead of inserting a near-identical row)."""
    content = (content or "").strip()
    now = _now()
    with _lock, _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM agent_memory WHERE agent_id=? AND content=?",
            (agent_id, content),
        ).fetchone()
        if existing:
            conn.execute("UPDATE agent_memory SET updated_at=?, tags=? WHERE id=?",
                         (now, (tags or "").strip(), existing["id"]))
            mem_id = existing["id"]
        else:
            mem_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO agent_memory (id, agent_id, content, tags, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (mem_id, agent_id, content, (tags or "").strip(), now, now),
            )
        r = conn.execute("SELECT * FROM agent_memory WHERE id=?", (mem_id,)).fetchone()
    return _row(r)


def list_for(agent_id: str, limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_memory WHERE agent_id=? ORDER BY updated_at DESC LIMIT ?",
            (agent_id, max(1, int(limit))),
        ).fetchall()
    return [_row(r) for r in rows]


def search(agent_id: str, query: str, limit: int = 20) -> list[dict]:
    """Case-insensitive substring match over content + tags, recent first."""
    q = f"%{(query or '').strip().lower()}%"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_memory WHERE agent_id=? AND "
            "(LOWER(content) LIKE ? OR LOWER(tags) LIKE ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (agent_id, q, q, max(1, int(limit))),
        ).fetchall()
    return [_row(r) for r in rows]


def delete(agent_id: str, mem_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM agent_memory WHERE agent_id=? AND id=?", (agent_id, mem_id))
    return cur.rowcount > 0


def clear(agent_id: str) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM agent_memory WHERE agent_id=?", (agent_id,))
    return cur.rowcount


def count(agent_id: str) -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM agent_memory WHERE agent_id=?", (agent_id,)
        ).fetchone()["c"]
