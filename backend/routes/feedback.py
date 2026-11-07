"""Feedback capture on assistant turns.

  POST   /api/feedback            record up / down / edit on a turn
  GET    /api/feedback            list (agentId, signal, sessionId)
  GET    /api/feedback/counts     tallies
  GET    /api/feedback/export     curated chat-format JSONL download
  DELETE /api/feedback/{id}       drop one signal
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response

from storage import feedback_store as fb

router = APIRouter()


@router.post("")
async def record(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "expected a JSON object")
    try:
        return fb.record(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def list_feedback(agentId: str | None = None, signal: str | None = None,
                        sessionId: str | None = None, limit: int = 500):
    return {"feedback": fb.list_feedback(agent_id=agentId, signal=signal,
                                         session_id=sessionId, limit=limit)}


@router.get("/counts")
async def counts(agentId: str | None = None):
    return fb.counts(agent_id=agentId)


@router.get("/export")
async def export(agentId: str | None = None):
    try:
        raw, summary = fb.build_dataset(agent_id=agentId)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=raw, media_type="application/jsonl",
        headers={"Content-Disposition": f'attachment; filename="feedback-{stamp}.jsonl"',
                 "X-Feedback-Examples": str(summary["examples"])},
    )


@router.delete("/{fid}")
async def delete(fid: str):
    if not fb.delete(fid):
        raise HTTPException(404, "no such feedback")
    return {"deleted": fid}
