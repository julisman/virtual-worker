# Chat sessions — persist & restore local conversations.

"""GET /api/sessions, GET/PUT/DELETE /api/sessions/{id}.

The Chat UI saves each conversation here so it survives restarts. Local only,
unauthenticated like the rest of /api/*."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import chat_store

logger = logging.getLogger(__name__)

router = APIRouter()


class MessageIn(BaseModel):
    role: str
    content: str = ""
    images: list[str] | None = None
    steps: list[dict] | None = None
    meta: dict | None = None


class SessionIn(BaseModel):
    title: str = "New chat"
    model: str | None = None
    agentId: str | None = None
    messages: list[MessageIn] = []


@router.get("")
async def list_sessions():
    return {"sessions": chat_store.list_sessions()}


@router.get("/{session_id}")
async def get_session(session_id: str):
    s = chat_store.get_session(session_id)
    if not s:
        raise HTTPException(404, "no such session")
    return s


@router.put("/{session_id}")
async def put_session(session_id: str, body: SessionIn):
    return chat_store.upsert_session(
        session_id, body.title, body.model, [m.model_dump() for m in body.messages],
        agent_id=body.agentId,
    )


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    if not chat_store.delete_session(session_id):
        raise HTTPException(404, "no such session")
    return {"deleted": session_id}
