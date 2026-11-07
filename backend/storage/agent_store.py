"""Local agents: a persona + tool/skill/model config, stored in app.db.

An agent is what the Chat page runs *as*: its ``persona`` becomes the system
prompt, its ``tools`` enable built-in tools (browser_search/browser_fetch), its
``skills`` attach uploaded skill packs (progressive disclosure via load_skill),
and ``model``/``temperature`` pin generation (model nullable -> active model).
``mcp_servers`` is reserved for Phase 2.

A default **"Assistant"** agent is seeded on first access so the existing chat
keeps working with no agent selected.
"""

from __future__ import annotations

import json
import sqlite3
from storage import _sqlite
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir

# Reentrant so a write that needs a consistent read+merge+write (update_agent)
# can hold the lock across a nested get_agent without deadlocking.
_lock = threading.RLock()

DEFAULT_AGENT_ID = "assistant"
_seeded = False
_seeded_path: str | None = None

# Per-agent guardrails — the *tunable* bounds (the destructive/privileged shell
# deny-list is always enforced and is NOT configurable here). See shell_tool.py,
# loop.py, worker.py for where each is applied.
DEFAULT_GUARDRAILS = {
    "shell": "auto",        # "auto" (workspace if working folder, else read-only) | "off" | "readonly" | "workspace"
    "verify": True,         # run the verify pass when the worker marks a task done
    "maxIterations": 0,     # tool-loop iterations per task; 0 = unlimited (set a number to cap)
    "wrapupWarnAt": 2,      # steps before the cap to tell the agent to wrap up and answer; 0 = no pre-warn
    "dailyCap": 60,         # task executions/day before auto-pause (idle polls don't count)
    "extraDenied": [],      # additional shell commands to block (tighten only)
    "allowedCommands": [],  # operator allow-list: named programs bypass the tier + deny-list (opt-in loosen)
    "tightContext": False,  # tighter tool-output + context budgets → faster (esp. local)
    "shellTimeoutS": 60,    # per-command shell kill timeout in seconds (clamped 10–600)
    "contextChars": 0,      # conversation char budget; 0 = auto (sized to the model's provider)
    "autoCompact": False,   # fold older turns into an LLM recap instead of eliding them
    "compactAt": 80,        # compact when the convo passes this % of the budget
    "mcpToolResultChars": 40000,  # cap on MCP tool results fed back to the model (big JSON)
    "wikiReadChars": 8000,  # cap on a wiki_read result fed back to the model (a full wiki page)
    "skillFactory": False,  # let the agent author its own installed skills (PRD-14) — off by default
    # PRD-23 context-window tracking. "chars" keeps today's char-proxy budget;
    # "window" meters real tokens (server usage + chars/4 tail) against the model's
    # resolved context window. reserveTokens/contextTokens are window-mode knobs
    # (0 = auto). compactKeep selects how the recap's verbatim tail is cut.
    "maxOutputTokens": 0,   # per-turn output budget (thinking + answer); 0 = auto (16k for remote reasoning models, else 3072)
    "contextMode": "chars",  # "chars" (default) | "window"
    "reserveTokens": 0,      # window mode output reserve; 0 = auto floor
    "contextTokens": 0,      # window override; 0 = auto-detect the model's window
    "compactKeep": "messages",  # "messages" (default) | "size" — recap tail cut
    "compactKeepAmount": 0,  # messages or tokens per compactKeep; 0 = auto
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
    # 0 (or anything non-positive/garbage) = unlimited; positive caps at 1000.
    try:
        _mi = int(g["maxIterations"])
    except (TypeError, ValueError):
        _mi = 0
    g["maxIterations"] = 0 if _mi <= 0 else min(_mi, 1000)
    # Steps-before-cap pre-warn: garbage → default 2, clamped to [0, 100]; 0 = off.
    try:
        _ww = int(g["wrapupWarnAt"])
    except (TypeError, ValueError):
        _ww = 2
    g["wrapupWarnAt"] = max(0, min(_ww, 100))
    g["dailyCap"] = max(1, min(int(g["dailyCap"] or 60), 1000))
    g["extraDenied"] = [str(x).strip() for x in (g["extraDenied"] or []) if str(x).strip()]
    g["allowedCommands"] = [str(x).strip() for x in (g.get("allowedCommands") or []) if str(x).strip()]
    g["tightContext"] = bool(g.get("tightContext"))
    # Shell kill timeout: garbage → 60, clamped to [10, 600] seconds.
    try:
        _st = int(g.get("shellTimeoutS") or 60)
    except (TypeError, ValueError):
        _st = 60
    g["shellTimeoutS"] = max(10, min(_st, 600))
    # Context char budget: 0 (or negative/garbage) = auto; else clamp to [8000, 2M].
    try:
        _cc = int(g.get("contextChars") or 0)
    except (TypeError, ValueError):
        _cc = 0
    g["contextChars"] = 0 if _cc <= 0 else max(8000, min(_cc, 2_000_000))
    g["autoCompact"] = bool(g.get("autoCompact"))
    # Compact trigger percent: garbage → 80, clamped to [30, 95].
    try:
        _ca = int(g["compactAt"])
    except (TypeError, ValueError):
        _ca = 80
    g["compactAt"] = max(30, min(_ca, 95))
    # Per-turn output token budget: 0 (or garbage) = auto; else clamp [256, 131072].
    try:
        _mo = int(g.get("maxOutputTokens") or 0)
    except (TypeError, ValueError):
        _mo = 0
    g["maxOutputTokens"] = 0 if _mo <= 0 else max(256, min(_mo, 131072))
    g["mcpToolResultChars"] = max(2000, min(int(g.get("mcpToolResultChars") or 40000), 500000))
    g["wikiReadChars"] = max(1000, min(int(g.get("wikiReadChars") or 8000), 200000))
    g["skillFactory"] = bool(g.get("skillFactory"))
    g["channelSend"] = bool(g.get("channelSend"))
    # PRD-23: context mode + window-mode knobs. Bogus values fall back to the
    # defaults, like the fields above.
    g["contextMode"] = g.get("contextMode") if g.get("contextMode") in ("chars", "window") else "chars"
    try:
        _rt = int(g.get("reserveTokens") or 0)
    except (TypeError, ValueError):
        _rt = 0
    g["reserveTokens"] = max(0, _rt)
    try:
        _ct = int(g.get("contextTokens") or 0)
    except (TypeError, ValueError):
        _ct = 0
    g["contextTokens"] = max(0, _ct)
    g["compactKeep"] = g.get("compactKeep") if g.get("compactKeep") in ("messages", "size") else "messages"
    try:
        _cka = int(g.get("compactKeepAmount") or 0)
    except (TypeError, ValueError):
        _cka = 0
    g["compactKeepAmount"] = max(0, _cka)
    return g


# Per-agent web-search backend for the browser_search tool. provider picks the
# engine; apiKey is for tavily/brave; searxngUrl overrides the global
# VW_SEARXNG_URL for the searxng provider. See agent/tools.py:make_search_tool.
DEFAULT_WEB_SEARCH = {"provider": "searxng", "apiKey": "", "searxngUrl": ""}
_WEB_SEARCH_PROVIDERS = ("searxng", "tavily", "brave", "duckduckgo")


def _web_search(raw) -> dict:
    """Merge stored web-search settings over the defaults (older agents get defaults)."""
    try:
        stored = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        stored = {}
    w = {**DEFAULT_WEB_SEARCH, **(stored if isinstance(stored, dict) else {})}
    w["provider"] = w["provider"] if w["provider"] in _WEB_SEARCH_PROVIDERS else "searxng"
    w["apiKey"] = str(w.get("apiKey") or "").strip()
    w["searxngUrl"] = str(w.get("searxngUrl") or "").strip().rstrip("/")
    return w


_MODEL_SOURCES = ("local", "byok", "subscription")


def _source(v) -> str:
    """Normalize the per-agent model source (unknown/blank -> local)."""
    v = (v or "local") if isinstance(v, str) else "local"
    return v if v in _MODEL_SOURCES else "local"


# "off" is an explicit level (thinking disabled on the wire); "" = unset means
# the model's native default — the two are different for reasoning models that
# think unless told otherwise (see docs/prd/PRD-25-provider-thinking-compat.md).
_REASONING_EFFORTS = ("off", "low", "medium", "high", "xhigh")


def _effort(v) -> str:
    """Normalize the per-agent reasoning effort ("" / unknown -> "" = unset)."""
    v = v.strip() if isinstance(v, str) else ""
    return v if v in _REASONING_EFFORTS else ""


def _mcp_tool_allow(raw) -> dict:
    """Normalize the per-agent, per-server MCP tool selection.

    Shape: ``{serverId: [toolName, ...]}``. A server id absent from the map
    means ALL tools from that server (so existing agents are unaffected and
    newly-advertised tools appear automatically); a present key lists exactly
    the exposed tools — including a present-but-empty list, which means NONE
    (the UI's "uncheck all" state). Keep only string keys whose value is a
    list; anything invalid normalizes to ``{}``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, list):
            continue
        out[k] = [str(x).strip() for x in v if str(x).strip()]
    return out


def _connector_grants(raw) -> dict:
    """Normalize the per-agent, per-connector write/merge grant.

    Shape: ``{connectorName: {"write": bool, "merge": bool}}``. A connector
    absent from the map (or a missing key) means the grant is off, so an agent
    only ever gets writes/merge for a connector it was explicitly granted. Keep
    only string keys with a dict value; coerce ``write``/``merge`` to bool.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        out[k] = {"write": bool(v.get("write")), "merge": bool(v.get("merge"))}
    return out


def _ask_budget(raw) -> int | None:
    """Normalize the per-agent ask budget (clamp 0..20; None = use the default)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(20, n))


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


# DB paths whose schema has been created/migrated this process, so the DDL +
# PRAGMA table_info + guarded ALTERs run once per db file instead of on every
# _conn() (the worker calls get_agent every loop iteration). Keyed by path so a
# fresh test db (monkeypatched APP_HOME) still migrates.
_migrated_paths: set[str] = set()


def _migrate(conn: sqlite3.Connection) -> None:
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
    if "repeat_penalty" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN repeat_penalty REAL")
    # Guarded migration: reasoning effort for external reasoning models (nullable
    # -> "" = unset; only threaded to remote providers, see gateway/egress.py).
    if "reasoning_effort" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN reasoning_effort TEXT")
    # Guarded migration: per-agent, per-server MCP tool selection (JSON object
    # {serverId: [toolName, ...]}; absent/empty = all tools from that server).
    if "mcp_tool_allow" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN mcp_tool_allow TEXT")
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
    # Guarded migration: assigned connector names (JSON list; per-verb portal
    # tools, PRD-20). Parallel to custom_tools.
    if "connectors" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN connectors TEXT")
    # Guarded migration: attached Knowledge Base ids (JSON; RAG via kb_search).
    if "knowledge_bases" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN knowledge_bases TEXT")
    # Guarded migration: model source + external provider (per-agent model routing).
    #   model_source: "local" (active model, default) | "byok" | "subscription"
    #   provider:     external provider id when model_source != "local"
    #                 ("deepseek" | "openai" | "anthropic"); the ``model`` column
    #                 then holds that provider's model id (e.g. "deepseek-chat").
    if "model_source" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN model_source TEXT DEFAULT 'local'")
    if "provider" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN provider TEXT")
    # Optional Anthropic extended prompt-cache retention. Blank keeps the
    # default five-minute ephemeral cache behavior unchanged.
    if "cache_ttl" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN cache_ttl TEXT")
    # Guarded migration: per-agent web-search backend (JSON; defaults on read).
    if "web_search" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN web_search TEXT")
    # Guarded migration: attached Knowledge Wiki ids (JSON; OKF wikis, PRD-13).
    if "wikis" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN wikis TEXT")
    # Guarded migration: vision-helper agent id (an agent this one delegates image
    # description to when its own model is text-only; "" = use the global default).
    if "vision_helper_agent_id" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN vision_helper_agent_id TEXT")
    if "template_ref" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN template_ref TEXT")  # JSON: PRD-29 provenance
    # Guarded migration: per-agent connector write/merge grants (JSON object
    # {connectorName: {"write": bool, "merge": bool}}; absent/missing = off, so
    # an agent only gets writes/merge for a connector it was explicitly granted).
    if "connector_grants" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN connector_grants TEXT")
    # Guarded migration: per-agent synchronous-consult (ask) budget override
    # (INTEGER 0..20; NULL = the global MAX_ASKS_PER_RUN default in agent/team.py).
    if "ask_budget" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN ask_budget INTEGER")


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    key = str(p)
    if key not in _migrated_paths:
        # Double-checked under the (reentrant) lock so concurrent readers don't
        # both run the DDL; safe whether or not the caller already holds _lock.
        with _lock:
            if key not in _migrated_paths:
                _migrate(conn)
                _migrated_paths.add(key)
    conn.mark_initialized()
    return conn


def _row_to_agent(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"] or "",
        "persona": r["persona"] or "",
        "model": r["model"],
        "modelSource": (r["model_source"] if "model_source" in r.keys() else None) or "local",
        "provider": (r["provider"] if "provider" in r.keys() else None) or "",
        "cacheTtl": (r["cache_ttl"] if "cache_ttl" in r.keys() else None) or "",
        "temperature": r["temperature"] if r["temperature"] is not None else 0.7,
        "topP": r["top_p"],
        "topK": r["top_k"],
        "repeatPenalty": r["repeat_penalty"] if "repeat_penalty" in r.keys() else None,
        "reasoningEffort": (r["reasoning_effort"] if "reasoning_effort" in r.keys() else "") or "",
        "workingDir": r["working_dir"] or "",
        "role": r["role"] or "",
        "dutyStatus": r["duty_status"] or "off",
        "guardrails": _guardrails(r["guardrails"] if "guardrails" in r.keys() else None),
        "webSearch": _web_search(r["web_search"] if "web_search" in r.keys() else None),
        "tools": json.loads(r["tools"]) if r["tools"] else [],
        "skills": json.loads(r["skills"]) if r["skills"] else [],
        "mcpServers": json.loads(r["mcp_servers"]) if r["mcp_servers"] else [],
        "mcpToolAllow": _mcp_tool_allow(r["mcp_tool_allow"] if "mcp_tool_allow" in r.keys() else None),
        "connectorGrants": _connector_grants(r["connector_grants"] if "connector_grants" in r.keys() else None),
        "askBudget": _ask_budget(r["ask_budget"] if "ask_budget" in r.keys() else None),
        "customTools": json.loads(r["custom_tools"]) if "custom_tools" in r.keys() and r["custom_tools"] else [],
        "connectors": json.loads(r["connectors"]) if "connectors" in r.keys() and r["connectors"] else [],
        "knowledgeBases": json.loads(r["knowledge_bases"]) if "knowledge_bases" in r.keys() and r["knowledge_bases"] else [],
        "wikis": json.loads(r["wikis"]) if "wikis" in r.keys() and r["wikis"] else [],
        "visionHelperAgentId": (r["vision_helper_agent_id"] if "vision_helper_agent_id" in r.keys() else "") or "",
        "templateRef": json.loads(r["template_ref"]) if "template_ref" in r.keys() and r["template_ref"] else None,
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def _ensure_seed() -> None:
    """Create the default Assistant agent once if the table is empty."""
    global _seeded, _seeded_path
    key = str(db_path())
    if _seeded and _seeded_path == key:
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
    _seeded_path = key


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
            "INSERT INTO agents (id, name, description, persona, model, model_source, provider, "
            "temperature, top_p, top_k, repeat_penalty, reasoning_effort, cache_ttl, working_dir, role, duty_status, guardrails, web_search, tools, skills, "
            "mcp_servers, mcp_tool_allow, custom_tools, connectors, knowledge_bases, wikis, vision_helper_agent_id, template_ref, connector_grants, ask_budget, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (agent_id, data.get("name") or "Untitled agent", data.get("description") or "",
             data.get("persona") or "", data.get("model") or None,
             _source(data.get("modelSource")), (data.get("provider") or "").strip() or None,
             float(data.get("temperature", 0.7)), _num(data.get("topP")), _int(data.get("topK")),
             _num(data.get("repeatPenalty")), _effort(data.get("reasoningEffort")) or None,
             "1h" if data.get("cacheTtl") == "1h" else None,
             (data.get("workingDir") or "").strip() or None,
             (data.get("role") or "").strip() or None, data.get("dutyStatus") or "off",
             json.dumps(_guardrails(json.dumps(data.get("guardrails") or {}))),
             json.dumps(_web_search(json.dumps(data.get("webSearch") or {}))),
             json.dumps(data.get("tools") or []), json.dumps(data.get("skills") or []),
             json.dumps(data.get("mcpServers") or []),
             json.dumps(_mcp_tool_allow(data.get("mcpToolAllow"))),
             json.dumps(data.get("customTools") or []),
             json.dumps(data.get("connectors") or []),
             json.dumps(data.get("knowledgeBases") or []),
             json.dumps(data.get("wikis") or []),
             str(data.get("visionHelperAgentId") or "").strip(),
             json.dumps(data.get("templateRef")) if data.get("templateRef") else None,
             json.dumps(_connector_grants(data.get("connectorGrants"))),
             _ask_budget(data.get("askBudget")), now, now),
        )
    return get_agent(agent_id)  # type: ignore[return-value]


def update_agent(agent_id: str, data: dict) -> dict | None:
    # Hold the lock across read+merge+write: this rewrites every column from a
    # merged snapshot, so an interleaved write (e.g. the worker flipping
    # dutyStatus while the UI saves settings) would otherwise be lost — last
    # full-row writer wins. The lock is an RLock so the nested get_agent is fine.
    with _lock:
        existing = get_agent(agent_id)
        if not existing:
            return None
        merged = {**existing, **{k: v for k, v in data.items() if v is not None}}
        # Nullable fields can be explicitly cleared (e.g. switching an agent back to
        # the local active model sends model=null to drop a stale external model id).
        for k in ("topP", "topK", "repeatPenalty", "model"):
            if k in data:
                merged[k] = data[k]
        # Guardrails + web-search: partial-merge incoming over existing, then normalize.
        guardrails = _guardrails(json.dumps({**existing["guardrails"], **(data.get("guardrails") or {})}))
        web_search = _web_search(json.dumps({**existing["webSearch"], **(data.get("webSearch") or {})}))
        now = _now()
        changed_fields = []
        for field in ("persona", "model", "modelSource", "provider", "skills"):
            if existing.get(field) != merged.get(field):
                changed_fields.append(field)
        with _conn() as conn:
            conn.execute(
                "UPDATE agents SET name=?, description=?, persona=?, model=?, model_source=?, provider=?, "
                "temperature=?, top_p=?, top_k=?, repeat_penalty=?, reasoning_effort=?, cache_ttl=?, working_dir=?, role=?, duty_status=?, guardrails=?, web_search=?, tools=?, "
                "skills=?, mcp_servers=?, mcp_tool_allow=?, custom_tools=?, connectors=?, knowledge_bases=?, wikis=?, vision_helper_agent_id=?, template_ref=?, connector_grants=?, ask_budget=?, updated_at=? WHERE id=?",
                (merged["name"], merged.get("description") or "", merged.get("persona") or "",
                 merged.get("model") or None,
                 _source(merged.get("modelSource")), (merged.get("provider") or "").strip() or None,
                 float(merged.get("temperature", 0.7)),
                 _num(merged.get("topP")), _int(merged.get("topK")),
                 _num(merged.get("repeatPenalty")), _effort(merged.get("reasoningEffort")) or None,
                 "1h" if merged.get("cacheTtl") == "1h" else None,
                 (merged.get("workingDir") or "").strip() or None,
                 (merged.get("role") or "").strip() or None,
                 merged.get("dutyStatus") or "off", json.dumps(guardrails), json.dumps(web_search),
                 json.dumps(merged.get("tools") or []), json.dumps(merged.get("skills") or []),
                 json.dumps(merged.get("mcpServers") or []),
                 json.dumps(_mcp_tool_allow(merged.get("mcpToolAllow"))),
                 json.dumps(merged.get("customTools") or []),
                 json.dumps(merged.get("connectors") or []),
                 json.dumps(merged.get("knowledgeBases") or []),
                 json.dumps(merged.get("wikis") or []),
                 str(merged.get("visionHelperAgentId") or "").strip(),
                 json.dumps(merged.get("templateRef")) if merged.get("templateRef") else None,
                 json.dumps(_connector_grants(merged.get("connectorGrants"))),
                 _ask_budget(merged.get("askBudget")), now, agent_id),
            )
        if changed_fields:
            try:
                from storage import activity_store
                activity_store.log(agent_id, "config_changed",
                                   "config changed: " + ", ".join(changed_fields))
            except Exception:  # noqa: BLE001 - journaling must never break updates
                pass
        return get_agent(agent_id)


def apply_managed_update(agent_id: str, managed: dict, template_ref: dict | None, expected_updated_at: str) -> dict | None:
    with _lock, _conn() as conn:
        existing = get_agent(agent_id)
        if not existing:
            return None
        custom_tools = (managed["customTools"] if "customTools" in managed
                        else existing.get("customTools") or [])
        connectors = (managed["connectors"] if "connectors" in managed
                      else existing.get("connectors") or [])
        wikis = (managed["wikis"] if "wikis" in managed else existing.get("wikis") or [])
        knowledge_bases = (managed["knowledgeBases"] if "knowledgeBases" in managed
                           else existing.get("knowledgeBases") or [])
        mcp_servers = (managed["mcpServers"] if "mcpServers" in managed
                       else existing.get("mcpServers") or [])
        mcp_tool_allow = (managed["mcpToolAllow"] if "mcpToolAllow" in managed
                          else existing.get("mcpToolAllow") or {})
        # connectorGrants is an agent-local trust decision, not shared template
        # prose — a managed update keeps the agent's existing grants untouched
        # unless the caller explicitly supplies them.
        connector_grants = (managed["connectorGrants"] if "connectorGrants" in managed
                            else existing.get("connectorGrants") or {})
        cur = conn.execute(
            "UPDATE agents SET persona=?, role=?, description=?, temperature=?, top_p=?, top_k=?, "
            "tools=?, skills=?, mcp_servers=?, mcp_tool_allow=?, custom_tools=?, connectors=?, "
            "knowledge_bases=?, wikis=?, guardrails=?, template_ref=?, connector_grants=?, updated_at=? "
            "WHERE id=? AND updated_at=?",
            ((managed.get("persona") or ""), (managed.get("role") or ""),
             (managed.get("description") or ""), float(managed.get("temperature", 0.7)),
             _num(managed.get("topP")), _int(managed.get("topK")),
             json.dumps(managed.get("tools") or []), json.dumps(managed.get("skills") or []),
             json.dumps(mcp_servers), json.dumps(_mcp_tool_allow(mcp_tool_allow)),
             json.dumps(custom_tools), json.dumps(connectors),
             json.dumps(knowledge_bases), json.dumps(wikis),
             json.dumps(_guardrails(json.dumps(managed.get("guardrails") or {}))),
             json.dumps(template_ref) if template_ref else None,
             json.dumps(_connector_grants(connector_grants)), _now(), agent_id,
             expected_updated_at),
        )
        applied = cur.rowcount == 1
    return get_agent(agent_id) if applied else None


def delete_agent(agent_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    return cur.rowcount > 0
