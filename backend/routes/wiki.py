# Knowledge Wikis — create/browse OKF wikis + test the agent tools (PRD-13).

"""/api/wiki — Knowledge Wiki CRUD, page read/write, log, and search.

A wiki is an OKF markdown bundle on disk (``storage/wiki_store``). These routes
back the Wiki tab (list/create/browse/delete + a query tester); the agent-facing
read/write/search happens in-loop via ``agent/wiki``'s tools.
"""

import json
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from storage import wiki_store

router = APIRouter()


class WikiIn(BaseModel):
    name: str
    description: str | None = None


class PageIn(BaseModel):
    content: str


class SearchIn(BaseModel):
    query: str
    k: int | None = 5


class LinkProbeIn(BaseModel):
    repoPath: str
    wikiRoot: str | None = "."


class LinkWikiIn(LinkProbeIn):
    name: str | None = None
    description: str | None = None
    operations: dict[str, Any] | None = None
    trustTier: str | None = "repo-tooling"


class ProposalIn(BaseModel):
    kind: str
    wikiId: str | None = None
    payload: dict[str, Any] = {}


class ProposalApproveIn(BaseModel):
    checkedOps: list[str] | None = None
    trustTier: str | None = None
    note: str | None = None


class ProposalRejectIn(BaseModel):
    note: str | None = None


class OperationTrustIn(BaseModel):
    trustedOperations: list[str]


class OperationIn(BaseModel):
    inputs: dict[str, Any] | None = None


class OperationSpecIn(BaseModel):
    description: str = ""
    argv: list[str]
    cwd: str = "."
    inputs: dict[str, Any] = {}
    mode: str = "explicit_user"
    kind: str = "custom"
    timeoutSeconds: int = 30
    outputLimitBytes: int = 65536


def _require(wiki_id: str) -> dict:
    w = wiki_store.get_wiki(wiki_id)
    if not w:
        raise HTTPException(404, "no such wiki")
    return w


def _require_linked(wiki_id: str) -> dict:
    w = _require(wiki_id)
    if w.get("sourceKind") != "linked_repository":
        raise HTTPException(400, "only linked repositories have operations")
    return w


@router.get("")
async def list_wikis():
    return {"wikis": wiki_store.list_wikis()}


@router.post("")
async def create_wiki(body: WikiIn):
    if not (body.name or "").strip():
        raise HTTPException(400, "name is required")
    return wiki_store.create_wiki(body.name, body.description or "")


@router.post("/import")
async def import_wiki(file: UploadFile = File(...), name: str = Form("")):
    data = await file.read()
    filename = file.filename or ""
    try:
        title = name or filename.rsplit(".", 1)[0] or "Imported wiki"
        if filename.lower().endswith(".zip"):
            return wiki_store.import_bundle(title, data)
        return wiki_store.import_document(title, filename, data)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.post("/link-repository/probe")
async def probe_link_repository(body: LinkProbeIn):
    try:
        from storage import linked_wiki
        return linked_wiki.probe(body.repoPath, body.wikiRoot or ".")
    except (OSError, linked_wiki.LinkedWikiError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/link-repository")
async def link_repository(body: LinkWikiIn):
    try:
        # Recipe operations are discovered but always untrusted on first link.
        return wiki_store.create_linked_wiki(
            body.name or "", body.repoPath, body.wikiRoot or ".", body.description or "",
            operation_overrides=body.operations, trust_tier=body.trustTier or "repo-tooling",
        )
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        from storage import linked_wiki
        if isinstance(exc, (OSError, linked_wiki.LinkedWikiError)):
            raise HTTPException(400, str(exc)) from exc
        raise


# --------------------------------------------------------------------------- #
# Unified approval queue (linked-wiki revamp §5). These MUST be registered
# before the /{wiki_id} catch-all so "proposals" is never captured as a wiki id
# (FastAPI matches routes in registration order — same trap as /operations/trust).
# --------------------------------------------------------------------------- #

@router.get("/proposals")
async def list_proposals(status: str = "", wikiId: str = ""):
    return {"proposals": wiki_store.list_proposals(status, wikiId or None)}


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str):
    p = wiki_store.get_proposal(proposal_id)
    if not p:
        raise HTTPException(404, "no such proposal")
    return p


