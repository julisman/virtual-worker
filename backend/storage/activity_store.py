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
from datetime import datetime, timedelta, timezone

from config import db_path, ensure_dir
from storage import _sqlite

_lock = threading.Lock()

TYPES = (
    "task_created", "task_moved", "task_claimed", "tool_call", "file_change",
    "note", "verify", "status_change", "error", "standup", "delegation",
    "config_changed",
)


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
        conn.mark_initialized()
        return conn
    except BaseException:
        conn.close()
        raise


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


def prune(*, max_age_days: int | None = None, max_rows: int | None = None) -> int:
    """Bound the append-only activity feed. Delete rows older than
    ``max_age_days``, then, if still over ``max_rows``, drop the oldest beyond
    that global ceiling. 0/None disables a bound. Returns rows deleted."""
    deleted = 0
    with _lock, _conn() as conn:
        if max_age_days and max_age_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            deleted += conn.execute(
                "DELETE FROM activities WHERE created_at < ?", (cutoff,)).rowcount
        if max_rows and max_rows > 0:
            deleted += conn.execute(
                "DELETE FROM activities WHERE id NOT IN "
                "(SELECT id FROM activities ORDER BY created_at DESC LIMIT ?)",
                (max_rows,)).rowcount
    return deleted


def count_for(agent_id: str, *, day: str | None = None, type: str | None = None) -> int:
    """Count matching activities (used to recover the worker's daily pass count
    across a restart, since activities are the persisted source of truth)."""
    q = "SELECT COUNT(*) AS n FROM activities WHERE agent_id=?"
    args: list = [agent_id]
    if type:
        q += " AND type=?"; args.append(type)
    if day:
        q += " AND created_at LIKE ?"; args.append(f"{day}%")
    with _conn() as conn:
        row = conn.execute(q, args).fetchone()
    return int(row["n"]) if row else 0


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
