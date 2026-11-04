"""MCP server configs (local-first), stored in app.db.

An MCP server is an external tool provider an agent can connect to. Two
transports:

  - ``stdio`` : a local command we spawn (``command`` + ``args`` + ``env``).
                Runs entirely on this machine.
  - ``http``  : a streamable-HTTP MCP endpoint (``url``). The user's explicit
                choice — traffic leaves the machine.

Agents reference servers by id (``agents.mcp_servers``); the chat loop discovers
each enabled server's tools and exposes them as ``mcp__<server>__<tool>``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "server"


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            transport TEXT NOT NULL DEFAULT 'stdio',   -- 'stdio' | 'http'
            command TEXT,
            args TEXT,          -- JSON list
            env TEXT,           -- JSON object
            url TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    return conn


def _row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "slug": slugify(r["name"]),
        "transport": r["transport"],
        "command": r["command"] or "",
        "args": json.loads(r["args"]) if r["args"] else [],
        "env": json.loads(r["env"]) if r["env"] else {},
        "url": r["url"] or "",
        "enabled": bool(r["enabled"]),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def list_servers() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM mcp_servers ORDER BY updated_at DESC").fetchall()
    return [_row(r) for r in rows]


def get_server(server_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
    return _row(r) if r else None


def create_server(data: dict) -> dict:
    now = _now()
    sid = data.get("id") or uuid.uuid4().hex[:12]
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO mcp_servers (id, name, transport, command, args, env, url, enabled, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, data.get("name") or "Untitled server",
             data.get("transport") or "stdio", data.get("command") or None,
             json.dumps(data.get("args") or []), json.dumps(data.get("env") or {}),
             data.get("url") or None, 1 if data.get("enabled", True) else 0, now, now),
        )
    return get_server(sid)  # type: ignore[return-value]


def update_server(server_id: str, data: dict) -> dict | None:
    existing = get_server(server_id)
    if not existing:
        return None
    merged = {**existing, **{k: v for k, v in data.items() if v is not None}}
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE mcp_servers SET name=?, transport=?, command=?, args=?, env=?, url=?, "
            "enabled=?, updated_at=? WHERE id=?",
            (merged["name"], merged.get("transport") or "stdio", merged.get("command") or None,
             json.dumps(merged.get("args") or []), json.dumps(merged.get("env") or {}),
             merged.get("url") or None, 1 if merged.get("enabled", True) else 0, now, server_id),
        )
    return get_server(server_id)


def delete_server(server_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
    return cur.rowcount > 0
