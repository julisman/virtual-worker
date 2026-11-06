"""Tasks — the intern's Kanban board, stored per-agent in app.db.

A task is a unit of assigned work with a lifecycle:

    backlog → todo → in_progress → ┬→ review → done
                                   ├→ blocked → (todo|in_progress)
                                   └→ cancelled

Both the mentor (via the UI) and the intern (via tools) move tasks across the
board. Each task carries an ordered comment thread — the mentor↔intern
conversation — and a ``result_summary`` the intern fills when it finishes.

Scoped to ``agent_id`` (one board per intern). See docs/INTERN_AGENT_PRD.md.
"""

from __future__ import annotations

import json
import sqlite3
from storage import _sqlite
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()

STATUSES = ("backlog", "todo", "in_progress", "blocked", "review", "done", "cancelled")
PRIORITIES = ("low", "medium", "high", "urgent")
_PRIORITY_RANK = {p: i for i, p in enumerate(("urgent", "high", "medium", "low"))}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'todo',
            priority TEXT NOT NULL DEFAULT 'medium',
            order_key REAL NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT 'mentor',   -- 'mentor' | 'intern' | 'agent'
            result_summary TEXT,
            metadata TEXT,
            due_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id, status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_comments (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            author TEXT NOT NULL,                    -- 'mentor' | 'intern'
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id)")
    # Guarded migration: definition of done — the acceptance criteria the agent
    # must satisfy before a task can be marked done (checked by the Verify pillar).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "criteria" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN criteria TEXT")
    conn.mark_initialized()
    return conn


def _row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "agentId": r["agent_id"],
        "title": r["title"],
        "description": r["description"] or "",
        "criteria": (r["criteria"] if "criteria" in r.keys() else None) or "",
        "status": r["status"],
        "priority": r["priority"],
        "orderKey": r["order_key"],
        "origin": r["origin"],
        "resultSummary": r["result_summary"] or "",
        "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
        "dueAt": r["due_at"],
        "startedAt": r["started_at"],
        "completedAt": r["completed_at"],
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def _comment_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "taskId": r["task_id"], "author": r["author"],
            "text": r["text"], "createdAt": r["created_at"]}


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def create(agent_id: str, data: dict, origin: str = "mentor") -> dict:
    now = _now()
    task_id = uuid.uuid4().hex[:12]
    status = data.get("status") if data.get("status") in STATUSES else "todo"
    priority = data.get("priority") if data.get("priority") in PRIORITIES else "medium"
    started = now if status == "in_progress" else None
    with _lock, _conn() as conn:
        # New tasks sort to the top of their column.
        row = conn.execute(
            "SELECT COALESCE(MIN(order_key), 0) AS m FROM tasks WHERE agent_id=? AND status=?",
            (agent_id, status)).fetchone()
        order_key = (row["m"] or 0) - 1.0
        conn.execute(
            "INSERT INTO tasks (id, agent_id, title, description, criteria, status, priority, order_key, "
            "origin, result_summary, metadata, due_at, started_at, completed_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, agent_id, (data.get("title") or "Untitled task").strip(),
             (data.get("description") or "").strip(), (data.get("criteria") or "").strip() or None,
             status, priority, order_key,
             origin if origin in ("mentor", "intern", "agent") else "mentor",
             (data.get("resultSummary") or "").strip() or None,
             json.dumps(data.get("metadata") or {}), data.get("dueAt") or None,
             started, None, now, now),
        )
    return get(agent_id, task_id)  # type: ignore[return-value]


def get(agent_id: str, task_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE agent_id=? AND id=?", (agent_id, task_id)).fetchone()
    return _row(r) if r else None


def list_for(agent_id: str, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM tasks WHERE agent_id=?"
    args: list = [agent_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY order_key ASC, created_at ASC"
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row(r) for r in rows]


def update(agent_id: str, task_id: str, data: dict) -> dict | None:
    existing = get(agent_id, task_id)
    if not existing:
        return None
    now = _now()
    fields: list[str] = []
    args: list = []

    def setf(col: str, val) -> None:
        fields.append(f"{col}=?")
        args.append(val)

    if "title" in data and data["title"]:
        setf("title", data["title"].strip())
    if "description" in data:
        setf("description", (data["description"] or "").strip())
    if "criteria" in data:
        setf("criteria", (data["criteria"] or "").strip() or None)
    if "priority" in data and data["priority"] in PRIORITIES:
        setf("priority", data["priority"])
    if "orderKey" in data and data["orderKey"] is not None:
        setf("order_key", float(data["orderKey"]))
    if "resultSummary" in data:
        setf("result_summary", (data["resultSummary"] or "").strip() or None)
    if "dueAt" in data:
        setf("due_at", data["dueAt"] or None)
    if "metadata" in data:
        setf("metadata", json.dumps(data["metadata"] or {}))

    if "status" in data and data["status"] in STATUSES:
        new_status = data["status"]
        setf("status", new_status)
        # Timestamp side-effects of entering a lifecycle stage.
        if new_status == "in_progress" and not existing["startedAt"]:
            setf("started_at", now)
        if new_status in ("done", "cancelled"):
            setf("completed_at", now)
        elif existing["completedAt"] and new_status not in ("done", "cancelled"):
            setf("completed_at", None)  # reopened

    if not fields:
        return existing
    setf("updated_at", now)
    args.extend([agent_id, task_id])
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE agent_id=? AND id=?", args)
    return get(agent_id, task_id)


def delete(agent_id: str, task_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE agent_id=? AND id=?", (agent_id, task_id))
        conn.execute("DELETE FROM task_comments WHERE task_id=?", (task_id,))
    return cur.rowcount > 0


def next_todo(agent_id: str) -> dict | None:
    """The task the always-on worker should pick up next: highest priority,
    then oldest. (Used by the M3 worker; safe to call now.)"""
    todos = list_for(agent_id, status="todo")
    if not todos:
        return None
    todos.sort(key=lambda t: (_PRIORITY_RANK.get(t["priority"], 9), t["createdAt"]))
    return todos[0]


def counts(agent_id: str) -> dict:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM tasks WHERE agent_id=? GROUP BY status",
            (agent_id,)).fetchall()
    return {r["status"]: r["c"] for r in rows}


# --------------------------------------------------------------------------- #
# Comments (the per-task mentor↔intern thread)
# --------------------------------------------------------------------------- #

def add_comment(task_id: str, author: str, text: str) -> dict:
    now = _now()
    cid = uuid.uuid4().hex[:12]
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, text, created_at) VALUES (?,?,?,?,?)",
            (cid, task_id, author if author in ("mentor", "intern") else "mentor", text.strip(), now))
        r = conn.execute("SELECT * FROM task_comments WHERE id=?", (cid,)).fetchone()
    return _comment_row(r)


def list_comments(task_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at ASC", (task_id,)).fetchall()
    return [_comment_row(r) for r in rows]
