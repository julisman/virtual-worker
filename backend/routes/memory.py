# Agent memory — view/manage the persistent notes an agent remembers.

"""GET/POST/DELETE /api/agents/{id}/memory, DELETE /api/agents/{id}/memory/{mem_id}.

Lets the UI show what an agent has remembered ("Your memory") and lets the user
add or prune notes by hand. The agent writes these itself at runtime via the
``memory_write`` tool; this route is the human-facing window onto the same store.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import agent_store, memory_store

logger = logging.getLogger(__name__)

router = APIRouter()


class MemoryIn(BaseModel):
    content: str
    tags: str | None = None


def _require_agent(agent_id: str) -> None:
    if not agent_store.get_agent(agent_id):
        raise HTTPException(404, "no such agent")


@router.get("/{agent_id}/memory")
async def list_memory(agent_id: str, q: str | None = None):
    _require_agent(agent_id)
    notes = memory_store.search(agent_id, q) if q else memory_store.list_for(agent_id)
    return {"memories": notes, "total": memory_store.count(agent_id)}


@router.post("/{agent_id}/memory")
async def add_memory(agent_id: str, body: MemoryIn):
    _require_agent(agent_id)
    if not body.content.strip():
        raise HTTPException(400, "content is required")
    return memory_store.add(agent_id, body.content, body.tags or "")


@router.delete("/{agent_id}/memory")
async def clear_memory(agent_id: str):
    _require_agent(agent_id)
    return {"cleared": memory_store.clear(agent_id)}


@router.delete("/{agent_id}/memory/{mem_id}")
async def delete_memory(agent_id: str, mem_id: str):
    _require_agent(agent_id)
    if not memory_store.delete(agent_id, mem_id):
        raise HTTPException(404, "no such memory")
    return {"deleted": mem_id}
