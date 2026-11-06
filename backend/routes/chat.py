# Agentic chat — run as an agent (persona + tools + skills), streamed.

"""POST /api/chat -> SSE event stream from the agentic tool loop.

The Chat UI runs *as an agent*: the agent's persona becomes the system prompt,
its enabled built-in tools + a ``load_skill`` tool (when skills are attached)
form the per-request tool registry, and its model/temperature pin generation.
No ``agentId`` falls back to the default Assistant agent (current chat behavior).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.runner import stream_run
from agent import run_registry
from agent.summarize import summarize_messages
from gateway.providers import resolve_target
from gateway.sse import sse_event
from storage import agent_store

logger = logging.getLogger(__name__)

router = APIRouter()


class Msg(BaseModel):
    role: str
    content: str = ""
    images: list[str] | None = None


class ChatRequest(BaseModel):
    messages: list[Msg]
    model: str | None = None
    temperature: float | None = None
    reasoningEffort: str | None = None  # per-chat effort override for external reasoning models
    tools: list[str] | None = None  # override the agent's built-in tools (optional)
    agentId: str | None = None
    system: str | None = None  # extra per-chat instruction appended to the persona
    preloadSkills: list[str] | None = None  # skills the user force-loaded via the chat `/` picker


class SteerRequest(BaseModel):
    runId: str
    content: str = ""


def _to_wire(m: Msg) -> dict:
    if m.images:
        return {"role": m.role, "content": [
            {"type": "text", "text": m.content},
            *({"type": "image_url", "image_url": {"url": u}} for u in m.images),
        ]}
    return {"role": m.role, "content": m.content}


@router.post("")
async def chat(request: Request, req: ChatRequest):
    agent = agent_store.get_agent(req.agentId) if req.agentId else agent_store.get_default_agent()
    messages = [_to_wire(m) for m in req.messages]
    # Tie this conversation's call trail to its chat session in Debug.
    session_id = request.headers.get("x-vw-session") or "chat:adhoc"

    async def gen():
        # The shared runner does compose + MCP wiring + guardrail kwargs; the
        # chat route only adds its per-request overrides (model/tools/temp/skills)
        # and forwards each event as SSE.
        async for event in stream_run(
            agent, messages, session_id=session_id,
            extra_system=req.system, requested_model=req.model,
            tools_override=req.tools, preload_skills=req.preloadSkills,
            temperature_override=req.temperature,
            reasoning_effort_override=req.reasoningEffort,
        ):
            # ``resultFull`` is an in-process-only field for chat-channel skill
            # retention. Never send the unbounded tool body to web clients;
            # keep the existing 600-char ``result`` field and every other key.
            wire_event = ({key: value for key, value in event.items()
                           if key != "resultFull"}
                          if "resultFull" in event else event)
            yield sse_event(wire_event, event=wire_event.get("type"))

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/steer")
async def steer(req: SteerRequest):
    return {"delivered": bool(run_registry.deliver(req.runId, req.content))}


class CompactRequest(BaseModel):
    messages: list[Msg]
    model: str | None = None
    agentId: str | None = None
    keepRecent: int = 4  # most-recent messages left verbatim after the summary


@router.post("/compact")
async def compact(req: CompactRequest):
    """Summarize the older part of a chat into a compact recap so the session fits
    the model's context window. Returns the summary text; the client rebuilds the
    history as [summary, ...last keepRecent]. Returns summary=None when there's too
    little history to be worth compacting."""
    agent = agent_store.get_agent(req.agentId) if req.agentId else agent_store.get_default_agent()
    model, target = await resolve_target(agent, req.model)
    wire = [_to_wire(m) for m in req.messages]
    keep = max(0, min(req.keepRecent, len(wire)))
    head = wire[:len(wire) - keep] if keep else wire
    head_chars = sum(len(str(m.get("content") or "")) for m in head)
    if len(head) < 2 or head_chars < 1500:
        return {"summary": None, "keptRecent": len(wire)}
    # Transcribe any inline images for a blind agent before summarizing (no status
    # event — compact is a one-shot call, not a streaming run).
    from agent import vision_helper
    head = await vision_helper.describe_message_images(agent, model, target, head)
    summary = await summarize_messages(model, head, provider=target)
    if not summary:
        return {"summary": None, "keptRecent": len(wire)}
    return {"summary": summary, "keptRecent": keep}
