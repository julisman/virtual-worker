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

from agent import loop
from agent.compose import compose
from agent.mcp_client import McpManager
from gateway.models import resolve_model
from gateway.sse import sse_event
from storage import agent_store, mcp_store

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
    tools: list[str] | None = None  # override the agent's built-in tools (optional)
    agentId: str | None = None
    system: str | None = None  # extra per-chat instruction appended to the persona


def _to_wire(m: Msg) -> dict:
    if m.images:
        return {"role": m.role, "content": [
            {"type": "text", "text": m.content},
            *({"type": "image_url", "image_url": {"url": u}} for u in m.images),
        ]}
    return {"role": m.role, "content": m.content}


def _agent_mcp_servers(agent: dict | None) -> list[dict]:
    """Resolve the agent's attached MCP server ids to their stored configs."""
    ids = (agent or {}).get("mcpServers") or []
    return [s for s in (mcp_store.get_server(i) for i in ids) if s]


@router.post("")
async def chat(request: Request, req: ChatRequest):
    agent = agent_store.get_agent(req.agentId) if req.agentId else agent_store.get_default_agent()
    model = req.model or (agent or {}).get("model") or await resolve_model(None)
    system, registry, temperature, top_p, top_k = compose(
        agent, extra_system=req.system, tools_override=req.tools)
    if req.temperature is not None:
        temperature = req.temperature
    mcp_servers = _agent_mcp_servers(agent)
    messages = [_to_wire(m) for m in req.messages]
    agent_id = (agent or {}).get("id")
    max_iterations = ((agent or {}).get("guardrails") or {}).get("maxIterations")
    # Tie this conversation's call trail to its chat session in Debug.
    session_id = request.headers.get("x-vw-session") or "chat:adhoc"

    async def gen():
        # MCP sessions stay open for the whole stream so the loop can call their
        # tools, then close when the request ends.
        async with McpManager() as mcp:
            registry.update(await mcp.build_registry(mcp_servers))
            async for event in loop.run(
                messages, model, registry, temperature, session_id=session_id, system=system,
                top_p=top_p, top_k=top_k, agent_id=agent_id, max_iterations=max_iterations,
            ):
                yield sse_event(event, event=event.get("type"))

    return StreamingResponse(gen(), media_type="text/event-stream")
