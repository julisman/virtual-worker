# API-Call Debugging — read API.

"""Read API for captured LLM API-call traces (backed by app.db).

  GET    /api/debug/sessions             -> { summary, sessions }
  GET    /api/debug/sessions/{id}        -> { runs, totalEntries, summary }
  GET    /api/debug/runs/{run_id}        -> { sessionId, run }   (deep-link a single run)
  DELETE /api/debug/sessions             -> clear all
  DELETE /api/debug/sessions/{id}        -> clear one

NOTE: no auth yet — add an auth dependency before exposing beyond localhost.
"""

import logging

from fastapi import APIRouter, HTTPException

from storage import debug_db

logger = logging.getLogger(__name__)

router = APIRouter()


def _backfill_window(rows: list[dict]) -> None:
    """Fill ``contextWindow`` for rows recorded before the window was stored
    (or by the gateway middleware) from the cached window ladder — no network.
    Best-effort: an unknown model just leaves the meter without a percentage."""
    from agent import context_window as cw
    cache: dict[tuple, int | None] = {}
    for row in rows:
        if row.get("contextWindow") or not row.get("model"):
            continue
        prov = row.get("provider")
        key = (row["model"], prov)
        if key not in cache:
            try:
                cache[key] = cw.resolve_window_sync(
                    model=row["model"],
                    provider_id=prov if prov and prov not in ("local", "unknown") else None)[0]
            except Exception:  # noqa: BLE001
                cache[key] = None
        row["contextWindow"] = cache[key]


@router.get("/sessions")
async def list_debug_sessions():
    try:
        sessions = debug_db.list_sessions()
        _backfill_window(sessions)
        return {"summary": debug_db.summary(), "sessions": sessions}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("debug: list sessions failed")
        raise HTTPException(status_code=500, detail="Failed to list debug sessions") from exc


@router.get("/sessions/{session_id}")
async def get_debug_session(session_id: str):
    try:
        data = debug_db.get_session(session_id)
        if not data["runs"]:
            raise HTTPException(status_code=404, detail="No runs for this session")
        _backfill_window(data["runs"])
        return data
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("debug: get session failed")
        raise HTTPException(status_code=500, detail="Failed to load debug session") from exc


@router.get("/runs/{run_id}")
async def get_debug_run(run_id: str):
    try:
        data = debug_db.get_run(run_id)
        if not data:
            raise HTTPException(status_code=404, detail="No such run")
        return data
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("debug: get run failed")
        raise HTTPException(status_code=500, detail="Failed to load debug run") from exc


@router.delete("/sessions")
async def clear_all_debug():
    return {"deleted": debug_db.clear(None)}


@router.delete("/sessions/{session_id}")
async def clear_one_debug(session_id: str):
    return {"deleted": debug_db.clear(session_id)}