@router.post("/proposals")
async def create_proposal(body: ProposalIn):
    try:
        return wiki_store.create_proposal(body.kind, body.wikiId, body.payload)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, body: ProposalApproveIn):
    p = wiki_store.get_proposal(proposal_id)
    if not p:
        raise HTTPException(404, "no such proposal")
    if p["status"] != "awaiting_approval":
        raise HTTPException(409, "proposal is not awaiting approval")
    overrides = {k: v for k, v in {"checkedOps": body.checkedOps, "trustTier": body.trustTier,
                                   "note": body.note}.items() if v is not None}
    try:
        return await wiki_store.approve_proposal(proposal_id, overrides)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, body: ProposalRejectIn):
    p = wiki_store.get_proposal(proposal_id)
    if not p:
        raise HTTPException(404, "no such proposal")
    if p["status"] != "awaiting_approval":
        raise HTTPException(409, "proposal is already resolved")
    try:
        return wiki_store.reject_proposal(proposal_id, body.note or "")
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{wiki_id}")
async def get_wiki(wiki_id: str):
    w = _require(wiki_id)
    result = {**w}
    linked = w.get("sourceKind") == "linked_repository"
    try:
        # Linked repositories can hold thousands of pages; embed only the first
        # window (lazy titles) and let the UI page through /page-list.  Managed
        # wikis keep the full listing (with frontmatter titles).
        result["pages"] = (wiki_store.list_pages_page(wiki_id, "", 0, 50)["items"]
                           if linked else wiki_store.list_pages(wiki_id))
    except wiki_store.WikiError as exc:
        # A linked checkout can be moved or temporarily unavailable.  Preserve
        # the Virtual Worker record so the UI can report that state and let the user relink
        # it instead of turning a read-only page listing into a 500.
        result["pages"] = []
        result["availabilityError"] = str(exc)
    if w.get("sourceKind") == "linked_repository":
        try:
            result.update(wiki_store.linked_status(wiki_id))
        except wiki_store.WikiError as exc:
            result["availabilityError"] = str(exc)
    return result


@router.post("/{wiki_id}/probe")
async def probe_linked_wiki(wiki_id: str):
    w = _require(wiki_id)
    if w.get("sourceKind") != "linked_repository":
        raise HTTPException(400, "only linked repositories can be probed")
    try:
        return wiki_store.probe_linked_wiki(wiki_id)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{wiki_id}/refusals")
async def list_shell_refusals(wiki_id: str):
    _require(wiki_id)
    return {"refusals": wiki_store.list_shell_refusals(wiki_id)}


@router.get("/{wiki_id}/proposals")
async def list_wiki_proposals(wiki_id: str, status: str = ""):
    _require(wiki_id)
    return {"proposals": wiki_store.list_proposals(status, wiki_id)}


@router.get("/{wiki_id}/instructions")
async def linked_instructions(wiki_id: str, path: str = ""):
    w = _require(wiki_id)
    if w.get("sourceKind") != "linked_repository":
        raise HTTPException(400, "only linked repositories have AGENTS.md instructions")
    try:
        return {"instructions": wiki_store.linked_instructions(wiki_id, path)}
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


# NOTE: /operations/trust MUST stay registered before the /operations/{name}
# catch-all so "trust" is never captured as an operation name (FastAPI matches
# routes in registration order).
@router.post("/{wiki_id}/operations/trust")
async def trust_linked_operations(wiki_id: str, body: OperationTrustIn):
    _require(wiki_id)
    try:
        return wiki_store.set_trusted_operations(wiki_id, body.trustedOperations)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.put("/{wiki_id}/operations/{name}")
async def upsert_operation(wiki_id: str, name: str, body: OperationSpecIn):
    _require_linked(wiki_id)
    try:
        return wiki_store.upsert_local_operation(wiki_id, name, body.model_dump())
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/{wiki_id}/operations/{name}")
async def delete_operation(wiki_id: str, name: str):
    _require_linked(wiki_id)
    try:
        return wiki_store.delete_local_operation(wiki_id, name)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{wiki_id}/operations/{name}")
async def run_linked_operation(wiki_id: str, name: str, body: OperationIn):
    _require(wiki_id)
    from storage import wiki_jobs
    try:
        job = await wiki_jobs.start(wiki_id, name, body.inputs or {})
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))
    return {"jobId": job.id}


