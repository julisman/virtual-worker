"""Agent inbox items for flow-back handoffs.

Inbox rows are scoped to the receiving agent, but claims are global so a later
drain loop can pick up work across all agents atomically.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

from config import db_path, ensure_dir
from storage import _sqlite

_lock = threading.Lock()
STATUSES = ("pending", "processing", "processed", "failed", "dismissed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_inbox (
            id TEXT PRIMARY KEY,
            agent_id TEXT,
            from_agent_id TEXT,
            task_id TEXT,
            origin_session_id TEXT,
            kind TEXT,
            payload TEXT,
            status TEXT,
            attempts INTEGER,
            last_error TEXT,
            created_at TEXT,
            processed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_inbox_agent ON agent_inbox(agent_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_inbox_status ON agent_inbox(status, created_at)")
    conn.mark_initialized()
    return conn


def _row(r: sqlite3.Row) -> dict:
    try:
        payload = json.loads(r["payload"]) if r["payload"] else {}
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": r["id"],
        "agentId": r["agent_id"],
        "fromAgentId": r["from_agent_id"],
        "taskId": r["task_id"],
        "originSessionId": r["origin_session_id"],
        "kind": r["kind"],
        "payload": payload,
        "status": r["status"],
        "attempts": r["attempts"],
        "lastError": r["last_error"],
        "createdAt": r["created_at"],
        "processedAt": r["processed_at"],
    }


def create(agent_id: str, data: dict) -> dict:
    item_id = "ibx_" + secrets.token_hex(8)
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO agent_inbox "
            "(id, agent_id, from_agent_id, task_id, origin_session_id, kind, payload, "
            "status, attempts, last_error, created_at, processed_at) "
            "VALUES (?,?,?,?,?,?,?,'pending',0,NULL,?,NULL)",
            (item_id, agent_id, data.get("fromAgentId"), data.get("taskId"),
             data.get("originSessionId"), data.get("kind") or "task_closed",
             json.dumps(data.get("payload") or {}), now),
        )
    return get(agent_id, item_id)  # type: ignore[return-value]


def get(agent_id: str, item_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_inbox WHERE agent_id=? AND id=?", (agent_id, item_id)
        ).fetchone()
    return _row(row) if row else None


def list_items(agent_id: str, statuses: list[str] | None = None) -> list[dict]:
    query = "SELECT * FROM agent_inbox WHERE agent_id=?"
    args: list = [agent_id]
    selected = [s for s in (statuses or []) if s in STATUSES]
    if statuses is not None:
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        query += f" AND status IN ({placeholders})"
        args.extend(selected)
    query += " ORDER BY created_at DESC"
    with _conn() as conn:
        rows = conn.execute(query, args).fetchall()
    return [_row(row) for row in rows]


def claim_pending(limit: int) -> list[dict]:
    limit = max(0, int(limit))
    if not limit:
        return []
    with _lock, _conn() as conn:
        # Take a write reservation before selecting so another process cannot
        # select the same pending ids between our SELECT and UPDATE.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM agent_inbox WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT ?", (limit,)
        ).fetchall()
        ids = [row["id"] for row in rows]
        for item_id in ids:
            conn.execute(
                "UPDATE agent_inbox SET status='processing' "
                "WHERE id=? AND status='pending'", (item_id,)
            )
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        claimed = conn.execute(
            f"SELECT * FROM agent_inbox WHERE id IN ({placeholders}) "
            "ORDER BY created_at ASC", ids
        ).fetchall()
    return [_row(row) for row in claimed]


def mark(agent_id: str, item_id: str, status: str, *, error: str | None = None) -> dict | None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    processed_at = _now() if status in ("processed", "failed") else None
    with _lock, _conn() as conn:
        if processed_at is None:
            conn.execute(
                "UPDATE agent_inbox SET status=?, last_error=COALESCE(?, last_error) "
                "WHERE agent_id=? AND id=?", (status, error, agent_id, item_id)
            )
        else:
            conn.execute(
                "UPDATE agent_inbox SET status=?, processed_at=?, "
                "last_error=COALESCE(?, last_error) WHERE agent_id=? AND id=?",
                (status, processed_at, error, agent_id, item_id),
            )
    return get(agent_id, item_id)


def bump_attempt(agent_id: str, item_id: str) -> int:
    """Increment an item's retry count and return the new value."""
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE agent_inbox SET attempts=attempts+1 WHERE agent_id=? AND id=?",
            (agent_id, item_id),
        )
        row = conn.execute(
            "SELECT attempts FROM agent_inbox WHERE agent_id=? AND id=?",
            (agent_id, item_id),
        ).fetchone()
    return int(row["attempts"]) if row else 0


def retry(agent_id: str, item_id: str) -> dict | None:
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE agent_inbox SET status='pending', attempts=0, last_error=NULL, "
            "processed_at=NULL WHERE agent_id=? AND id=?", (agent_id, item_id)
        )
    return get(agent_id, item_id)
