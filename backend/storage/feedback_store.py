"""Feedback capture — rate an assistant turn, keep the signal, export a dataset.

Every assistant chat turn can be rated up or down, or corrected with an inline
edit. Signals land in the ``feedback`` table of ``app.db`` and can be exported
as a chat-format JSONL that any supervised fine-tuning pipeline accepts. The
curation rules for that export:

  up    -> (user prompt -> original reply)    positive exemplar
  edit  -> (user prompt -> corrected reply)   the trusted human-edited signal
  down  -> excluded from positive targets (kept for later preference tuning)

Re-rating the same turn replaces the earlier signal, so the data reflects the
operator's latest judgement. Nothing here trains a model; it only keeps the
examples a trainer would need.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir
from storage import _sqlite

_lock = threading.Lock()

SIGNALS = ("up", "down", "edit")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = _sqlite.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            agent_id TEXT,
            session_id TEXT,
            message_id TEXT,
            turn_index INTEGER,
            signal TEXT NOT NULL,
            user_prompt TEXT,
            original TEXT,
            corrected TEXT,
            model TEXT,
            debug_run_id TEXT,
            context_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_session ON feedback(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_agent ON feedback(agent_id, signal)")
    conn.mark_initialized()
    return conn


def _row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "agentId": r["agent_id"],
        "sessionId": r["session_id"],
        "messageId": r["message_id"],
        "turnIndex": r["turn_index"],
        "signal": r["signal"],
        "userPrompt": r["user_prompt"] or "",
        "original": r["original"] or "",
        "corrected": r["corrected"],
        "model": r["model"],
        "debugRunId": r["debug_run_id"],
        "context": json.loads(r["context_json"]) if r["context_json"] else None,
        "createdAt": r["created_at"],
    }


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #

def record(data: dict) -> dict:
    """Record one signal on an assistant turn. Same session + message replaces."""
    signal = (data.get("signal") or "").strip().lower()
    if signal not in SIGNALS:
        raise ValueError(f"signal must be one of {SIGNALS}")
    corrected = data.get("correctedText")
    if corrected is None:
        corrected = data.get("corrected")
    if signal == "edit" and not (corrected and str(corrected).strip()):
        raise ValueError("an 'edit' signal requires correctedText")

    session_id = data.get("sessionId")
    message_id = data.get("messageId")
    if message_id is None and data.get("turnIndex") is not None:
        message_id = str(data.get("turnIndex"))

    now = _now()
    fid = "fb_" + uuid.uuid4().hex[:12]
    context = data.get("context")
    with _lock, _conn() as conn:
        if session_id and message_id is not None:
            prev = conn.execute(
                "SELECT id, created_at FROM feedback WHERE session_id=? AND message_id=?",
                (session_id, str(message_id))).fetchone()
            if prev:
                fid = prev["id"]
                now = prev["created_at"]
                conn.execute("DELETE FROM feedback WHERE id=?", (fid,))
        conn.execute(
            "INSERT INTO feedback (id, agent_id, session_id, message_id, turn_index, signal, "
            "user_prompt, original, corrected, model, debug_run_id, context_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, data.get("agentId"), session_id,
             str(message_id) if message_id is not None else None,
             data.get("turnIndex"), signal,
             (data.get("userPrompt") or "")[:20000],
             (data.get("original") or data.get("originalReply") or "")[:20000],
             str(corrected).strip()[:20000] if (signal == "edit" and corrected) else None,
             data.get("model"), data.get("debugRunId"),
             json.dumps(context) if context else None, now),
        )
    return get(fid)  # type: ignore[return-value]


def get(fid: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
    return _row(r) if r else None


def list_feedback(agent_id: str | None = None, signal: str | None = None,
                  session_id: str | None = None, limit: int = 500) -> list[dict]:
    q = "SELECT * FROM feedback WHERE 1=1"
    args: list = []
    if agent_id:
        q += " AND agent_id=?"; args.append(agent_id)
    if signal:
        q += " AND signal=?"; args.append(signal)
    if session_id:
        q += " AND session_id=?"; args.append(session_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row(r) for r in rows]


def delete(fid: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM feedback WHERE id=?", (fid,))
    return cur.rowcount > 0


def counts(agent_id: str | None = None) -> dict:
    q = "SELECT signal, COUNT(*) AS c FROM feedback"
    args: list = []
    if agent_id:
        q += " WHERE agent_id=?"; args.append(agent_id)
    q += " GROUP BY signal"
    with _conn() as conn:
        rows = conn.execute(q, args).fetchall()
    by = {r["signal"]: r["c"] for r in rows}
    up, down, edit = by.get("up", 0), by.get("down", 0), by.get("edit", 0)
    return {"up": up, "down": down, "edit": edit, "total": up + down + edit,
            "trainable": up + edit}


# --------------------------------------------------------------------------- #
# Export — curated chat-format JSONL
# --------------------------------------------------------------------------- #

def _example(user_prompt: str, assistant: str) -> dict:
    return {"messages": [
        {"role": "user", "content": user_prompt},
        {"role": "assistant", "content": assistant},
    ]}


def build_dataset(agent_id: str | None = None) -> tuple[bytes, dict]:
    """Curate feedback into JSONL bytes plus a summary. Down-only turns are
    excluded; near-identical (prompt, reply) pairs are collapsed."""
    rows = list_feedback(agent_id=agent_id, limit=100000)
    lines: list[str] = []
    seen: set[str] = set()
    positives = corrections = skipped = 0
    for r in reversed(rows):  # oldest first, so the file reads chronologically
        if r["signal"] == "up":
            user_prompt, assistant, is_corr = r["userPrompt"], r["original"], False
        elif r["signal"] == "edit":
            user_prompt, assistant, is_corr = r["userPrompt"], (r["corrected"] or ""), True
        else:
            skipped += 1
            continue
        user_prompt = (user_prompt or "").strip()
        assistant = (assistant or "").strip()
        if not user_prompt or not assistant:
            skipped += 1
            continue
        key = hashlib.sha256((user_prompt + "\x00" + assistant).encode("utf-8")).hexdigest()
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        lines.append(json.dumps(_example(user_prompt, assistant), ensure_ascii=False))
        if is_corr:
            corrections += 1
        else:
            positives += 1
    if not lines:
        raise ValueError("no usable feedback yet: rate a reply up or save a correction first "
                         "(down-only turns are excluded)")
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    return raw, {"examples": len(lines), "positives": positives,
                 "corrections": corrections, "skipped": skipped, "bytes": len(raw)}