@router.get("/{wiki_id}/jobs/{job_id}")
async def get_operation_job(wiki_id: str, job_id: str):
    _require(wiki_id)
    from storage import wiki_jobs
    snap = wiki_jobs.get(job_id)
    if snap and snap["wikiId"] == wiki_id:
        return snap
    row = wiki_store.get_job(wiki_id, job_id)
    if not row:
        raise HTTPException(404, "no such job")
    return row


@router.get("/{wiki_id}/jobs/{job_id}/stream")
async def stream_operation_job(wiki_id: str, job_id: str):
    _require(wiki_id)
    from storage import wiki_jobs
    snap = wiki_jobs.get(job_id)
    if snap and snap["wikiId"] == wiki_id:
        async def _events():
            async for event in wiki_jobs.subscribe(job_id):
                yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(_events(), media_type="text/event-stream")
    row = wiki_store.get_job(wiki_id, job_id)
    if not row:
        raise HTTPException(404, "no such job")

    async def _replay():
        # The live job is gone (e.g. after a restart); replay the persisted row.
        yield f"data: {json.dumps({'type': 'line', 'line': row['output']})}\n\n"
        yield f"data: {json.dumps({'type': 'end', **row})}\n\n"
    return StreamingResponse(_replay(), media_type="text/event-stream")


@router.post("/{wiki_id}/jobs/{job_id}/cancel")
async def cancel_operation_job(wiki_id: str, job_id: str):
    _require(wiki_id)
    from storage import wiki_jobs
    snap = wiki_jobs.get(job_id)
    if snap and snap["wikiId"] == wiki_id:
        return await wiki_jobs.cancel(job_id) or {}
    row = wiki_store.get_job(wiki_id, job_id)
    if not row:
        raise HTTPException(404, "no such job")
    return row


@router.delete("/{wiki_id}")
async def delete_wiki(wiki_id: str):
    if not wiki_store.delete_wiki(wiki_id):
        raise HTTPException(404, "no such wiki")
    return {"deleted": wiki_id}


@router.get("/{wiki_id}/page-list")
async def page_list(wiki_id: str, prefix: str = "", offset: int = 0, limit: int = 50):
    _require(wiki_id)
    try:
        return wiki_store.list_pages_page(wiki_id, prefix, offset, limit)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{wiki_id}/log")
async def get_log(wiki_id: str):
    _require(wiki_id)
    return {"log": wiki_store.read_log(wiki_id)}


@router.get("/{wiki_id}/pages/{path:path}")
async def get_page(wiki_id: str, path: str):
    _require(wiki_id)
    try:
        content = wiki_store.read_page(wiki_id, path)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))
    if content is None:
        raise HTTPException(404, "no such page")
    return {"path": path, "content": content}


@router.put("/{wiki_id}/pages/{path:path}")
async def put_page(wiki_id: str, path: str, body: PageIn):
    _require(wiki_id)
    try:
        return wiki_store.write_page(wiki_id, path, body.content)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/{wiki_id}/pages/{path:path}")
async def delete_page(wiki_id: str, path: str):
    _require(wiki_id)
    try:
        removed = wiki_store.delete_page(wiki_id, path)
    except wiki_store.WikiError as exc:
        raise HTTPException(400, str(exc))
    if not removed:
        raise HTTPException(404, "no such page")
    return {"deleted": path}


@router.get("/{wiki_id}/lint")
async def lint(wiki_id: str):
    _require(wiki_id)
    return wiki_store.lint(wiki_id)


@router.get("/{wiki_id}/graph")
async def graph(wiki_id: str):
    _require(wiki_id)
    return wiki_store.link_graph(wiki_id)


@router.get("/{wiki_id}/export")
async def export(wiki_id: str):
    w = _require(wiki_id)
    data = wiki_store.export_bundle(wiki_id)
    safe = "".join(c for c in w["name"] if c.isalnum() or c in "-_") or "wiki"
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'})


@router.post("/{wiki_id}/search")
async def search(wiki_id: str, body: SearchIn):
    _require(wiki_id)
    return {"results": wiki_store.search_pages(wiki_id, body.query, k=body.k or 5)}
