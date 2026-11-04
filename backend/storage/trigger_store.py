"""Event-driven triggers — fire an agent when something happens (PRD-09, M1).

Triggers are the event-shaped sibling of schedules (storage/schedule_store.py).
Where a schedule fires on a clock, a trigger fires on an *event*:

  • ``file``    — a watched folder gains a new/changed file (polled, mtime-based)
  • ``webhook`` — an inbound ``POST /api/triggers/hook/{token}`` arrives

Each trigger points at one agent + a ``prompt_template`` and, when it fires, runs
that prompt through the same ``compose()`` + ``loop.run`` path the scheduler and
chat use (see agent/triggers.py). Output is recorded on the trigger and logged to
the agent's activity feed — there is no auto-delivery target, so a trigger can't
leak to a frozen recipient (same safety posture as schedule_store).

Local-first & opt-in: file watching runs fully offline; the webhook endpoint is
the only inbound network surface and is guarded by an unguessable per-trigger
token. Deferred to later milestones (noted, not built here): email-in,
monitor-threshold, chat-rule triggers, and multi-step workflow chaining.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime

from config import db_path, ensure_dir

_lock = threading.Lock()

TYPES = ("file", "webhook")
_RESULT_CAP = 4000


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS triggers (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,                      -- 'file' | 'webhook'
            config TEXT,                             -- JSON: {path} | {hookToken}
            prompt_template TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            watch_state TEXT,                        -- JSON snapshot for file dedup
            hook_token TEXT,                         -- indexed lookup for webhooks
            last_fired_at TEXT,
            last_status TEXT,                        -- 'ok' | 'error'
            last_result TEXT,
            last_error TEXT,
            last_event TEXT,                         -- short summary of last event
            fire_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_triggers_agent ON triggers(agent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_triggers_hook ON triggers(hook_token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_triggers_enabled ON triggers(enabled, type)")
    return conn


def _row(r: sqlite3.Row) -> dict:
    config = json.loads(r["config"]) if r["config"] else {}
    return {
        "id": r["id"],
        "agentId": r["agent_id"],
        "name": r["name"],
        "type": r["type"],
        "config": config,
        "path": config.get("path"),
        "hookToken": r["hook_token"],
        "promptTemplate": r["prompt_template"],
        "enabled": bool(r["enabled"]),
        "lastFiredAt": r["last_fired_at"],
        "lastStatus": r["last_status"],
        "lastResult": r["last_result"],
        "lastError": r["last_error"],
        "lastEvent": r["last_event"],
        "fireCount": r["fire_count"],
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


# --------------------------------------------------------------------------- #
# File-watch signatures
# --------------------------------------------------------------------------- #

def snapshot_dir(path: str) -> dict[str, str]:
    """A {relpath: "mtime:size"} fingerprint of a watched path's files.

    Used to detect new/modified files between polls without an OS watcher.
    A missing path yields an empty snapshot (nothing to fire). Recurses one
    directory deep into subfolders; symlinks and unreadable entries are skipped.
    """
    sig: dict[str, str] = {}
    if not path:
        return sig
    try:
        if os.path.isfile(path):
            st = os.stat(path)
            sig[os.path.abspath(path)] = f"{st.st_mtime_ns}:{st.st_size}"
            return sig
        for root, _dirs, files in os.walk(path):
            for name in files:
                if name.startswith("."):
                    continue
                fp = os.path.join(root, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                sig[fp] = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return sig
    return sig


def diff_snapshot(prev: dict[str, str], cur: dict[str, str]) -> list[str]:
    """Files that are new or whose fingerprint changed since ``prev``."""
    return sorted(fp for fp, sig in cur.items() if prev.get(fp) != sig)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def create(agent_id: str, name: str, type: str, prompt_template: str, *,
           path: str | None = None) -> dict:
    if type not in TYPES:
        raise ValueError(f"type must be one of {TYPES}")
    if not (prompt_template or "").strip():
        raise ValueError("prompt_template is required")

    config: dict = {}
    hook_token: str | None = None
    watch_state: str | None = None

    if type == "file":
        if not (path or "").strip():
            raise ValueError("a folder/file 'path' is required for a file trigger")
        path = path.strip()
        config = {"path": path}
        # Baseline now so existing files don't all fire on the first poll —
        # only files added/changed *after* creation are events.
        watch_state = json.dumps(snapshot_dir(path))
    else:  # webhook
        hook_token = uuid.uuid4().hex
        config = {"hookToken": hook_token}

    tid = uuid.uuid4().hex[:12]
    now = _now_iso()
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO triggers
               (id, agent_id, name, type, config, prompt_template, enabled,
                watch_state, hook_token, fire_count, created_at, updated_at)
               VALUES (?,?,?,?,?,?,1,?,?,0,?,?)""",
            (tid, agent_id, name.strip() or "Untitled", type, json.dumps(config),
             prompt_template.strip(), watch_state, hook_token, now, now),
        )
    return get(tid)


