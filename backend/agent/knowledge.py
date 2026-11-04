"""Knowledge Base embedding + retrieval tool (PRD-03).

The storage + cosine math live in ``storage/kb_store``; this module adds the one
piece that needs the network — turning text into vectors via the **local**
OpenAI-compatible ``/v1/embeddings`` gateway — plus the agent-facing glue:

  * ``embed(texts)`` — POST to the configured upstream's ``/v1/embeddings`` and
    return one float vector per input. Raises ``EmbeddingUnavailable`` (with a
    clear message) when no embedding model is being served, so callers can
    degrade gracefully instead of crashing.
  * ``ingest_text`` — chunk a document, embed each chunk, store the vectors.
  * ``search_kbs`` — embed a query and rank chunks across one or more KBs.
  * ``make_kb_tools(agent)`` — a ``kb_search`` tool (mirrors ``make_memory_tools``)
    that searches the KBs attached to the agent and returns cited passages.

Everything is local-first: embeddings are computed by the same gateway the rest
of Virtual Worker talks to; nothing is sent to a cloud embedding API.
"""

from __future__ import annotations

import logging

import httpx

from config import UPSTREAM_URL
from storage import kb_store

logger = logging.getLogger(__name__)

# Embedding calls are short; a wedged upstream should surface fast, not hang.
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)


class EmbeddingUnavailable(Exception):
    """No local embedding model is being served (or the gateway is unreachable).

    Carries a user-facing message so routes/tools can show a clear CTA instead
    of a stack trace — KB features degrade gracefully when embeddings are off.
    """


async def embed(texts: list[str], model: str = "") -> list[list[float]]:
    """Embed ``texts`` via the local ``/v1/embeddings`` gateway.

    Returns one vector per input, in order. Raises ``EmbeddingUnavailable`` if
    the upstream is unreachable, returns a non-2xx, or yields no embeddings
    (e.g. the active model isn't an embedding model)."""
    inputs = [t for t in (texts or []) if isinstance(t, str)]
    if not inputs:
        return []
    payload: dict = {"input": inputs}
    if (model or "").strip():
        payload["model"] = model.strip()
    url = f"{UPSTREAM_URL.rstrip('/')}/v1/embeddings"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise EmbeddingUnavailable(
            f"Couldn't reach the local embedding model at {UPSTREAM_URL}: {exc}. "
            "Start an embedding model in Models/Hub, then retry."
        ) from exc
    if resp.status_code // 100 != 2:
        raise EmbeddingUnavailable(
            f"The embedding endpoint returned {resp.status_code}. Make sure the active "
            f"local model supports embeddings (served with --embedding). Detail: {resp.text[:200]}"
        )
    try:
        data = resp.json().get("data") or []
    except ValueError as exc:
        raise EmbeddingUnavailable(f"Embedding response wasn't valid JSON: {exc}") from exc
    if not data:
        raise EmbeddingUnavailable(
            "The embedding model returned no vectors. Is an embedding model active?"
        )
    # Sort by the OpenAI "index" field so order matches the inputs we sent.
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    vectors = [list(map(float, d.get("embedding") or [])) for d in ordered]
    if any(not v for v in vectors):
        raise EmbeddingUnavailable("The embedding model returned an empty vector.")
    return vectors


async def ingest_text(kb_id: str, source: str, text: str, model: str = "") -> int:
    """Chunk ``text``, embed every chunk, and store the vectors. Returns the
    chunk count. Raises ``EmbeddingUnavailable`` if embeddings can't be made."""
    chunks = kb_store.chunk_text(text)
    if not chunks:
        return 0
    vectors = await embed(chunks, model=model)
    pairs = list(zip(chunks, vectors))
    return kb_store.add_chunks(kb_id, source, pairs, embed_model=model)


async def search_kbs(kb_ids: list[str], query: str, k: int = 5, model: str = "") -> list[dict]:
    """Embed ``query`` once, search each KB in ``kb_ids``, and return the global
    top-``k`` chunks (each tagged with its ``kbId``), highest score first."""
    query = (query or "").strip()
    if not query or not kb_ids:
        return []
    vec = (await embed([query], model=model))[0]
    hits: list[dict] = []
    for kb_id in kb_ids:
        for c in kb_store.search(kb_id, vec, k=k):
            hits.append({**c, "kbId": kb_id})
    hits.sort(key=lambda c: c["score"], reverse=True)
    return hits[:max(1, int(k))]


def build_kb_context(knowledge_bases: list[str]) -> str:
    """A short system-prompt note telling the agent it has a KB + the tool to use."""
    if not knowledge_bases:
        return ""
    return (
        "## Knowledge base\n"
        "You have a knowledge base of the user's own documents. When a question might "
        "be answered by those documents, call `kb_search(query, k)` to retrieve the most "
        "relevant passages, then ground your answer in them and cite the source file each "
        "passage came from (shown as `[source]`). Don't invent sources."
    )


def make_kb_tools(agent: dict | None) -> dict[str, dict]:
    """Build the ``{name: {schema, fn}}`` registry with a ``kb_search`` tool that
    searches the KBs attached to ``agent``. Mirrors ``make_memory_tools``."""
    kb_ids = list((agent or {}).get("knowledgeBases") or [])

    async def kb_search(query: str = "", k: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            return "kb_search needs 'query' — what to look up in the knowledge base."
        if not kb_ids:
            return "No knowledge base is attached to this agent."
        try:
            hits = await search_kbs(kb_ids, query, k=int(k) if k else 5)
        except EmbeddingUnavailable as exc:
            return f"kb_search unavailable: {exc}"
        if not hits:
            return f"No passages in the knowledge base matched {query!r}."
        lines = [f"Top {len(hits)} passage(s) for {query!r}:"]
        for h in hits:
            lines.append(
                f"\n[{h['source']}] (score {h['score']:.2f})\n{h['text'].strip()}"
            )
        return "\n".join(lines)

    return {
        "kb_search": {
            "fn": kb_search,
            "schema": {
                "type": "function",
                "function": {
                    "name": "kb_search",
                    "description": "Search the user's attached knowledge base for passages "
                                   "relevant to a query. Returns ranked passages with their "
                                   "source file so you can cite them. Use this to answer "
                                   "questions about the user's own documents.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "what to look up"},
                            "k": {"type": "integer",
                                  "description": "how many passages to return (default 5)"},
                        },
                        "required": ["query"],
                    },
                },
            },
        },
    }
