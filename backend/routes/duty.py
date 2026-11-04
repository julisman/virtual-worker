# Duty — start/stop the always-on worker for an intern and report what it's doing.

"""POST /api/agents/{id}/duty {status: "on"|"off"} — put the intern on/off duty.
GET  /api/agents/{id}/duty — current duty status + the task it's working (if any).

"On" launches the background worker loop (agent/worker.py); "off" is the kill
switch — it cancels the loop promptly. Duty defaults to off and never auto-starts
on app launch (decision D2).
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.worker import manager
from storage import agent_store

logger = logging.getLogger(__name__)

router = APIRouter()


class DutyIn(BaseModel):
    status: str  # "on" | "off"


@router.get("/{agent_id}/duty")
async def get_duty(agent_id: str):
    agent = agent_store.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "no such agent")
    return {
        "dutyStatus": agent.get("dutyStatus") or "off",
        "running": manager.is_running(agent_id),
        "current": manager.current(agent_id),
    }


@router.post("/{agent_id}/duty")
async def set_duty(agent_id: str, body: DutyIn):
    agent = agent_store.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "no such agent")
    if not (agent.get("role") or "").strip():
        raise HTTPException(400, "give the agent a job role first — only interns go on duty")
    if body.status == "on":
        await manager.start(agent_id)
    elif body.status == "off":
        await manager.stop(agent_id)
    else:
        raise HTTPException(400, "status must be 'on' or 'off'")
    return await get_duty(agent_id)
