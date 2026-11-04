"""Activity — the intern's work journal, append-only, per-agent.

Every meaningful thing the intern does is recorded here: tool calls, file
changes, task moves, notes, review requests/resolutions, status changes, errors,
and end-of-day standups. The harness loop auto-logs tool activity; tasks log
their own transitions. This is a *higher-level* feed than the raw ``debug_db``
LLM traces (which stay for call inspection) — an activity row links out to a
trace when useful via ``detail``.

Scoped to ``agent_id``, optionally tied to a ``task_id``. See
docs/INTERN_AGENT_PRD.md §8.2.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()

TYPES = (
    "task_created", "task_moved", "task_claimed", "tool_call", "file_change",
    "note", "verify", "status_change", "error", "standup", "delegation",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            task_id TEXT,
            type TEXT NOT NULL,
            summary TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_agent ON activities(agent_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_task ON activities(task_id)")
    return conn


def _row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "agentId": r["agent_id"],
        "taskId": r["task_id"],
        "type": r["type"],
        "summary": r["summary"],
        "detail": json.loads(r["detail"]) if r["detail"] else None,
        "createdAt": r["created_at"],
    }


def log(agent_id: str, type: str, summary: str,
        task_id: str | None = None, detail: dict | None = None) -> dict:
    """Append one activity row. Best-effort callers should wrap in try/except —
    journaling must never break the work it's recording."""
    now = _now()
    aid = uuid.uuid4().hex[:12]
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO activities (id, agent_id, task_id, type, summary, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (aid, agent_id, task_id, type if type in TYPES else "note",
             (summary or "").strip()[:500], json.dumps(detail) if detail else None, now),
        )
    return {"id": aid, "agentId": agent_id, "taskId": task_id, "type": type,
            "summary": summary, "detail": detail, "createdAt": now}


def list_for(agent_id: str, *, day: str | None = None, type: str | None = None,
             task_id: str | None = None, limit: int = 200) -> list[dict]:
    """Recent-first feed. ``day`` is an ISO date prefix (YYYY-MM-DD) matched
    against the UTC timestamp."""
    q = "SELECT * FROM activities WHERE agent_id=?"
    args: list = [agent_id]
    if task_id:
        q += " AND task_id=?"; args.append(task_id)
    if type:
        q += " AND type=?"; args.append(type)
    if day:
        q += " AND created_at LIKE ?"; args.append(f"{day}%")
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row(r) for r in rows]


def recent_across_agents(*, type: str | None = None, limit: int = 100) -> list[dict]:
    """Recent-first feed spanning *all* agents — powers the combined team view.
    Optionally filtered to one ``type`` (e.g. 'delegation')."""
    q = "SELECT * FROM activities"
    args: list = []
    if type:
        q += " WHERE type=?"; args.append(type)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row(r) for r in rows]


def since(agent_id: str, iso_ts: str, limit: int = 500) -> list[dict]:
    """Activities at/after ``iso_ts`` (oldest-first) — for the standup window."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activities WHERE agent_id=? AND created_at>=? "
            "ORDER BY created_at ASC LIMIT ?",
            (agent_id, iso_ts, max(1, int(limit)))).fetchall()
    return [_row(r) for r in rows]
