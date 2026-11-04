"""Local Knowledge Bases — the RAG storage layer (PRD-03, Milestone 1).

A Knowledge Base is a named collection of ingested documents, chunked and
embedded with a **local** embedding model, with the vectors stored entirely in
``app.db`` (SQLite) — no external vector DB, nothing leaves the machine.

This module owns the storage + the pure-Python retrieval math only:

  * ``chunk_text`` — split a document into overlapping windows.
  * ``search`` — cosine similarity (pure stdlib ``math``, no numpy) over the
    stored float vectors, returning the top-k chunks with their source + score.

Embedding (turning text into vectors) lives one layer up in ``agent/knowledge``
because it needs the network (the local ``/v1/embeddings`` gateway); keeping it
out of here lets the retrieval math be tested with deterministic fake vectors.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from config import db_path, ensure_dir

_lock = threading.Lock()

# Chunking defaults — ~800 chars per window with ~100 chars of overlap so a fact
# straddling a boundary still lands whole in at least one chunk.
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    p = db_path()
    ensure_dir(p.parent)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            embed_model TEXT,
            embed_dim INTEGER,
            created_at TEXT NOT NULL,
            indexed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb_chunk (
            id TEXT PRIMARY KEY,
            kb_id TEXT NOT NULL,
            source TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding TEXT NOT NULL,       -- JSON list[float], the chunk vector
            created_at TEXT NOT NULL,
            FOREIGN KEY (kb_id) REFERENCES kb(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunk_kb ON kb_chunk(kb_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunk_source ON kb_chunk(kb_id, source)")
    return conn


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split ``text`` into overlapping fixed-size windows.

    A tiny doc (< one window) becomes a single chunk. Windows slide by
    ``size - overlap`` so consecutive chunks share ``overlap`` chars of context.
    Size/overlap default to the module constants (resolved at call time so they
    stay overridable in tests).
    """
    size = CHUNK_SIZE if size is None else size
    overlap = CHUNK_OVERLAP if overlap is None else overlap
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = max(1, size - max(0, overlap))
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        piece = text[start:start + size].strip()
        if piece:
            chunks.append(piece)
        if start + size >= n:
            break
        start += step
    return chunks


# --------------------------------------------------------------------------- #
# Cosine similarity (pure Python — no numpy)
# --------------------------------------------------------------------------- #

def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. 0.0 if either is empty,
    zero-magnitude, or length-mismatched (a safe, comparable fallback)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# --------------------------------------------------------------------------- #
# KB CRUD
# --------------------------------------------------------------------------- #

def _kb_row(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    counts = conn.execute(
        "SELECT COUNT(*) AS chunks, COUNT(DISTINCT source) AS sources "
        "FROM kb_chunk WHERE kb_id=?",
        (r["id"],),
    ).fetchone()
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"] or "",
        "embeddingModel": r["embed_model"] or "",
        "embedDim": r["embed_dim"],
        "createdAt": r["created_at"],
        "indexedAt": r["indexed_at"],
        "chunkCount": counts["chunks"],
        "sourceCount": counts["sources"],
    }


def create_kb(name: str, description: str = "", embed_model: str = "") -> dict:
    name = (name or "").strip() or "Untitled knowledge base"
    kb_id = uuid.uuid4().hex[:12]
    now = _now()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO kb (id, name, description, embed_model, embed_dim, created_at, indexed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (kb_id, name, (description or "").strip(), (embed_model or "").strip() or None, None, now, None),
        )
        r = conn.execute("SELECT * FROM kb WHERE id=?", (kb_id,)).fetchone()
        return _kb_row(conn, r)


def list_kbs() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM kb ORDER BY created_at DESC").fetchall()
        return [_kb_row(conn, r) for r in rows]


def get_kb(kb_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM kb WHERE id=?", (kb_id,)).fetchone()
        return _kb_row(conn, r) if r else None


def delete_kb(kb_id: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM kb WHERE id=?", (kb_id,))
        # Chunks cascade via FK, but delete explicitly in case PRAGMA is off.
        conn.execute("DELETE FROM kb_chunk WHERE kb_id=?", (kb_id,))
    return cur.rowcount > 0


def _mark_indexed(conn: sqlite3.Connection, kb_id: str, embed_model: str, dim: int) -> None:
    conn.execute(
        "UPDATE kb SET indexed_at=?, embed_model=COALESCE(NULLIF(?, ''), embed_model), "
        "embed_dim=? WHERE id=?",
        (_now(), (embed_model or "").strip(), dim, kb_id),
    )


# --------------------------------------------------------------------------- #
# Sources & chunks
# --------------------------------------------------------------------------- #

def add_chunks(kb_id: str, source: str, chunks: list[tuple[str, list[float]]],
               embed_model: str = "") -> int:
    """Store ``(text, embedding)`` pairs for one source file under ``kb_id``.

    Replaces any existing chunks for the same source (re-ingest is idempotent).
    Returns the number of chunks written.
    """
    source = (source or "").strip() or "untitled"
    now = _now()
    dim = len(chunks[0][1]) if chunks else 0
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM kb_chunk WHERE kb_id=? AND source=?", (kb_id, source))
        for ordinal, (text, vec) in enumerate(chunks):
            conn.execute(
                "INSERT INTO kb_chunk (id, kb_id, source, ordinal, text, embedding, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], kb_id, source, ordinal, text, json.dumps(vec), now),
            )
        if chunks:
            _mark_indexed(conn, kb_id, embed_model, dim)
    return len(chunks)


def list_sources(kb_id: str) -> list[dict]:
    """One row per distinct source file with its chunk count."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS chunks, MAX(created_at) AS created_at "
            "FROM kb_chunk WHERE kb_id=? GROUP BY source ORDER BY created_at DESC",
            (kb_id,),
        ).fetchall()
        return [{"source": r["source"], "chunkCount": r["chunks"], "createdAt": r["created_at"]}
                for r in rows]


def delete_source(kb_id: str, source: str) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM kb_chunk WHERE kb_id=? AND source=?", (kb_id, source))
    return cur.rowcount


def list_chunks(kb_id: str, limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, source, ordinal, text FROM kb_chunk WHERE kb_id=? "
            "ORDER BY source, ordinal LIMIT ?",
            (kb_id, max(1, int(limit))),
        ).fetchall()
        return [{"id": r["id"], "source": r["source"], "ordinal": r["ordinal"], "text": r["text"]}
                for r in rows]


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

def search(kb_id: str, query_vec: list[float], k: int = 5) -> list[dict]:
    """Brute-force cosine search over one KB's stored vectors.

    Takes an already-embedded ``query_vec`` (embedding is done by the caller so
    this stays pure + offline-testable) and returns the top-``k`` chunks, each
    ``{id, source, ordinal, text, score}``, highest score first.
    """
    if not query_vec:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, source, ordinal, text, embedding FROM kb_chunk WHERE kb_id=?",
            (kb_id,),
        ).fetchall()
    scored: list[dict] = []
    for r in rows:
        try:
            vec = json.loads(r["embedding"])
        except (TypeError, ValueError):
            continue
        scored.append({
            "id": r["id"],
            "source": r["source"],
            "ordinal": r["ordinal"],
            "text": r["text"],
            "score": cosine(query_vec, vec),
        })
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:max(1, int(k))]
