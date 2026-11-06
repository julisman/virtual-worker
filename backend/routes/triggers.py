# Triggers — the human window onto an agent's event-driven runs (PRD-09, M1).

"""GET/POST /api/triggers, PATCH/DELETE /api/triggers/{id},
POST /api/triggers/{id}/run  (fire now / test-fire),
POST /api/triggers/hook/{token}  (inbound webhook — the only inbound surface).

A trigger fires an agent's prompt when an event happens — a file lands in a
watched folder, or a webhook is POSTed to its unguessable token URL. The fired
run executes the agent's prompt with its tools; there's no delivery target, so
nothing is auto-sent (see agent/triggers.py). The webhook endpoint is opt-in and
token-guarded — it only does anything when its trigger is enabled."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent.triggers import triggers
from storage import agent_store, trigger_store

logger = logging.getLogger(__name__)

router = APIRouter()


class TriggerIn(BaseModel):
    agentId: str | None = None
    name: str | None = None
    type: str | None = None            # 'file' | 'webhook'
    path: str | None = None            # for type='file'
    promptTemplate: str | None = None
    enabled: bool | None = None


class RunIn(BaseModel):
    event: str | None = None           # optional sample payload for test-fire


def _require_agent(agent_id: str) -> None:
    if not agent_store.get_agent(agent_id):
        raise HTTPException(404, "no such agent")


@router.get("")
async def list_triggers(agentId: str):
    _require_agent(agentId)
    return {"triggers": trigger_store.list_for_agent(agentId)}


@router.post("")
async def create_trigger(body: TriggerIn):
    if not body.agentId:
        raise HTTPException(400, "agentId is required")
    _require_agent(body.agentId)
    ttype = (body.type or "").lower()
    if ttype not in trigger_store.TYPES:
        raise HTTPException(400, f"type must be one of {trigger_store.TYPES}")
    if not (body.promptTemplate or "").strip():
        raise HTTPException(400, "promptTemplate is required")
    try:
        t = trigger_store.create(
            body.agentId, (body.name or "").strip(), ttype, body.promptTemplate,
            path=body.path,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return t


@router.patch("/{trigger_id}")
async def update_trigger(trigger_id: str, body: TriggerIn):
    fields = body.model_dump(exclude_none=True)
    fields.pop("agentId", None)
    fields.pop("type", None)  # type is immutable after creation
    try:
        t = trigger_store.update(trigger_id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not t:
        raise HTTPException(404, "no such trigger")
    return t


@router.delete("/{trigger_id}")
async def delete_trigger(trigger_id: str):
    if not trigger_store.delete(trigger_id):
        raise HTTPException(404, "no such trigger")
    return {"ok": True}


@router.post("/{trigger_id}/run")
async def run_trigger(trigger_id: str, body: RunIn | None = None):
    t = trigger_store.get(trigger_id)
    if not t:
        raise HTTPException(404, "no such trigger")
    event = (body.event if body else None) or ""
    return await triggers.run_now(t, event)


@router.post("/hook/{token}")
async def webhook(token: str, request: Request):
    """Inbound webhook receive endpoint — fires the matching webhook trigger.

    The only inbound network surface. Guarded by the per-trigger token; an
    unknown or disabled token is a 404 (don't leak which tokens exist)."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        body = (await request.body()).decode("utf-8", "replace")
        payload = body
    t = await triggers.fire_hook(token, payload)
    if t is None:
        raise HTTPException(404, "no such webhook")
    if not t["enabled"]:
        raise HTTPException(403, "trigger is disabled")
    # The run was accepted and fires in the background — don't hold the caller
    # open for the (possibly multi-minute) agent run.
    return JSONResponse({"ok": True, "accepted": True, "triggerId": t["id"]},
                        status_code=202)
