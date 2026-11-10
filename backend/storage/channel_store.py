"""Delivery channels + the delivery audit log (PRD-01).

Local-first outbound delivery state lives here in ``app.db``:

  • ``channels``   — pluggable destinations (in-app chat, desktop, webhook, …).
                     Non-secret config is JSON; network channels are opt-in and
                     disabled until explicitly enabled. No plaintext secrets.
  • ``deliveries`` — an append-only audit row for *every* send attempt (channel,
                     target, source, status, error, payload size). This is the
                     queryable history the UI surfaces and the safety record that
                     proves every delivery had an explicit, named target.

Local channels (in_app / desktop) work offline and need no row here; ``channels``
exists for network channels (webhook today) whose URLs form an opt-in allow-list.
"""

from __future__ import annotations

import json
import re
import sqlite3
from storage import _sqlite
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir
from storage import settings

_lock = threading.Lock()

_SECRETS_KEY = "deliveryTargetSecrets"

KINDS = ("in_app", "desktop", "webhook", "email", "chat", "obsidian")
STATUSES = ("sent", "failed", "queued", "dropped")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            config_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            channel TEXT NOT NULL,
            target TEXT,
            source TEXT NOT NULL,            -- tool | schedule | event | test
            agent_id TEXT,
            schedule_id TEXT,
            status TEXT NOT NULL,            -- sent | failed | queued | dropped
            error TEXT,
            payload_text TEXT,
            payload_bytes INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_ts ON deliveries(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_agent ON deliveries(agent_id, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_schedule ON deliveries(schedule_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status)")
    conn.mark_initialized()
    return conn


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #

def _chan_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "name": r["name"],
        "enabled": bool(r["enabled"]),
        "config": json.loads(r["config_json"]) if r["config_json"] else {},
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def create_channel(kind: str, name: str, *, enabled: bool = False,
                   config: dict | None = None) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    cid = uuid.uuid4().hex[:12]
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO channels (id, kind, name, enabled, config_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, kind, (name or kind).strip()[:80], 1 if enabled else 0,
             json.dumps(config or {}), now, now),
        )
    return get_channel(cid)  # type: ignore[return-value]


def get_channel(channel_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    return _chan_row(r) if r else None


def list_channels() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM channels ORDER BY created_at ASC").fetchall()
    return [_chan_row(r) for r in rows]


def update_channel(channel_id: str, **fields) -> dict | None:
    cur = get_channel(channel_id)
    if not cur:
        return None
    name = fields.get("name", cur["name"])
    enabled = fields.get("enabled", cur["enabled"])
    config = fields.get("config", cur["config"])
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE channels SET name=?, enabled=?, config_json=?, updated_at=? WHERE id=?",
            ((name or cur["kind"]).strip()[:80], 1 if enabled else 0,
             json.dumps(config or {}), _now(), channel_id),
        )
    return get_channel(channel_id)


def delete_channel(channel_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        deleted = cur.rowcount > 0
    set_secrets(channel_id, None)
    return deleted


def webhook_allowlist() -> set[str]:
    """The set of URLs reachable via *enabled* webhook channels — the opt-in
    egress allow-list. A URL not in this set must never be POSTed to."""
    urls: set[str] = set()
    for c in list_channels():
        if c["kind"] == "webhook" and c["enabled"]:
            for u in (c["config"].get("urls") or []):
                if isinstance(u, str) and u.strip():
                    urls.add(u.strip())
    return urls


def ensure_webhook(url: str, name: str | None = None) -> dict:
    """Idempotently register + enable a webhook URL in the allow-list. Explicit
    opt-in: the caller supplied the URL. Reuses an existing enabled channel that
    already lists this URL, else creates one."""
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    for c in list_channels():
        if c["kind"] == "webhook" and url in (c["config"].get("urls") or []):
            if not c["enabled"]:
                update_channel(c["id"], enabled=True)
            return get_channel(c["id"])  # type: ignore[return-value]
    return create_channel("webhook", name or url, enabled=True, config={"urls": [url]})


# --------------------------------------------------------------------------- #
# Deliveries (audit log)
# --------------------------------------------------------------------------- #

def _del_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "ts": r["ts"],
        "channel": r["channel"],
        "target": r["target"],
        "source": r["source"],
        "agentId": r["agent_id"],
        "scheduleId": r["schedule_id"],
        "status": r["status"],
        "error": r["error"],
        "payloadText": r["payload_text"],
        "payloadBytes": r["payload_bytes"],
    }


def log_delivery(*, channel: str, target: str | None, source: str,
                 status: str, agent_id: str | None = None,
                 schedule_id: str | None = None, error: str = "",
                 payload_text: str = "") -> dict:
    """Append one delivery audit row. Best-effort: never raise into the caller."""
    did = uuid.uuid4().hex[:12]
    now = _now()
    payload_text = payload_text or ""
    nbytes = len(payload_text.encode("utf-8"))
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO deliveries (id, ts, channel, target, source, agent_id, "
            "schedule_id, status, error, payload_text, payload_bytes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (did, now, channel, target, source, agent_id, schedule_id,
             status if status in STATUSES else "failed",
             (error or "")[:1000], payload_text[:8000], nbytes),
        )
    return {
        "id": did, "ts": now, "channel": channel, "target": target,
        "source": source, "agentId": agent_id, "scheduleId": schedule_id,
        "status": status, "error": error, "payloadText": payload_text,
        "payloadBytes": nbytes,
    }


