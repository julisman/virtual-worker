"""API access tokens for the gateway (Unsloth-style).

Tokens are `sk-vw-<hex>`. Only a SHA-256 **hash** is stored — the plaintext
is shown once at creation. A small in-memory cache avoids a DB read on every
inference request; it's invalidated on create/revoke.

Policy (see gateway/auth.py): the gateway is *open* (accepts any key) until the
first token is created; once any active token exists, a valid token is required.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from storage import _sqlite
import threading
import time
from datetime import datetime, timedelta, timezone

from config import db_path

_lock = threading.Lock()
_cache: dict[str, dict] | None = None      # token_hash -> record
_last_used_flush: dict[str, float] = {}     # throttle last_used writes


def _conn() -> sqlite3.Connection:
    conn = _sqlite.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_tokens (
            id           TEXT PRIMARY KEY,
            name         TEXT,
            token_hash   TEXT UNIQUE NOT NULL,
            prefix       TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            expires_at   TEXT,
            last_used_at TEXT,
            revoked      INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.mark_initialized()
    return conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expired(rec: dict) -> bool:
    exp = rec.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp) < _now()
    except ValueError:
        return False


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM api_tokens WHERE revoked = 0"
        ).fetchall()
    _cache = {r["token_hash"]: dict(r) for r in rows}
    return _cache


def _invalidate():
    global _cache
    _cache = None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def create(name: str, ttl_days: int | None = None) -> dict:
    """Create a token. Returns the record INCLUDING the plaintext `token`
    (shown only this once)."""
    token = "sk-vw-" + secrets.token_hex(20)
    tid = secrets.token_hex(8)
    prefix = token[:19]  # e.g. "sk-vw-5134054"
    created = _now().isoformat()
    expires = (_now() + timedelta(days=ttl_days)).isoformat() if ttl_days else None
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO api_tokens (id, name, token_hash, prefix, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, name or "token", _hash(token), prefix, created, expires),
        )
    _invalidate()
    return {"id": tid, "name": name or "token", "token": token, "prefix": prefix,
            "createdAt": created, "expiresAt": expires}


def list_tokens() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, prefix, created_at, expires_at, last_used_at, revoked "
            "FROM api_tokens ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        rec = dict(r)
        out.append({
            "id": rec["id"], "name": rec["name"], "prefix": rec["prefix"],
            "createdAt": rec["created_at"], "expiresAt": rec["expires_at"],
            "lastUsedAt": rec["last_used_at"],
            "revoked": bool(rec["revoked"]), "expired": _expired(rec),
        })
    return out


def revoke(token_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        deleted = cur.rowcount > 0
    _invalidate()
    return deleted


def has_active() -> bool:
    return any(not _expired(rec) for rec in _load_cache().values())


def verify(token: str) -> bool:
    """True if the token is known, not revoked, and not expired."""
    if not token:
        return False
    rec = _load_cache().get(_hash(token))
    if not rec or _expired(rec):
        return False
    _touch(rec)
    return True


def _touch(rec: dict):
    """Throttled last_used_at update (best-effort)."""
    tid = rec["id"]
    now = time.time()
    if now - _last_used_flush.get(tid, 0) < 30:
        return
    _last_used_flush[tid] = now
    try:
        with _conn() as conn:
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                         (_now().isoformat(), tid))
    except sqlite3.Error:
        pass
