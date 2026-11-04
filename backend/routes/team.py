# Team — the org view: every agent, its board counts, and recent delegations.

"""GET /api/team/overview — agents (id, name, role, duty, task counts) plus the
combined, cross-agent delegation feed (who asked/handed off to whom).

PRD-02 §6.6: a team is purely additive over the single-agent tables, so this is a
thin read over agent_store + task_store + activity_store — no new run engine.
"""

import logging

from fastapi import APIRouter

from storage import activity_store, agent_store, task_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/overview")
async def overview(limit: int = 50):
    agents = agent_store.list_agents()
    members = [{
        "id": a["id"],
        "name": a["name"],
        "role": a.get("role") or "",
        "dutyStatus": a.get("dutyStatus") or "off",
        "counts": task_store.counts(a["id"]),
    } for a in agents]
    delegations = activity_store.recent_across_agents(type="delegation", limit=limit)
    # Map agent id -> name so the UI can render "who → whom" without N lookups.
    names = {a["id"]: a["name"] for a in agents}
    for d in delegations:
        det = d.get("detail") or {}
        d["fromAgentId"] = det.get("from")
        d["toAgentId"] = det.get("to")
        d["fromName"] = names.get(det.get("from"))
        d["toName"] = names.get(det.get("to"))
        d["kind"] = det.get("kind")
    return {"agents": members, "delegations": delegations}
