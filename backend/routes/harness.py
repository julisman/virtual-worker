"""Coding harness (Claude Code) API.

  GET  /api/harness/status            availability + settings
  GET  /api/harness/settings
  PUT  /api/harness/settings
  GET  /api/harness/sessions          ?limit&project
  GET  /api/harness/sessions/{id}     ?turns
  POST /api/harness/run               {prompt, session?, project?}
  GET  /api/harness/jobs
  GET  /api/harness/jobs/{id}
  POST /api/harness/jobs/{id}/stop
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from harness import claude_code as cc

router = APIRouter()


class SettingsIn(BaseModel):
    bin: str | None = None
    permissionMode: str | None = None
    maxTurns: int | None = None
    timeoutS: int | None = None
    model: str | None = None
    allowedRoots: list[str] | None = None
    maxResultChars: int | None = None


class RunIn(BaseModel):
    prompt: str
    session: str | None = None
    project: str | None = None


@router.get("/status")
async def status():
    return {**cc.availability(), "settings": cc.get_settings(),
            "running": sum(1 for j in cc.JOBS.values() if j.status == "running")}


@router.get("/settings")
async def get_settings():
    return cc.get_settings()


@router.put("/settings")
async def put_settings(body: SettingsIn):
    try:
        return cc.set_settings(body.model_dump(exclude_none=True))
    except (cc.HarnessError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@router.get("/sessions")
async def sessions(limit: int = 20, project: str | None = None):
    return {"sessions": cc.list_sessions(limit=max(1, min(limit, 100)), project=project)}


@router.get("/sessions/{session_id}")
async def session(session_id: str, turns: int = 8):
    s = cc.resolve_session(session_id)
    d = cc.session_detail(s["id"], turns=max(0, min(turns, 40))) if s else None
    if not d:
        raise HTTPException(404, "no such session")
    return d


@router.post("/run")
async def run(body: RunIn):
    try:
        job = await cc.start(body.prompt, session=body.session or None, project=body.project or None,
                             origin={"source": "api"})
    except cc.HarnessError as exc:
        raise HTTPException(400, str(exc))
    return job.view()


@router.get("/jobs")
async def jobs(limit: int = 20):
    return {"jobs": cc.list_jobs(limit=max(1, min(limit, 50)))}


@router.get("/jobs/{job_id}")
async def job(job_id: str):
    j = cc.get_job(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j.view()


@router.post("/jobs/{job_id}/stop")
async def stop(job_id: str):
    j = await cc.stop(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j.view()
