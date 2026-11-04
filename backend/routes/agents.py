# Agents — create/manage local agents (persona + tools + skills).

"""GET/POST /api/agents, GET/PUT/DELETE /api/agents/{id}.

Local only, unauthenticated like the rest of /api/*. The Chat page runs as one
of these agents; a default "Assistant" is seeded on first access.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import agent_store

logger = logging.getLogger(__name__)

router = APIRouter()


class AgentIn(BaseModel):
    name: str | None = None
    description: str | None = None
    persona: str | None = None
    model: str | None = None
    temperature: float | None = None
    topP: float | None = None
    topK: int | None = None
    workingDir: str | None = None
    role: str | None = None
    dutyStatus: str | None = None
    guardrails: dict | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcpServers: list[str] | None = None
    knowledgeBases: list[str] | None = None


@router.get("")
async def list_agents():
    return {"agents": agent_store.list_agents()}


@router.post("")
async def create_agent(body: AgentIn):
    return agent_store.create_agent(body.model_dump(exclude_none=True))


@router.get("/{agent_id}")
async def get_agent(agent_id: str):
    a = agent_store.get_agent(agent_id)
    if not a:
        raise HTTPException(404, "no such agent")
    return a


@router.put("/{agent_id}")
async def update_agent(agent_id: str, body: AgentIn):
    a = agent_store.update_agent(agent_id, body.model_dump(exclude_unset=True))
    if not a:
        raise HTTPException(404, "no such agent")
    return a


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    if agent_id == agent_store.DEFAULT_AGENT_ID:
        raise HTTPException(400, "cannot delete the default agent")
    if not agent_store.delete_agent(agent_id):
        raise HTTPException(404, "no such agent")
    return {"deleted": agent_id}
