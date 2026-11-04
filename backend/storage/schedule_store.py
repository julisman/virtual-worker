"""Scheduled agent runs — "do X at 2pm", stored per-agent in app.db.

A schedule fires a prompt to an agent at a time. Two kinds:

  • ``cron`` — recurring, a 5-field cron expression (see agent/cron.py)
  • ``at``   — one-time, a local datetime; the schedule disables itself after it runs

Times are evaluated in the machine's **local time** (this is a local-first,
single-user app, so "5pm" means the user's 5pm). ``next_run_at`` is precomputed
so the scheduler's tick is a cheap "is anything due?" scan. When a run finishes
we record its status + a short result snippet for the UI, then advance
``next_run_at`` (or disable a one-time schedule).

Unlike the engine's channel-delivering cron, a fired schedule here just *runs the
agent* — there is no frozen delivery target, so the recurring-digest leak that
bit the engine (PR #648) can't happen. If a schedule should notify someone, the
agent does it with its own tools at run time.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime

from agent import cron
from config import db_path, ensure_dir

_lock = threading.Lock()

KINDS = ("cron", "at")
_RESULT_CAP = 4000


def _now_local() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'cron',       -- 'cron' | 'at'
            cron_expr TEXT,                          -- when kind='cron'
            run_at TEXT,                             -- local ISO, when kind='at'
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at TEXT,                        -- local ISO; NULL = nothing scheduled
            last_run_at TEXT,
            last_status TEXT,                        -- 'ok' | 'error'
            last_result TEXT,
            last_error TEXT,
            run_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_agent ON schedules(agent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at)")
    # Guarded migration (PRD-01): an explicit per-schedule delivery target list.
    # JSON array of {channel, target}; resolved at *send* time, never a captured
    # recipient. Empty/absent = today's "Activity only" behavior.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(schedules)").fetchall()}
    if "deliver_to_json" not in cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN deliver_to_json TEXT")
    return conn


def _deliver_to(value) -> list[dict]:
    """Normalize a deliver_to value to a clean list of {channel, target} dicts."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else []
        except (ValueError, TypeError):
            return []
    out: list[dict] = []
    for item in (value or []):
        if not isinstance(item, dict):
            continue
        ch = (item.get("channel") or "").strip()
        if not ch:
            continue
        out.append({"channel": ch, "target": (item.get("target") or "").strip()})
    return out


