"""Agent working-dir files — serve a file an agent tool wrote (e.g. a
``browser_screenshot`` PNG) back to the browser so it can be shown inline in
chat (an ``![...](...)`` markdown image the model emits).

GET /api/agents/{agent_id}/files?path=<relative path>

Confined to that agent's configured ``workingDir`` the same way the fs /
screenshot tools are (``fs_tools._resolve_root`` / ``_safe``): a ``../`` escape
or an absolute path outside the folder is rejected. Local only, unauthenticated
like the rest of /api/* (``TeamAuthMiddleware`` still gates it in team mode via
the session cookie, which a plain ``<img>`` tag sends automatically).
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from agent.fs_tools import FsError, _resolve_root, _safe
from storage import agent_store

router = APIRouter()


@router.get("/{agent_id}/files")
async def get_agent_file(agent_id: str, path: str):
    agent = agent_store.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "no such agent")
    working_dir = (agent.get("workingDir") or "").strip()
    if not working_dir:
        raise HTTPException(400, "this agent has no working folder configured")
    try:
        root = _resolve_root(working_dir)
        target = _safe(root, path)
    except FsError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not target.is_file():
        raise HTTPException(404, "no such file")
    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)
