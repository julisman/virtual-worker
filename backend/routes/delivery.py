# Outbound delivery & notification channels (PRD-01) — the human + API side.

"""Channel config CRUD, the audit log, and a programmatic send endpoint.

  GET/POST          /api/channels
  PATCH/DELETE      /api/channels/{id}
  POST              /api/channels/{id}/test     — send a test delivery
  POST              /api/deliver                — explicit-target send
  GET               /api/deliveries             — audit log (filterable)

Local-only and unauthenticated like the rest of /api/*. Network channels
(webhook) are opt-in: a URL only becomes reachable once it's in an *enabled*
webhook channel's allow-list. Every send — here or from a tool/schedule — writes
a row to the deliveries audit log.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.delivery import deliver as do_deliver
from storage import channel_store

logger = logging.getLogger(__name__)

router = APIRouter()


class ChannelIn(BaseModel):
    kind: str | None = None
    name: str | None = None
    enabled: bool | None = None
    config: dict | None = None


class DeliverIn(BaseModel):
    channel: str
    target: str | None = None
    text: str
    subject: str | None = None
    data: dict | None = None
    agentId: str | None = None


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #

@router.get("/delivery-targets")
async def list_channels():
    return {"channels": channel_store.list_channels()}


@router.post("/delivery-targets")
async def create_channel(body: ChannelIn):
    if not (body.kind or "").strip():
        raise HTTPException(400, "kind is required")
    try:
        return channel_store.create_channel(
            body.kind, body.name or body.kind,
            enabled=bool(body.enabled), config=body.config or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.patch("/delivery-targets/{channel_id}")
async def update_channel(channel_id: str, body: ChannelIn):
    fields = body.model_dump(exclude_none=True)
    fields.pop("kind", None)  # kind is immutable
    c = channel_store.update_channel(channel_id, **fields)
    if not c:
        raise HTTPException(404, "no such channel")
    return c


@router.delete("/delivery-targets/{channel_id}")
async def delete_channel(channel_id: str):
    if not channel_store.delete_channel(channel_id):
        raise HTTPException(404, "no such channel")
    return {"ok": True}


@router.post("/delivery-targets/{channel_id}/test")
async def test_channel(channel_id: str, body: DeliverIn | None = None):
    c = channel_store.get_channel(channel_id)
    if not c:
        raise HTTPException(404, "no such channel")
    target = (body.target if body else None) or ""
    # For webhook, default the test target to the channel's first allow-listed URL.
    if c["kind"] == "webhook" and not target:
        urls = c["config"].get("urls") or []
        target = urls[0] if urls else ""
    text = (body.text if body and body.text else "Virtual Worker test delivery — if you can read this, the channel works.")
    rec = await do_deliver(c["kind"], target, text, subject="Virtual Worker test", source="test")
    return rec


# --------------------------------------------------------------------------- #
# Deliver + audit log
# --------------------------------------------------------------------------- #

@router.post("/deliver")
async def deliver_endpoint(body: DeliverIn):
    rec = await do_deliver(
        body.channel, body.target or "", body.text, subject=body.subject,
        data=body.data, agent_id=body.agentId, source="tool")
    return rec


@router.get("/deliveries")
async def list_deliveries(agentId: str | None = None, scheduleId: str | None = None,
                          channel: str | None = None, status: str | None = None,
                          source: str | None = None, limit: int = 200):
    return {"deliveries": channel_store.list_deliveries(
        agent_id=agentId, schedule_id=scheduleId, channel=channel,
        status=status, source=source, limit=limit)}
