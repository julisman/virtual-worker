"""Coding harness API (Claude Code and Codex).

  GET  /api/harness/status                  availability of each harness + settings
  GET  /api/harness/settings
  PUT  /api/harness/settings
  GET  /api/harness/sessions                ?harness=claude|codex&limit&project
  GET  /api/harness/sessions/{id}           ?harness&turns
  POST /api/harness/run                     {prompt, session?, project?, harness?}
  GET  /api/harness/jobs                    ?harness
  GET  /api/harness/jobs/{id}
  POST /api/harness/jobs/{id}/stop
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from harness import base
from harness import claude_code, codex  # noqa: F401 — registers both adapters

router = APIRouter()


class SettingsIn(BaseModel):
    bin: str | None = None
    permissionMode: str | None = None
    model: str | None = None
    codexBin: str | None = None
    codexSandbox: str | None = None
    codexModel: str | None = None
    maxTurns: int | None = None
    timeoutS: int | None = None
    allowedRoots: list[str] | None = None
    maxResultChars: int | None = None


class RunIn(BaseModel):
    prompt: str
    session: str | None = None
    project: str | None = None
    harness: str | None = "claude"


def _adapter(key: str | None):
    try:
        return base.get(key)
    except base.HarnessError as exc:
        raise HTTPException(400, str(exc))


@router.get("/status")
async def status():
    return {
        "harnesses": {ad.key: {"label": ad.label, **ad.availability()} for ad in base.adapters()},
        "settings": base.get_settings(),
        "running": sum(1 for j in base.JOBS.values() if j.status == "running"),
    }


@router.get("/settings")
async def get_settings():
    return base.get_settings()


@router.put("/settings")
async def put_settings(body: SettingsIn):
    try:
        return base.set_settings(body.model_dump(exclude_none=True))
    except (base.HarnessError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@router.get("/sessions")
async def sessions(harness: str = "claude", limit: int = 20, project: str | None = None):
    ad = _adapter(harness)
    return {"harness": ad.key, "sessions": ad.list_sessions(limit=max(1, min(limit, 100)), project=project)}


@router.get("/sessions/{session_id}")
async def session(session_id: str, harness: str = "claude", turns: int = 8):
    ad = _adapter(harness)
    s = ad.resolve_session(session_id)
    d = ad.session_detail(s["id"], turns=max(0, min(turns, 40))) if s else None
    if not d:
        raise HTTPException(404, "no such session")
    return d


@router.post("/run")
async def run(body: RunIn):
    ad = _adapter(body.harness)
    try:
        job = await base.start_job(ad, body.prompt, session=body.session or None,
                                   project=body.project or None, origin={"source": "api"})
    except base.HarnessError as exc:
        raise HTTPException(400, str(exc))
    return job.view()


@router.get("/jobs")
async def jobs(limit: int = 20, harness: str | None = None):
    return {"jobs": base.list_jobs(limit=max(1, min(limit, 50)), harness=harness or None)}


@router.get("/jobs/{job_id}")
async def job(job_id: str):
    j = base.get_job(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j.view()


@router.post("/jobs/{job_id}/stop")
async def stop(job_id: str):
    j = await base.stop(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j.view()
