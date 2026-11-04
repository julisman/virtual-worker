"""Chat-channel accounts, bindings, dedup and pairing requests (PRD §8.6, §10).

Tables in ``app.db``:

  • ``chat_accounts``         — one row per connected account (a WhatsApp number,
                                a bot token, …): kind, non-secret config JSON,
                                default agent, enabled flag. **Secrets never live
                                here** — they go to ``settings.json`` under
                                ``chatChannelSecrets.<accountId>`` (write-only).
  • ``chat_bindings``         — ``(account, conversation) → agent`` overrides.
                                Absent row → the account's default agent.
  • ``chat_events``           — seen platform message ids (dedup, prunable).
  • ``chat_pairing_requests`` — senders waiting for owner approval when the DM
                                policy is ``pairing``.
"""

from __future__ import annotations

import json
import secrets as _secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from config import db_path, ensure_dir
from storage import settings

_lock = threading.Lock()

DM_POLICIES = ("open", "pairing", "allowlist")
GROUP_POLICIES = ("open", "mention", "allowlist", "off")

DEFAULT_CONFIG = {
    "dmPolicy": "pairing",      # safest default: unknown senders must be approved
    "groupPolicy": "mention",   # reply in groups only when mentioned
    "allowFrom": [],            # sender ids (phone numbers / user ids) allowed
    "debounceMs": 1500,         # merge rapid consecutive messages
    "ackAfterMs": 4000,         # best-effort "working on it" after this long
    "runTimeoutS": 900,         # hard cap on one turn's run
    "historyTurns": 20,         # prior messages replayed into the model
    "sendMediaToModel": False,  # pass images as model input (needs a VLM)
}