def get_delivery(delivery_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
    return _del_row(r) if r else None


def delivery_health(agent_id: str, channel: str, *, window: int = 50) -> dict:
    """Recent send health for one ``agent_id`` + ``channel`` — for surfacing
    'is this channel actually working?' in a status UI (the socket can be up while
    every send fails). Looks at the most recent ``window`` attempts (newest-first).

    Returns ``{lastSentAt, lastFailedAt, lastError, failuresSinceSuccess}``.
    ``failuresSinceSuccess > 0`` means something is broken *right now* (every
    attempt since the last success failed)."""
    rows = list_deliveries(agent_id=agent_id, channel=channel, limit=max(1, window))
    last_sent = next((r for r in rows if r["status"] == "sent"), None)
    last_failed = next((r for r in rows if r["status"] in ("failed", "dropped")), None)
    fails_since = 0
    for r in rows:                    # newest-first: count failures up to the last success
        if r["status"] == "sent":
            break
        if r["status"] in ("failed", "dropped"):
            fails_since += 1
    return {
        "lastSentAt": last_sent["ts"] if last_sent else None,
        "lastFailedAt": last_failed["ts"] if last_failed else None,
        "lastError": last_failed["error"] if last_failed else None,
        "failuresSinceSuccess": fails_since,
    }


def _delivery_where(*, agent_id: str | None = None, schedule_id: str | None = None,
                    channel: str | None = None, status: str | None = None,
                    source: str | None = None, search: str | None = None) -> tuple[str, list]:
    q = " WHERE 1=1"
    args: list = []
    if agent_id:
        q += " AND agent_id=?"; args.append(agent_id)
    if schedule_id:
        q += " AND schedule_id=?"; args.append(schedule_id)
    if channel == "chat":                      # all chat channels: chat, chat:whatsapp, chat:telegram, …
        q += " AND (channel=? OR channel LIKE 'chat:%')"; args.append(channel)
    elif channel:
        q += " AND channel=?"; args.append(channel)
    if status:
        q += " AND status=?"; args.append(status)
    if source:
        q += " AND source=?"; args.append(source)
    if search and search.strip():
        # case-insensitive substring over target, text and error; LIKE wildcards in the term are literal
        term = "%" + re.sub(r"([\\%_])", r"\\\1", search.strip().lower()) + "%"
        q += (" AND (lower(coalesce(target,'')) LIKE ? ESCAPE '\\'"
              " OR lower(coalesce(payload_text,'')) LIKE ? ESCAPE '\\'"
              " OR lower(coalesce(error,'')) LIKE ? ESCAPE '\\')")
        args += [term, term, term]
    return q, args


def list_deliveries(*, agent_id: str | None = None, schedule_id: str | None = None,
                    channel: str | None = None, status: str | None = None,
                    source: str | None = None, search: str | None = None,
                    limit: int = 200, offset: int = 0) -> list[dict]:
    where, args = _delivery_where(agent_id=agent_id, schedule_id=schedule_id, channel=channel,
                                  status=status, source=source, search=search)
    q = "SELECT * FROM deliveries" + where + " ORDER BY ts DESC, rowid DESC LIMIT ? OFFSET ?"
    args += [max(1, int(limit)), max(0, int(offset))]
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_del_row(r) for r in rows]


def count_deliveries(*, agent_id: str | None = None, schedule_id: str | None = None,
                     channel: str | None = None, status: str | None = None,
                     source: str | None = None, search: str | None = None) -> dict:
    """Rows matching the filters, plus a per-status tally over the same filters
    minus ``status`` (so the summary chips stay meaningful while one is picked)."""
    where, args = _delivery_where(agent_id=agent_id, schedule_id=schedule_id, channel=channel,
                                  source=source, search=search)
    with _conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM deliveries" + where + " GROUP BY status",
                            args).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    total = by_status.get(status, 0) if status else sum(by_status.values())
    return {"total": total, "byStatus": by_status}


# --------------------------------------------------------------------------- #
# Write-only secrets (settings file, never the DB, never echoed back)
# --------------------------------------------------------------------------- #

def get_secrets(channel_id: str) -> dict:
    allv = settings.get(_SECRETS_KEY) or {}
    v = allv.get(channel_id) if isinstance(allv, dict) else None
    return dict(v) if isinstance(v, dict) else {}


def set_secrets(channel_id: str, values: dict | None) -> None:
    allv = settings.get(_SECRETS_KEY) or {}
    if not isinstance(allv, dict):
        allv = {}
    if values is None:
        allv.pop(channel_id, None)
    else:
        cur = dict(allv.get(channel_id) or {})
        for k, v in values.items():
            if v is None or v == "":
                cur.pop(k, None)
            else:
                cur[k] = str(v)
        allv[channel_id] = cur
    settings.set(_SECRETS_KEY, allv or None)


def secret_flags(channel_id: str) -> dict:
    cur = get_secrets(channel_id)
    return {f"has{k[0].upper()}{k[1:]}": bool(v) for k, v in cur.items()}


def email_target_for(address: str) -> dict | None:
    """The first *enabled* email target whose ``allowTo`` list permits
    ``address`` (exact address, or ``@domain``). None → refuse to send."""
    addr = (address or "").strip().lower()
    if "@" not in addr:
        return None
    domain = "@" + addr.rsplit("@", 1)[1]
    for c in list_channels():
        if c["kind"] != "email" or not c["enabled"]:
            continue
        allow = [str(x).strip().lower() for x in (c["config"].get("allowTo") or []) if str(x).strip()]
        if addr in allow or domain in allow or "*" in allow:
            return c
    return None


def prune_deliveries(older_than_iso: str) -> int:
    """Delete delivery audit rows older than ``older_than_iso``."""
    with _lock, _conn() as conn:
        return conn.execute("DELETE FROM deliveries WHERE ts < ?", (older_than_iso,)).rowcount
