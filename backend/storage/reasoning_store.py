"""Encrypted provider reasoning sidecars for replayable Codex turns."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from storage import _sqlite
import threading
from datetime import datetime, timezone

from config import db_path, ensure_dir


_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reasoning_sidecars (
            session_id TEXT NOT NULL, seq INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            items TEXT NOT NULL, provider TEXT, model TEXT, origin_id TEXT,
            created_at TEXT NOT NULL,
            enc_version INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, seq, fingerprint)
        )
    """)
    # Idempotent migration for dbs created before enc_version existed. 0 = raw
    # JSON (legacy + luna encrypted_content rows), 1 = AES-GCM envelope.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reasoning_sidecars)")}
    if "enc_version" not in cols:
        conn.execute("ALTER TABLE reasoning_sidecars "
                     "ADD COLUMN enc_version INTEGER NOT NULL DEFAULT 0")
    conn.mark_initialized()
    return conn


def put(session_id: str, seq: int, fingerprint: str, items: list[dict] | dict, *,
        provider: str | None, model: str | None, origin_id: str | None,
        enc_version: int = 0) -> None:
    """Low-level insert. ``items`` is JSON-serialized into the ``items`` column:
    a raw item list when ``enc_version=0``, an AES-GCM envelope dict when ``1``."""
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reasoning_sidecars "
            "(session_id, seq, fingerprint, items, provider, model, origin_id, created_at, enc_version) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, seq, fingerprint, json.dumps(items), provider, model, origin_id, now, enc_version),
        )


def _is_reasoning(item: object) -> bool:
    return isinstance(item, dict) and item.get("type") == "reasoning"


def _has_plaintext_reasoning(items: list[dict] | None) -> bool:
    """A captured item carries non-empty plaintext reasoning_text (DeepSeek/Kimi,
    which return no encrypted_content)."""
    for item in items or []:
        if not _is_reasoning(item):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "reasoning_text":
                text = c.get("text")
                if isinstance(text, str) and text.strip():
                    return True
    return False


def _strip_luna_summaries(items: list[dict]) -> list[dict]:
    """Drop the plaintext ``summary`` field from luna reasoning items (those
    carrying opaque ``encrypted_content``) so a raw (enc_version=0) row holds
    zero readable reasoning. Returns a shallow-copied item list."""
    out: list[dict] = []
    for item in items:
        if _is_reasoning(item) and item.get("encrypted_content") and "summary" in item:
            item = {k: v for k, v in item.items() if k != "summary"}
        out.append(item)
    return out


def persist(session_id: str, seq: int, fingerprint: str, items: list[dict], *,
            provider: str | None, model: str | None, origin_id: str | None,
            enc_key: bytes | None = None) -> dict | None:
    """Decide per turn how to store captured reasoning items (fail-closed):

      - plaintext reasoning present, no key  -> do NOT store (no consent).
      - plaintext reasoning present, key set  -> AES-GCM encrypt, enc_version=1.
      - only encrypted_content (luna)         -> strip summary, store raw, v0.
    """
    has_plaintext = _has_plaintext_reasoning(items)
    if has_plaintext and not enc_key:
        return None  # fail-closed: never store readable reasoning without a key
    if has_plaintext and enc_key:
        from storage import reasoning_crypto
        aad = reasoning_crypto.build_aad(session_id, seq, fingerprint,
                                         provider, model, origin_id)
        try:
            envelope = reasoning_crypto.encrypt_items(enc_key, items, aad)
        except Exception:
            return None  # fail-closed: encrypt failure -> never store plaintext
        put(session_id, seq, fingerprint, envelope, provider=provider,
            model=model, origin_id=origin_id, enc_version=1)
        # Return the decrypted form that get_for_session() would expose. This
        # lets the active loop replay the sidecar immediately while keeping the
        # encrypted envelope out of its in-memory replay map.
        stored_items = items
    else:
        stored_items = _strip_luna_summaries(items)
        put(session_id, seq, fingerprint, stored_items, provider=provider,
            model=model, origin_id=origin_id, enc_version=0)

    return {
        "seq": seq, "fingerprint": fingerprint, "items": stored_items,
        "provider": provider, "model": model, "originId": origin_id,
        "createdAt": _now(),
    }


def get_for_session(session_id: str, enc_key: bytes | None = None) -> list[dict]:
    """Load a session's sidecars. ``enc_version=1`` rows are AES-GCM decrypted
    with ``enc_key`` + the row-derived AAD; a decrypt failure (no/wrong key,
    tamper) DELETEs the offending row and skips it (fail-closed — never replay
    plaintext on failure). ``enc_version=0`` rows are returned as-is."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT seq, fingerprint, items, provider, model, origin_id, created_at, enc_version "
            "FROM reasoning_sidecars WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
    out: list[dict] = []
    dead: list[tuple] = []
    for r in rows:
        if r["enc_version"] == 1:
            from storage import reasoning_crypto
            aad = reasoning_crypto.build_aad(session_id, r["seq"], r["fingerprint"],
                                             r["provider"], r["model"], r["origin_id"])
            items = (reasoning_crypto.decrypt_items(enc_key, r["items"], aad)
                     if enc_key else None)
            if items is None:
                dead.append((session_id, r["seq"], r["fingerprint"]))
                continue
        else:
            items = json.loads(r["items"])
        out.append({
            "seq": r["seq"], "fingerprint": r["fingerprint"], "items": items,
            "provider": r["provider"], "model": r["model"],
            "originId": r["origin_id"], "createdAt": r["created_at"],
        })
    if dead:
        with _lock, _conn() as conn:
            for sid, seq, fp in dead:
                conn.execute("DELETE FROM reasoning_sidecars "
                             "WHERE session_id = ? AND seq = ? AND fingerprint = ?",
                             (sid, seq, fp))
    return out


def delete_session(session_id: str) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM reasoning_sidecars WHERE session_id = ?", (session_id,))
        return cur.rowcount


def purge_encrypted_plaintext() -> int:
    """Delete every AES-GCM (enc_version=1) plaintext-reasoning row. Called on a
    reasoning-key clear/change so replay can't silently continue on stale
    reasoning encrypted under a superseded key."""
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM reasoning_sidecars WHERE enc_version = 1")
        return cur.rowcount


def has_replayable_reasoning(items: list[dict] | None, allow_plaintext: bool) -> bool:
    """Whether a captured item list is worth persisting as a sidecar. A reasoning
    item carrying non-empty ``encrypted_content`` is always replayable. A reasoning
    item carrying non-empty plaintext ``reasoning_text`` (DeepSeek/Kimi, no
    encrypted_content) is replayable ONLY when ``allow_plaintext`` is True (a
    reasoning encryption key is configured). A turn with only message/function_call
    items can never be usefully replayed, so it is skipped."""
    for item in items or []:
        if not _is_reasoning(item):
            continue
        if item.get("encrypted_content"):
            return True
        if allow_plaintext:
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "reasoning_text":
                    text = c.get("text")
                    if isinstance(text, str) and text.strip():
                        return True
    return False


def sidecar_fingerprint(assistant_msg: dict) -> str:
    content = str(assistant_msg.get("content") or "")
    calls = [[(tc.get("function") or {}).get("name"),
              (tc.get("function") or {}).get("arguments")]
             for tc in (assistant_msg.get("tool_calls") or [])]
    payload = json.dumps({"content": content, "calls": calls},
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
