# Chat sessions — persist & restore local conversations.

"""GET /api/sessions, GET/PUT/DELETE /api/sessions/{id}.

The Chat UI saves each conversation here so it survives restarts. Local only,
unauthenticated like the rest of /api/*."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import chat_store, reasoning_store

logger = logging.getLogger(__name__)

router = APIRouter()


class MessageIn(BaseModel):
    role: str
    content: str = ""
    images: list[str] | None = None
    steps: list[dict] | None = None
    # Ordered narration + tool-call items ({kind:"text"|"tool", ...}) — what the
    # chat UI's collapsible work panel renders. Pydantic drops unknown fields,
    # so leaving this out silently lost the panel on page refresh.
    timeline: list[dict] | None = None
    meta: dict | None = None


class SessionIn(BaseModel):
    title: str = "New chat"
    model: str | None = None
    agentId: str | None = None
    messages: list[MessageIn] = []


def _session_api_copy(session: dict) -> dict:
    """Return a UI-safe session copy without runtime-only retained skill bodies."""
    out = dict(session)
    messages = []
    for message in session.get("messages") or []:
        meta = message.get("meta")
        if not isinstance(meta, dict) or "loadedSkills" not in meta:
            messages.append(message)
            continue
        clean = dict(message)
        clean_meta = dict(meta)
        clean_meta.pop("loadedSkills", None)
        clean["meta"] = clean_meta
        messages.append(clean)
    out["messages"] = messages
    return out


def _preserve_channel_loaded_skills(session_id: str, messages: list[dict]) -> list[dict]:
    """Keep runtime-only skill bodies when the Chat UI round-trips a chat-channel
    session ("<channel>:<account>:<conversation>")."""
    if session_id.startswith("chat:") or session_id.count(":") < 2:
        return messages
    existing = chat_store.get_session(session_id)
    if not existing:
        return messages
    old_messages = existing.get("messages") or []
    by_run_id = {}
    for old in old_messages:
        old_meta = old.get("meta") or {}
        loaded = old_meta.get("loadedSkills")
        run_id = old_meta.get("runId")
        if run_id and isinstance(loaded, dict):
            by_run_id[run_id] = loaded

    preserved = []
    for index, message in enumerate(messages):
        meta = message.get("meta") or {}
        loaded = by_run_id.get(meta.get("runId"))
        if loaded is None and index < len(old_messages):
            old = old_messages[index]
            old_meta = old.get("meta") or {}
            if (old.get("role") == message.get("role")
                    and old.get("content") == message.get("content")
                    and isinstance(old_meta.get("loadedSkills"), dict)):
                loaded = old_meta["loadedSkills"]
        if loaded is None:
            preserved.append(message)
            continue
        copy = dict(message)
        copy_meta = dict(meta)
        copy_meta["loadedSkills"] = dict(loaded)
        copy["meta"] = copy_meta
        preserved.append(copy)
    return preserved


@router.get("")
async def list_sessions(agentId: str | None = None):
    return {"sessions": chat_store.list_sessions(agent_id=agentId)}


@router.get("/{session_id}")
async def get_session(session_id: str):
    s = chat_store.get_session(session_id)
    if not s:
        raise HTTPException(404, "no such session")
    return _session_api_copy(s)


@router.put("/{session_id}")
async def put_session(session_id: str, body: SessionIn):
    messages = _preserve_channel_loaded_skills(
        session_id, [m.model_dump() for m in body.messages])
    return chat_store.upsert_session(
        session_id, body.title, body.model, messages,
        agent_id=body.agentId,
    )


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    if not chat_store.delete_session(session_id):
        raise HTTPException(404, "no such session")
    try:
        reasoning_store.delete_session(session_id)
    except Exception:
        logger.debug("reasoning sidecar cleanup failed", exc_info=True)
    return {"deleted": session_id}
