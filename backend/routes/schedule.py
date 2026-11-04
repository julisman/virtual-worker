# Schedules — the human window onto an agent's timed runs ("do X at 2pm").

"""GET/POST /api/agents/{id}/schedules,
PUT/DELETE /api/agents/{id}/schedules/{scheduleId},
POST /api/agents/{id}/schedules/{scheduleId}/run  (run now).

The agent can also create/list/delete its own schedules from inside the loop via
its schedule_* tools; this route is the human side. Times are the machine's
local time. A fired schedule runs the agent's prompt with its tools — there's no
delivery target, so nothing is auto-sent (see storage/schedule_store.py)."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.cron import describe, validate
from agent.scheduler import scheduler
from storage import agent_store, schedule_store

logger = logging.getLogger(__name__)

router = APIRouter()


class ScheduleIn(BaseModel):
    title: str | None = None
    prompt: str | None = None
    kind: str | None = "cron"          # 'cron' | 'at'
    cron: str | None = None
    runAt: str | None = None
    enabled: bool | None = None
    # Explicit per-schedule delivery targets (PRD-01): [{channel, target}].
    deliverTo: list[dict] | None = None


def _require_agent(agent_id: str) -> None:
    if not agent_store.get_agent(agent_id):
        raise HTTPException(404, "no such agent")


@router.get("/{agent_id}/schedules")
async def list_schedules(agent_id: str):
    _require_agent(agent_id)
    return {"schedules": schedule_store.list_for_agent(agent_id)}


@router.post("/{agent_id}/schedules")
async def create_schedule(agent_id: str, body: ScheduleIn):
    _require_agent(agent_id)
    if not (body.prompt or "").strip():
        raise HTTPException(400, "prompt is required")
    kind = (body.kind or "cron").lower()
    try:
        s = schedule_store.create(
            agent_id, (body.title or "").strip(), body.prompt,
            kind=kind, cron_expr=body.cron, run_at=body.runAt,
            deliver_to=body.deliverTo,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return s


@router.put("/{agent_id}/schedules/{schedule_id}")
async def update_schedule(agent_id: str, schedule_id: str, body: ScheduleIn):
    _require_agent(agent_id)
    fields = body.model_dump(exclude_none=True)
    try:
        s = schedule_store.update(schedule_id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not s:
        raise HTTPException(404, "no such schedule")
    return s


@router.delete("/{agent_id}/schedules/{schedule_id}")
async def delete_schedule(agent_id: str, schedule_id: str):
    _require_agent(agent_id)
    if not schedule_store.delete(schedule_id):
        raise HTTPException(404, "no such schedule")
    return {"ok": True}


@router.post("/{agent_id}/schedules/{schedule_id}/run")
async def run_schedule(agent_id: str, schedule_id: str):
    _require_agent(agent_id)
    s = schedule_store.get(schedule_id)
    if not s:
        raise HTTPException(404, "no such schedule")
    return await scheduler.run_now(s)


@router.get("/cron/preview")
async def cron_preview(expr: str):
    """Validate a cron expression and describe it — powers the create form."""
    ok = validate(expr)
    return {"valid": ok, "text": describe(expr) if ok else None}
