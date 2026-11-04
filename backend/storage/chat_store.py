"""Persistent chat sessions (local).

Stores Chat conversations in app.db so they survive restarts — list past
chats, restore one, start fresh. A session = a conversation; messages keep their
role/content plus optional images and tool steps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY, title TEXT, model TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    # Guarded migration: record which agent ran a conversation.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    if "agent_id" not in cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN agent_id TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, seq INTEGER NOT NULL,
            role TEXT NOT NULL, content TEXT, images TEXT, steps TEXT, ts TEXT,
            FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages (session_id, seq)")
    # Guarded migration: per-message meta (timing / token usage / tools used).
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
    if "meta" not in mcols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN meta TEXT")
    return conn


def list_sessions() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT s.id, s.title, s.model, s.agent_id, s.created_at, s.updated_at,
                   COUNT(m.id) AS message_count
            FROM chat_sessions s LEFT JOIN chat_messages m ON m.session_id = s.id
            GROUP BY s.id ORDER BY s.updated_at DESC
        """).fetchall()
    return [{
        "id": r["id"], "title": r["title"], "model": r["model"], "agentId": r["agent_id"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
        "messageCount": r["message_count"],
    } for r in rows]


def get_session(session_id: str) -> dict | None:
    with _conn() as conn:
        s = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        if not s:
            return None
        rows = conn.execute(
            "SELECT role, content, images, steps, meta FROM chat_messages WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
    messages = [{
        "role": r["role"], "content": r["content"],
        "images": json.loads(r["images"]) if r["images"] else None,
        "steps": json.loads(r["steps"]) if r["steps"] else None,
        "meta": json.loads(r["meta"]) if r["meta"] else None,
    } for r in rows]
    return {"id": s["id"], "title": s["title"], "model": s["model"], "agentId": s["agent_id"],
            "createdAt": s["created_at"], "updatedAt": s["updated_at"], "messages": messages}


def upsert_session(session_id: str, title: str, model: str | None, messages: list[dict],
                   agent_id: str | None = None) -> dict:
    """Create or replace a session's content (messages overwritten wholesale)."""
    now = _now()
    with _lock, _conn() as conn:
        exists = conn.execute("SELECT created_at FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        created = exists["created_at"] if exists else now
        conn.execute(
            "INSERT OR REPLACE INTO chat_sessions (id, title, model, agent_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, title[:120] if title else "New chat", model, agent_id, created, now),
        )
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        for seq, m in enumerate(messages):
            conn.execute(
                "INSERT INTO chat_messages (session_id, seq, role, content, images, steps, meta, ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (session_id, seq, m.get("role"), m.get("content", ""),
                 json.dumps(m["images"]) if m.get("images") else None,
                 json.dumps(m["steps"]) if m.get("steps") else None,
                 json.dumps(m["meta"]) if m.get("meta") else None, now),
            )
    return {"id": session_id, "title": title, "model": model, "agentId": agent_id, "updatedAt": now}


def append_message(session_id: str, role: str, content: str, *,
                   meta: dict | None = None, title: str | None = None,
                   agent_id: str | None = None) -> dict:
    """Append a single message to a session, creating the session if it doesn't
    exist yet. Used by the in-app delivery channel (PRD-01) so a delivered result
    shows up as a new message in the operator's chat thread — addressed by an
    explicit ``session_id`` (or stable alias), never a captured sender."""
    now = _now()
    with _lock, _conn() as conn:
        existing = conn.execute(
            "SELECT created_at FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO chat_sessions (id, title, model, agent_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, (title or "Delivered")[:120], None, agent_id, now, now),
            )
            seq = 0
        else:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM chat_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["n"]
            conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        conn.execute(
            "INSERT INTO chat_messages (session_id, seq, role, content, images, steps, meta, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session_id, seq, role, content or "", None, None,
             json.dumps(meta) if meta else None, now),
        )
    return {"sessionId": session_id, "seq": seq, "ts": now}


def delete_session(session_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
    return cur.rowcount > 0