def _row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "agentId": r["agent_id"],
        "title": r["title"],
        "prompt": r["prompt"],
        "kind": r["kind"],
        "cron": r["cron_expr"],
        "cronText": cron.describe(r["cron_expr"]) if r["cron_expr"] else None,
        "runAt": r["run_at"],
        "enabled": bool(r["enabled"]),
        "nextRunAt": r["next_run_at"],
        "lastRunAt": r["last_run_at"],
        "lastStatus": r["last_status"],
        "lastResult": r["last_result"],
        "lastError": r["last_error"],
        "runCount": r["run_count"],
        "deliverTo": _deliver_to(r["deliver_to_json"] if "deliver_to_json" in r.keys() else None),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def _compute_next(kind: str, cron_expr: str | None, run_at: str | None,
                  after: datetime | None = None) -> str | None:
    """Next local fire time as ISO, or None if nothing is pending."""
    after = after or _now_local()
    if kind == "cron" and cron_expr:
        nxt = cron.next_run(cron_expr, after)
        return nxt.isoformat(timespec="seconds") if nxt else None
    if kind == "at" and run_at:
        # Fire at run_at; a time already in the past fires once on the next tick
        # (catch-up), then record_run disables it.
        return run_at
    return None


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def create(agent_id: str, title: str, prompt: str, *, kind: str = "cron",
           cron_expr: str | None = None, run_at: str | None = None,
           deliver_to: list | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if kind == "cron":
        if not cron_expr or not cron.validate(cron_expr):
            raise ValueError("a valid 5-field cron expression is required")
        run_at = None
    else:  # at
        if not run_at:
            raise ValueError("run_at (local ISO datetime) is required for a one-time schedule")
        # normalize / validate
        run_at = datetime.fromisoformat(run_at).replace(second=0, microsecond=0).isoformat(timespec="seconds")
        cron_expr = None

    sid = uuid.uuid4().hex[:12]
    now = _now_iso()
    nxt = _compute_next(kind, cron_expr, run_at)
    deliver_json = json.dumps(_deliver_to(deliver_to))
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO schedules
               (id, agent_id, title, prompt, kind, cron_expr, run_at, enabled,
                next_run_at, run_count, deliver_to_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,1,?,0,?,?,?)""",
            (sid, agent_id, title.strip() or "Untitled", prompt.strip(), kind,
             cron_expr, run_at, nxt, deliver_json, now, now),
        )
    return get(sid)


def get(schedule_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    return _row(r) if r else None


def list_for_agent(agent_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE agent_id=? ORDER BY created_at DESC", (agent_id,)
        ).fetchall()
    return [_row(r) for r in rows]


def list_due(now: datetime | None = None) -> list[dict]:
    """Enabled schedules whose next_run_at has arrived (local time)."""
    cutoff = (now or _now_local()).isoformat(timespec="seconds")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at ASC", (cutoff,),
        ).fetchall()
    return [_row(r) for r in rows]


def update(schedule_id: str, **fields) -> dict | None:
    cur = get(schedule_id)
    if not cur:
        return None

    title = fields.get("title", cur["title"])
    prompt = fields.get("prompt", cur["prompt"])
    enabled = fields.get("enabled", cur["enabled"])
    kind = fields.get("kind", cur["kind"])
    cron_expr = fields.get("cron", cur["cron"])
    run_at = fields.get("runAt", cur["runAt"])
    deliver_to = _deliver_to(fields.get("deliverTo", cur["deliverTo"]))

    if kind == "cron":
        if not cron_expr or not cron.validate(cron_expr):
            raise ValueError("a valid 5-field cron expression is required")
        run_at = None
    elif kind == "at":
        if not run_at:
            raise ValueError("run_at is required for a one-time schedule")
        run_at = datetime.fromisoformat(run_at).replace(second=0, microsecond=0).isoformat(timespec="seconds")
        cron_expr = None

    nxt = _compute_next(kind, cron_expr, run_at) if enabled else None
    with _lock, _conn() as conn:
        conn.execute(
            """UPDATE schedules SET title=?, prompt=?, enabled=?, kind=?, cron_expr=?,
               run_at=?, next_run_at=?, deliver_to_json=?, updated_at=? WHERE id=?""",
            (title.strip() or "Untitled", prompt.strip(), 1 if enabled else 0, kind,
             cron_expr, run_at, nxt, json.dumps(deliver_to), _now_iso(), schedule_id),
        )
    return get(schedule_id)


def delete(schedule_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        return cur.rowcount > 0


def record_run(schedule_id: str, status: str, result: str = "", error: str = "") -> dict | None:
    """Store a run's outcome and advance the schedule (or disable a one-time one)."""
    cur = get(schedule_id)
    if not cur:
        return None
    now = _now_local()
    if cur["kind"] == "cron" and cur["cron"]:
        nxt = _compute_next("cron", cur["cron"], None, after=now)
        enabled = cur["enabled"]
    else:  # one-time: done
        nxt = None
        enabled = False
    with _lock, _conn() as conn:
        conn.execute(
            """UPDATE schedules SET last_run_at=?, last_status=?, last_result=?, last_error=?,
               run_count=run_count+1, next_run_at=?, enabled=?, updated_at=? WHERE id=?""",
            (now.isoformat(timespec="seconds"), status, (result or "")[:_RESULT_CAP],
             (error or "")[:_RESULT_CAP], nxt, 1 if enabled else 0, _now_iso(), schedule_id),
        )
    return get(schedule_id)