_SECRETS_KEY = "chatChannelSecrets"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_accounts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            default_agent_id TEXT,
            config_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_bindings (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(account_id, conversation_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_events (
            event_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            conversation_id TEXT,
            received_at TEXT NOT NULL,
            PRIMARY KEY (account_id, event_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_pairing_requests (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            sender_name TEXT,
            code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
            created_at TEXT NOT NULL,
            UNIQUE(account_id, sender_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_events_time ON chat_events(received_at)")
    return conn


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

def _norm_config(cfg: dict | None) -> dict:
    c = {**DEFAULT_CONFIG, **(cfg or {})}
    if c["dmPolicy"] not in DM_POLICIES:
        c["dmPolicy"] = DEFAULT_CONFIG["dmPolicy"]
    if c["groupPolicy"] not in GROUP_POLICIES:
        c["groupPolicy"] = DEFAULT_CONFIG["groupPolicy"]
    c["allowFrom"] = [str(x).strip() for x in (c.get("allowFrom") or []) if str(x).strip()]
    for k in ("debounceMs", "ackAfterMs", "runTimeoutS", "historyTurns"):
        try:
            c[k] = max(0, int(c.get(k) or 0))
        except (TypeError, ValueError):
            c[k] = DEFAULT_CONFIG[k]
    c["sendMediaToModel"] = bool(c.get("sendMediaToModel"))
    return c


def _acct_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "name": r["name"],
        "enabled": bool(r["enabled"]),
        "defaultAgentId": r["default_agent_id"],
        "config": _norm_config(json.loads(r["config_json"]) if r["config_json"] else {}),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def create_account(kind: str, name: str, *, enabled: bool = False,
                   default_agent_id: str | None = None,
                   config: dict | None = None) -> dict:
    kind = (kind or "").strip().lower()
    if not kind:
        raise ValueError("kind is required")
    aid = uuid.uuid4().hex[:12]
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO chat_accounts (id, kind, name, enabled, default_agent_id, "
            "config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, kind, (name or kind).strip()[:80], 1 if enabled else 0,
             default_agent_id, json.dumps(_norm_config(config)), now, now),
        )
    return get_account(aid)  # type: ignore[return-value]


def get_account(account_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM chat_accounts WHERE id=?", (account_id,)).fetchone()
    return _acct_row(r) if r else None


def list_accounts(*, kind: str | None = None) -> list[dict]:
    q = "SELECT * FROM chat_accounts"
    args: list = []
    if kind:
        q += " WHERE kind=?"; args.append(kind)
    q += " ORDER BY created_at ASC"
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_acct_row(r) for r in rows]


def update_account(account_id: str, **fields) -> dict | None:
    cur = get_account(account_id)
    if not cur:
        return None
    name = fields.get("name", cur["name"])
    enabled = fields.get("enabled", cur["enabled"])
    default_agent_id = fields.get("default_agent_id", cur["defaultAgentId"])
    config = _norm_config({**cur["config"], **(fields.get("config") or {})}) \
        if "config" in fields else cur["config"]
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE chat_accounts SET name=?, enabled=?, default_agent_id=?, config_json=?, "
            "updated_at=? WHERE id=?",
            ((name or cur["kind"]).strip()[:80], 1 if enabled else 0, default_agent_id,
             json.dumps(config), _now(), account_id),
        )
    return get_account(account_id)


def delete_account(account_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM chat_accounts WHERE id=?", (account_id,))
        conn.execute("DELETE FROM chat_bindings WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM chat_events WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM chat_pairing_requests WHERE account_id=?", (account_id,))
        deleted = cur.rowcount > 0
    set_secrets(account_id, None)
    return deleted


# --------------------------------------------------------------------------- #
# Secrets — write-only, in settings.json (never in the DB, never echoed back)
# --------------------------------------------------------------------------- #

def get_secrets(account_id: str) -> dict:
    allv = settings.get(_SECRETS_KEY) or {}
    v = allv.get(account_id) if isinstance(allv, dict) else None
    return dict(v) if isinstance(v, dict) else {}


def set_secrets(account_id: str, values: dict | None) -> None:
    """Merge ``values`` into the account's secrets. ``None`` wipes them; an
    empty-string value removes that one key (so the UI can clear a field)."""
    allv = settings.get(_SECRETS_KEY) or {}
    if not isinstance(allv, dict):
        allv = {}
    if values is None:
        allv.pop(account_id, None)
    else:
        cur = dict(allv.get(account_id) or {})
        for k, v in values.items():
            if v is None or v == "":
                cur.pop(k, None)
            else:
                cur[k] = str(v)
        allv[account_id] = cur
    settings.set(_SECRETS_KEY, allv or None)


def secret_flags(account_id: str, keys: list[str]) -> dict:
    """``{hasX: bool}`` for the UI — presence only, never the value."""
    cur = get_secrets(account_id)
    return {f"has{k[0].upper()}{k[1:]}": bool(cur.get(k)) for k in keys}


# --------------------------------------------------------------------------- #
# Bindings
# --------------------------------------------------------------------------- #

def _bind_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "accountId": r["account_id"], "conversationId": r["conversation_id"],
            "agentId": r["agent_id"], "title": r["title"], "createdAt": r["created_at"]}


def list_bindings(account_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM chat_bindings WHERE account_id=? ORDER BY created_at",
                            (account_id,)).fetchall()
    return [_bind_row(r) for r in rows]


def get_binding(account_id: str, conversation_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM chat_bindings WHERE account_id=? AND conversation_id=?",
                         (account_id, conversation_id)).fetchone()
    return _bind_row(r) if r else None


def upsert_binding(account_id: str, conversation_id: str, agent_id: str,
                   title: str | None = None) -> dict:
    conversation_id = (conversation_id or "").strip()
    if not conversation_id or not agent_id:
        raise ValueError("conversationId and agentId are required")
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO chat_bindings (id, account_id, conversation_id, agent_id, title, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(account_id, conversation_id) DO UPDATE SET "
            "agent_id=excluded.agent_id, title=COALESCE(excluded.title, chat_bindings.title)",
            (uuid.uuid4().hex[:12], account_id, conversation_id, agent_id, title, _now()),
        )
    return get_binding(account_id, conversation_id)  # type: ignore[return-value]


def delete_binding(binding_id: str) -> bool:
    with _lock, _conn() as conn:
        return conn.execute("DELETE FROM chat_bindings WHERE id=?", (binding_id,)).rowcount > 0


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #

def seen_before(account_id: str, event_id: str, conversation_id: str | None = None) -> bool:
    """Insert-before-run: returns True when this event was already handled."""
    if not event_id:
        return False
    with _lock, _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO chat_events (event_id, account_id, conversation_id, received_at) "
                "VALUES (?,?,?,?)", (event_id, account_id, conversation_id, _now()))
            return False
        except sqlite3.IntegrityError:
            return True


def prune_events(days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock, _conn() as conn:
        return conn.execute("DELETE FROM chat_events WHERE received_at < ?", (cutoff,)).rowcount


# --------------------------------------------------------------------------- #
# Pairing requests
# --------------------------------------------------------------------------- #

def _pair_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "accountId": r["account_id"], "senderId": r["sender_id"],
            "senderName": r["sender_name"], "code": r["code"], "status": r["status"],
            "createdAt": r["created_at"]}


def request_pairing(account_id: str, sender_id: str, sender_name: str = "") -> tuple[dict, bool]:
    """Create (or fetch) the pending request for ``sender_id``. Returns
    ``(request, created)`` so the caller can decide whether to message the sender."""
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM chat_pairing_requests WHERE account_id=? AND sender_id=?",
                         (account_id, sender_id)).fetchone()
        if r:
            return _pair_row(r), False
        code = f"{_secrets.randbelow(10**6):06d}"
        rid = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO chat_pairing_requests (id, account_id, sender_id, sender_name, code, "
            "status, created_at) VALUES (?,?,?,?,?,'pending',?)",
            (rid, account_id, sender_id, sender_name or "", code, _now()))
        r = conn.execute("SELECT * FROM chat_pairing_requests WHERE id=?", (rid,)).fetchone()
    return _pair_row(r), True


def list_pairing_requests(account_id: str, status: str | None = "pending") -> list[dict]:
    q = "SELECT * FROM chat_pairing_requests WHERE account_id=?"
    args: list = [account_id]
    if status:
        q += " AND status=?"; args.append(status)
    q += " ORDER BY created_at DESC"
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_pair_row(r) for r in rows]


def resolve_pairing(request_id: str, approve: bool) -> dict | None:
    """Approve (adds the sender to the account allow-list) or reject."""
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM chat_pairing_requests WHERE id=?", (request_id,)).fetchone()
        if not r:
            return None
        conn.execute("UPDATE chat_pairing_requests SET status=? WHERE id=?",
                     ("approved" if approve else "rejected", request_id))
        req = _pair_row(r)
    if approve:
        acct = get_account(req["accountId"])
        if acct:
            allow = list(acct["config"].get("allowFrom") or [])
            if req["senderId"] not in allow:
                allow.append(req["senderId"])
                update_account(acct["id"], config={"allowFrom": allow})
    req["status"] = "approved" if approve else "rejected"
    return req