def get(trigger_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
    return _row(r) if r else None


def get_by_token(token: str) -> dict | None:
    if not token:
        return None
    with _conn() as conn:
        r = conn.execute("SELECT * FROM triggers WHERE hook_token=?", (token,)).fetchone()
    return _row(r) if r else None


def list_for_agent(agent_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE agent_id=? ORDER BY created_at DESC", (agent_id,)
        ).fetchall()
    return [_row(r) for r in rows]


def list_enabled(type: str | None = None) -> list[dict]:
    """Enabled triggers (optionally of one type) — drives the polling loop."""
    q = "SELECT * FROM triggers WHERE enabled=1"
    args: list = []
    if type:
        q += " AND type=?"
        args.append(type)
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row(r) for r in rows]


def update(trigger_id: str, **fields) -> dict | None:
    cur = get(trigger_id)
    if not cur:
        return None

    name = fields.get("name", cur["name"])
    prompt = fields.get("promptTemplate", cur["promptTemplate"])
    enabled = fields.get("enabled", cur["enabled"])
    path = fields.get("path", cur["path"])

    config = dict(cur["config"])
    watch_state_sql = None  # only rewrite when the watched path changes
    if cur["type"] == "file":
        if not (path or "").strip():
            raise ValueError("a folder/file 'path' is required for a file trigger")
        path = path.strip()
        config["path"] = path
        if path != cur["path"]:
            # New path → re-baseline so we don't fire on its pre-existing files.
            watch_state_sql = json.dumps(snapshot_dir(path))

    with _lock, _conn() as conn:
        if watch_state_sql is not None:
            conn.execute(
                """UPDATE triggers SET name=?, prompt_template=?, enabled=?, config=?,
                   watch_state=?, updated_at=? WHERE id=?""",
                (name.strip() or "Untitled", (prompt or "").strip(), 1 if enabled else 0,
                 json.dumps(config), watch_state_sql, _now_iso(), trigger_id),
            )
        else:
            conn.execute(
                """UPDATE triggers SET name=?, prompt_template=?, enabled=?, config=?,
                   updated_at=? WHERE id=?""",
                (name.strip() or "Untitled", (prompt or "").strip(), 1 if enabled else 0,
                 json.dumps(config), _now_iso(), trigger_id),
            )
    return get(trigger_id)


def delete(trigger_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM triggers WHERE id=?", (trigger_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Watch state + fire recording
# --------------------------------------------------------------------------- #

def get_watch_state(trigger_id: str) -> dict[str, str]:
    with _conn() as conn:
        r = conn.execute("SELECT watch_state FROM triggers WHERE id=?", (trigger_id,)).fetchone()
    if not r or not r["watch_state"]:
        return {}
    try:
        return json.loads(r["watch_state"])
    except (ValueError, TypeError):
        return {}


def set_watch_state(trigger_id: str, state: dict[str, str]) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE triggers SET watch_state=? WHERE id=?",
            (json.dumps(state), trigger_id),
        )


def record_fire(trigger_id: str, status: str, *, result: str = "",
                error: str = "", event: str = "") -> dict | None:
    """Store a fire's outcome and bump the fire counter."""
    cur = get(trigger_id)
    if not cur:
        return None
    with _lock, _conn() as conn:
        conn.execute(
            """UPDATE triggers SET last_fired_at=?, last_status=?, last_result=?,
               last_error=?, last_event=?, fire_count=fire_count+1, updated_at=? WHERE id=?""",
            (_now_iso(), status, (result or "")[:_RESULT_CAP], (error or "")[:_RESULT_CAP],
             (event or "")[:500], _now_iso(), trigger_id),
        )
    return get(trigger_id)
