"""Custom tool definitions (local-first), stored in app.db.

A *custom tool* lets an operator give an agent a new capability by declaration —
no backend code, no MCP server. The lightweight first-class alternative to MCP
(see ``storage/mcp_store.py``): an HTTP endpoint described by a name + JSON-Schema
parameters + a URL, which Virtual Worker calls with the model's arguments and returns the
response to the model.

Only the ``http`` transport is implemented here. Sandboxed ``script`` (Python)
tools are a deferred milestone (PRD-10 §M3) — the schema reserves ``transport``
so they can slot in later without a migration.

Agents reference tools by id (``agents.custom_tools``); ``agent/compose.py`` loads
the enabled, assigned tools into the per-run registry as ``{name: {schema, fn}}``.
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

# A custom tool's name is what the model calls — keep it a safe identifier and
# never let it shadow a built-in or the reserved MCP namespace.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Names an operator may not claim: the built-ins + per-agent self-tools the
# harness always injects, plus the ``mcp__`` prefix reserved for MCP tools.
RESERVED_NAMES = {
    "web_search", "web_fetch",
    "read_file", "write_file", "edit_file", "list_dir", "glob", "grep",
    "memory_write", "memory_search", "memory_forget",
    "schedule_create", "schedule_list", "schedule_cancel",
    "load_skill", "load_skill_resource", "shell",
    "task_create", "task_update", "task_list", "task_comment",
}

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class ToolError(ValueError):
    """A bad custom-tool definition (invalid name, schema, method, …)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_tools (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            transport TEXT NOT NULL DEFAULT 'http',   -- 'http' (script: deferred)
            parameters TEXT,        -- JSON Schema for the tool's arguments
            method TEXT NOT NULL DEFAULT 'GET',
            url TEXT,
            headers TEXT,           -- JSON object (may carry a secret value)
            allow_private INTEGER NOT NULL DEFAULT 0,  -- opt-in: reach loopback/private hosts
            timeout INTEGER NOT NULL DEFAULT 20,
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
        "description": r["description"] or "",
        "transport": r["transport"] or "http",
        "parameters": json.loads(r["parameters"]) if r["parameters"] else _empty_schema(),
        "method": (r["method"] or "GET").upper(),
        "url": r["url"] or "",
        "headers": json.loads(r["headers"]) if r["headers"] else {},
        "allowPrivate": bool(r["allow_private"]),
        "timeout": r["timeout"] if r["timeout"] is not None else 20,
        "enabled": bool(r["enabled"]),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}}


def _validate_name(name: str, *, existing_id: str | None = None) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ToolError(
            "tool name must match ^[a-z][a-z0-9_]*$ (lowercase letter, then "
            "letters/digits/underscores)")
    if name in RESERVED_NAMES or name.startswith("mcp__"):
        raise ToolError(f"'{name}' is a reserved tool name")
    # Uniqueness across the registry (case-sensitive id match excluded on edit).
    for t in list_tools():
        if t["name"] == name and t["id"] != existing_id:
            raise ToolError(f"a custom tool named '{name}' already exists")
    return name


def _validate_schema(params) -> dict:
    if params in (None, ""):
        return _empty_schema()
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as exc:
            raise ToolError(f"parameters must be valid JSON Schema: {exc}") from exc
    if not isinstance(params, dict):
        raise ToolError("parameters must be a JSON Schema object")
    # Minimal shape check — a JSON-Schema object with properties.
    if params.get("type", "object") != "object":
        raise ToolError("parameters schema 'type' must be 'object'")
    props = params.get("properties", {})
    if not isinstance(props, dict):
        raise ToolError("parameters.properties must be an object")
    return params


def _validate_method(method: str) -> str:
    method = (method or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise ToolError(f"method must be one of {sorted(ALLOWED_METHODS)}")
    return method


def list_tools() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM custom_tools ORDER BY updated_at DESC").fetchall()
    return [_row(r) for r in rows]


def get_tool(tool_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM custom_tools WHERE id = ?", (tool_id,)).fetchone()
    return _row(r) if r else None


def get_tools(tool_ids: list[str]) -> list[dict]:
    """Resolve a list of ids to tool defs, preserving the requested order."""
    if not tool_ids:
        return []
    by_id = {t["id"]: t for t in list_tools()}
    return [by_id[i] for i in tool_ids if i in by_id]


def create_tool(data: dict) -> dict:
    name = _validate_name(data.get("name"))
    schema = _validate_schema(data.get("parameters"))
    method = _validate_method(data.get("method") or "GET")
    now = _now()
    tid = data.get("id") or uuid.uuid4().hex[:12]
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO custom_tools (id, name, description, transport, parameters, method, "
            "url, headers, allow_private, timeout, enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, name, data.get("description") or "", data.get("transport") or "http",
             json.dumps(schema), method, (data.get("url") or "").strip() or None,
             json.dumps(data.get("headers") or {}),
             1 if data.get("allowPrivate") else 0,
             int(data.get("timeout") or 20),
             0 if data.get("enabled") is False else 1, now, now),
        )
    return get_tool(tid)  # type: ignore[return-value]


def update_tool(tool_id: str, data: dict) -> dict | None:
    existing = get_tool(tool_id)
    if not existing:
        return None
    merged = {**existing, **{k: v for k, v in data.items() if v is not None}}
    name = _validate_name(merged.get("name"), existing_id=tool_id)
    schema = _validate_schema(merged.get("parameters"))
    method = _validate_method(merged.get("method") or "GET")
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE custom_tools SET name=?, description=?, transport=?, parameters=?, method=?, "
            "url=?, headers=?, allow_private=?, timeout=?, enabled=?, updated_at=? WHERE id=?",
            (name, merged.get("description") or "", merged.get("transport") or "http",
             json.dumps(schema), method, (merged.get("url") or "").strip() or None,
             json.dumps(merged.get("headers") or {}),
             1 if merged.get("allowPrivate") else 0,
             int(merged.get("timeout") or 20),
             0 if merged.get("enabled") is False else 1, now, tool_id),
        )
    return get_tool(tool_id)


def delete_tool(tool_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM custom_tools WHERE id = ?", (tool_id,))
    return cur.rowcount > 0
