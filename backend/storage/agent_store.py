"""Local agents: a persona + tool/skill/model config, stored in app.db.

An agent is what the Chat page runs *as*: its ``persona`` becomes the system
prompt, its ``tools`` enable built-in tools (web_search/web_fetch), its
``skills`` attach uploaded skill packs (progressive disclosure via load_skill),
and ``model``/``temperature`` pin generation (model nullable -> active model).
``mcp_servers`` is reserved for Phase 2.

A default **"Assistant"** agent is seeded on first access so the existing chat
keeps working with no agent selected.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()

DEFAULT_AGENT_ID = "assistant"
_seeded = False

# Per-agent guardrails — the *tunable* bounds (the destructive/privileged shell
# deny-list is always enforced and is NOT configurable here). See shell_tool.py,
# loop.py, worker.py for where each is applied.
DEFAULT_GUARDRAILS = {
    "shell": "auto",        # "auto" (workspace if working folder, else read-only) | "off" | "readonly" | "workspace"
    "verify": True,         # run the verify pass when the worker marks a task done
    "maxIterations": 6,     # tool-loop iterations per task
    "dailyCap": 60,         # worker passes/day before auto-pause
    "extraDenied": [],      # additional shell commands to block (tighten only)
    "channelSend": False,   # may message people on connected chat channels (channel_send)
}


def _guardrails(raw) -> dict:
    """Merge stored guardrails over the defaults (older agents get defaults)."""
    try:
        stored = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        stored = {}
    g = {**DEFAULT_GUARDRAILS, **(stored if isinstance(stored, dict) else {})}
    # Coerce types defensively.
    g["shell"] = g["shell"] if g["shell"] in ("auto", "off", "readonly", "workspace") else "auto"
    g["verify"] = bool(g["verify"])
    g["maxIterations"] = max(1, min(int(g["maxIterations"] or 6), 50))
    g["dailyCap"] = max(1, min(int(g["dailyCap"] or 60), 1000))
    g["extraDenied"] = [str(x).strip() for x in (g["extraDenied"] or []) if str(x).strip()]
    g["channelSend"] = bool(g.get("channelSend"))
    return g


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v) -> float | None:
    """Coerce to float, or None (blank/invalid -> use the model default)."""
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            persona TEXT,
            model TEXT,
            temperature REAL NOT NULL DEFAULT 0.7,
            tools TEXT,          -- JSON: enabled built-in tool names
            skills TEXT,         -- JSON: attached skill slugs
            mcp_servers TEXT,    -- JSON: attached MCP server ids (Phase 2)
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Guarded migration: sampling params (nullable -> use the model's default).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
    if "top_p" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN top_p REAL")
    if "top_k" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN top_k INTEGER")
    # Guarded migration: working folder (grants the agent file tools when set).
    if "working_dir" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN working_dir TEXT")
    # Guarded migration: intern-employee fields (job title + duty state).
    if "role" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN role TEXT")
    if "duty_status" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN duty_status TEXT DEFAULT 'off'")
    # Guarded migration: per-agent guardrails (JSON; defaults applied on read).
    if "guardrails" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN guardrails TEXT")
    # Guarded migration: assigned custom-tool ids (JSON list).
    if "custom_tools" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN custom_tools TEXT")
    # Guarded migration: attached Knowledge Base ids (JSON; RAG via kb_search).
    if "knowledge_bases" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN knowledge_bases TEXT")
    return conn


def _row_to_agent(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"] or "",
        "persona": r["persona"] or "",
        "model": r["model"],
        "temperature": r["temperature"] if r["temperature"] is not None else 0.7,
        "topP": r["top_p"],
        "topK": r["top_k"],
        "workingDir": r["working_dir"] or "",
        "role": r["role"] or "",
        "dutyStatus": r["duty_status"] or "off",
        "guardrails": _guardrails(r["guardrails"] if "guardrails" in r.keys() else None),
        "tools": json.loads(r["tools"]) if r["tools"] else [],
        "skills": json.loads(r["skills"]) if r["skills"] else [],
        "mcpServers": json.loads(r["mcp_servers"]) if r["mcp_servers"] else [],
        "customTools": json.loads(r["custom_tools"]) if "custom_tools" in r.keys() and r["custom_tools"] else [],
        "knowledgeBases": json.loads(r["knowledge_bases"]) if "knowledge_bases" in r.keys() and r["knowledge_bases"] else [],
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def _ensure_seed() -> None:
    """Create the default Assistant agent once if the table is empty."""
    global _seeded
    if _seeded:
        return
    with _lock, _conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
        if n == 0:
            now = _now()
            conn.execute(
                "INSERT INTO agents (id, name, description, persona, model, temperature, "
                "top_p, top_k, working_dir, tools, skills, mcp_servers, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (DEFAULT_AGENT_ID, "Assistant",
                 "A helpful general-purpose assistant.",
                 "You are a helpful assistant running locally on the user's machine.",
                 None, 0.7, None, None, None, json.dumps([]), json.dumps([]), json.dumps([]), now, now),
            )
    _seeded = True


def list_agents() -> list[dict]:
    _ensure_seed()
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM agents ORDER BY updated_at DESC").fetchall()
    return [_row_to_agent(r) for r in rows]


def get_agent(agent_id: str) -> dict | None:
    _ensure_seed()
    with _conn() as conn:
        r = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return _row_to_agent(r) if r else None


def get_default_agent() -> dict | None:
    """The Assistant agent, or the most-recent agent if it was deleted."""
    agent = get_agent(DEFAULT_AGENT_ID)
    if agent:
        return agent
    agents = list_agents()
    return agents[0] if agents else None


def create_agent(data: dict) -> dict:
    _ensure_seed()
    now = _now()
    agent_id = data.get("id") or uuid.uuid4().hex[:12]
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO agents (id, name, description, persona, model, temperature, "
            "top_p, top_k, working_dir, role, duty_status, guardrails, tools, skills, "
            "mcp_servers, custom_tools, knowledge_bases, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (agent_id, data.get("name") or "Untitled agent", data.get("description") or "",
             data.get("persona") or "", data.get("model") or None,
             float(data.get("temperature", 0.7)), _num(data.get("topP")), _int(data.get("topK")),
             (data.get("workingDir") or "").strip() or None,
             (data.get("role") or "").strip() or None, data.get("dutyStatus") or "off",
             json.dumps(_guardrails(json.dumps(data.get("guardrails") or {}))),
             json.dumps(data.get("tools") or []), json.dumps(data.get("skills") or []),
             json.dumps(data.get("mcpServers") or []),
             json.dumps(data.get("customTools") or []),
             json.dumps(data.get("knowledgeBases") or []), now, now),
        )
    return get_agent(agent_id)  # type: ignore[return-value]


def update_agent(agent_id: str, data: dict) -> dict | None:
    existing = get_agent(agent_id)
    if not existing:
        return None
    merged = {**existing, **{k: v for k, v in data.items() if v is not None}}
    # Nullable sampling fields can be explicitly cleared back to the model default.
    for k in ("topP", "topK"):
        if k in data:
            merged[k] = data[k]
    # Guardrails: partial-merge incoming over existing, then normalize.
    guardrails = _guardrails(json.dumps({**existing["guardrails"], **(data.get("guardrails") or {})}))
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE agents SET name=?, description=?, persona=?, model=?, temperature=?, "
            "top_p=?, top_k=?, working_dir=?, role=?, duty_status=?, guardrails=?, tools=?, "
            "skills=?, mcp_servers=?, custom_tools=?, knowledge_bases=?, updated_at=? WHERE id=?",
            (merged["name"], merged.get("description") or "", merged.get("persona") or "",
             merged.get("model") or None, float(merged.get("temperature", 0.7)),
             _num(merged.get("topP")), _int(merged.get("topK")),
             (merged.get("workingDir") or "").strip() or None,
             (merged.get("role") or "").strip() or None,
             merged.get("dutyStatus") or "off", json.dumps(guardrails),
             json.dumps(merged.get("tools") or []), json.dumps(merged.get("skills") or []),
             json.dumps(merged.get("mcpServers") or []),
             json.dumps(merged.get("customTools") or []),
             json.dumps(merged.get("knowledgeBases") or []), now, agent_id),
        )
    return get_agent(agent_id)


def delete_agent(agent_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    return cur.rowcount > 0
