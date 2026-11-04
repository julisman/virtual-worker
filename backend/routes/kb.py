# Knowledge Bases — create/manage local RAG corpora (PRD-03, Milestone 1).

"""/api/kb — local Knowledge Base CRUD, ingest, and retrieval.

A KB is a named collection of ingested documents whose chunk vectors live in
``app.db``. Text/markdown files are chunked, embedded via the local
``/v1/embeddings`` gateway, and stored for semantic search — all on-machine.
PDF/docx parsing is deferred (see PRD §11 M3); only ``.txt``/``.md`` ingest at M1.
"""

import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from agent.knowledge import EmbeddingUnavailable, ingest_text, search_kbs
from storage import kb_store

logger = logging.getLogger(__name__)

router = APIRouter()

# Text types we can extract at M1. Anything else (PDF/docx/binaries) is rejected
# with a clear "deferred" message rather than ingesting garbage.
_TEXT_EXTS = (".txt", ".md", ".markdown", ".text")


class KbIn(BaseModel):
    name: str
    description: str | None = None
    embeddingModel: str | None = None


class SearchIn(BaseModel):
    query: str
    k: int | None = 5


def _require_kb(kb_id: str) -> dict:
    kb = kb_store.get_kb(kb_id)
    if not kb:
        raise HTTPException(404, "no such knowledge base")
    return kb


@router.get("")
async def list_kbs():
    return {"knowledgeBases": kb_store.list_kbs()}


@router.post("")
async def create_kb(body: KbIn):
    if not (body.name or "").strip():
        raise HTTPException(400, "name is required")
    return kb_store.create_kb(body.name, body.description or "", body.embeddingModel or "")


@router.get("/{kb_id}")
async def get_kb(kb_id: str):
    kb = _require_kb(kb_id)
    return {**kb, "sources": kb_store.list_sources(kb_id)}


@router.delete("/{kb_id}")
async def delete_kb(kb_id: str):
    if not kb_store.delete_kb(kb_id):
        raise HTTPException(404, "no such knowledge base")
    return {"deleted": kb_id}


@router.get("/{kb_id}/chunks")
async def list_chunks(kb_id: str, limit: int = 200):
    _require_kb(kb_id)
    return {"chunks": kb_store.list_chunks(kb_id, limit=limit)}


@router.post("/{kb_id}/sources")
async def add_source(kb_id: str, file: UploadFile = File(...), model: str = Form("")):
    """Upload a .txt/.md file; chunk + embed + store it under this KB."""
    kb = _require_kb(kb_id)
    name = file.filename or "document.txt"
    if not name.lower().endswith(_TEXT_EXTS):
        raise HTTPException(
            400, "only .txt and .md files are supported at this time "
                 "(PDF/docx ingest is deferred — see PRD-03 M3)")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        text = data.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"couldn't read file as text: {exc}") from exc
    embed_model = (model or "").strip() or kb.get("embeddingModel") or ""
    try:
        count = await ingest_text(kb_id, name, text, model=embed_model)
    except EmbeddingUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if count == 0:
        raise HTTPException(400, "no text to ingest (file was empty after trimming)")
    return {"source": name, "chunkCount": count, "kb": kb_store.get_kb(kb_id)}


@router.delete("/{kb_id}/sources/{source}")
async def delete_source(kb_id: str, source: str):
    _require_kb(kb_id)
    removed = kb_store.delete_source(kb_id, source)
    if not removed:
        raise HTTPException(404, "no such source in this knowledge base")
    return {"deleted": source, "chunksRemoved": removed}


@router.post("/{kb_id}/search")
async def search(kb_id: str, body: SearchIn):
    """Debug / 'ask this KB' retrieval tester — embed the query, rank chunks."""
    kb = _require_kb(kb_id)
    if not (body.query or "").strip():
        raise HTTPException(400, "query is required")
    try:
        hits = await search_kbs([kb_id], body.query, k=body.k or 5,
                                model=kb.get("embeddingModel") or "")
    except EmbeddingUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"results": hits}
