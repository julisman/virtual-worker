# Activity — the intern's work journal + a daily standup summary.

"""GET /api/agents/{id}/activity  — the feed (filters: day, type, task).
GET /api/agents/{id}/standup     — a rule-based summary of a day's work.

The standup rolls up the activity feed into the kind of update an intern would
give at standup: what moved, what it touched, what it's waiting on. Rule-based
in M1 (no extra LLM call); see docs/INTERN_AGENT_PRD.md decision D4.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from storage import activity_store, agent_store, task_store

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_agent(agent_id: str) -> None:
    if not agent_store.get_agent(agent_id):
        raise HTTPException(404, "no such agent")


@router.get("/{agent_id}/activity")
async def feed(agent_id: str, day: str | None = None, type: str | None = None,
               task: str | None = None, limit: int = 200):
    _require_agent(agent_id)
    return {"activities": activity_store.list_for(agent_id, day=day, type=type, task_id=task, limit=limit)}


@router.get("/{agent_id}/standup")
async def standup(agent_id: str, day: str | None = None):
    _require_agent(agent_id)
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    acts = activity_store.list_for(agent_id, day=day, limit=1000)

    by_type: dict[str, int] = {}
    for a in acts:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1

    counts = task_store.counts(agent_id)
    moved_done = [a["summary"] for a in acts if a["type"] == "task_moved" and "→ done" in a["summary"]]
    moved_review = [a["summary"] for a in acts if a["type"] == "task_moved" and "→ review" in a["summary"]]
    created = [a["summary"] for a in acts if a["type"] == "task_created"]
    files = by_type.get("file_change", 0)
    tools = by_type.get("tool_call", 0)
    errors = by_type.get("error", 0)

    # A short human line — the kind of thing you'd hear at standup.
    parts: list[str] = []
    if not acts:
        parts.append("No activity recorded today.")
    else:
        if moved_done:
            parts.append(f"completed {len(moved_done)} task(s)")
        if moved_review:
            parts.append(f"sent {len(moved_review)} for review")
        if created:
            parts.append(f"filed {len(created)} new task(s)")
        if files:
            parts.append(f"changed {files} file(s)")
        if tools:
            parts.append(f"ran {tools} tool action(s)")
        if errors:
            parts.append(f"hit {errors} error(s)")
    headline = "Today I " + ", ".join(parts) + "." if parts and acts else (parts[0] if parts else "")

    waiting = counts.get("review", 0) + counts.get("blocked", 0)
    return {
        "day": day,
        "headline": headline,
        "byType": by_type,
        "taskCounts": counts,
        "waitingOnMentor": waiting,
        "highlights": (moved_done + moved_review + created)[:10],
        "totalActivities": len(acts),
    }
