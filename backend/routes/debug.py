# API-Call Debugging — read API.

"""Read API for captured LLM API-call traces (backed by app.db).

  GET    /api/debug/sessions             -> { summary, sessions }
  GET    /api/debug/sessions/{id}        -> { runs, totalEntries, summary }
  DELETE /api/debug/sessions             -> clear all
  DELETE /api/debug/sessions/{id}        -> clear one

NOTE: no auth yet — add an auth dependency before exposing beyond localhost.
"""

import logging

from fastapi import APIRouter, HTTPException

from storage import debug_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sessions")
async def list_debug_sessions():
    try:
        return {
            "summary": debug_db.summary(),
            "sessions": debug_db.list_sessions(),
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("debug: list sessions failed")
        raise HTTPException(status_code=500, detail="Failed to list debug sessions") from exc


@router.get("/sessions/{session_id}")
async def get_debug_session(session_id: str):
    try:
        data = debug_db.get_session(session_id)
        if not data["runs"]:
            raise HTTPException(status_code=404, detail="No runs for this session")
        return data
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("debug: get session failed")
        raise HTTPException(status_code=500, detail="Failed to load debug session") from exc


@router.delete("/sessions")
async def clear_all_debug():
    return {"deleted": debug_db.clear(None)}


@router.delete("/sessions/{session_id}")
async def clear_one_debug(session_id: str):
    return {"deleted": debug_db.clear(session_id)}
