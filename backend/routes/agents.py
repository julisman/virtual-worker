# Agents — create/manage local agents (persona + tools + skills).

"""GET/POST /api/agents, GET/PUT/DELETE /api/agents/{id}.

Local only, unauthenticated like the rest of /api/*. The Chat page runs as one
of these agents; a default "Assistant" is seeded on first access.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.context_window import context_window_info
from agent.prefix_lint import find_volatile
from storage import agent_store, apply_template

logger = logging.getLogger(__name__)

router = APIRouter()


def _with_lint(a: dict) -> dict:
    """Attach computed, never-stored read-only fields: the persona cache-prefix
    lint (PRD-17 FR-10; non-empty means this persona breaks prompt caching every
    run) and the resolved context window info (PRD-23 FR-18)."""
    a["personaCacheWarnings"] = find_volatile(a.get("persona") or "")
    a["contextWindowInfo"] = context_window_info(a)
    return a


class AgentIn(BaseModel):
    name: str | None = None
    description: str | None = None
    persona: str | None = None
    model: str | None = None
    modelSource: str | None = None  # "local" (default) | "byok" | "subscription"
    provider: str | None = None     # external provider id when modelSource != "local"
    cacheTtl: str | None = None     # Anthropic extended cache retention: "1h" or unset
    temperature: float | None = None
    topP: float | None = None
    topK: int | None = None
    repeatPenalty: float | None = None
    reasoningEffort: str | None = None  # "" (model default) | off | low | medium | high | xhigh
    workingDir: str | None = None
    role: str | None = None
    dutyStatus: str | None = None
    guardrails: dict | None = None
    webSearch: dict | None = None   # { provider, apiKey, searxngUrl } for browser_search
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcpServers: list[str] | None = None
    mcpToolAllow: dict | None = None  # { serverId: [toolName, ...] }; absent/empty = all tools
    knowledgeBases: list[str] | None = None
    wikis: list[str] | None = None
    customTools: list[str] | None = None
    connectors: list[str] | None = None
    connectorGrants: dict | None = None  # { connectorName: {write, merge} }; absent = read-only
    askBudget: int | None = None  # per-agent synchronous-consult cap (0..20); None = default
    visionHelperAgentId: str | None = None  # agent this one delegates image description to ("" = global default)


class ApplyIn(BaseModel):
    pins: dict | None = None
    variables: dict | None = None
    secrets: dict[str, str] = {}
    adoptions: list[dict] = []
    confirmDuty: bool = False


@router.get("")
async def list_agents():
    return {"agents": [_with_lint(a) for a in agent_store.list_agents()]}


@router.post("")
async def create_agent(body: AgentIn):
    return _with_lint(agent_store.create_agent(body.model_dump(exclude_none=True)))


@router.get("/{agent_id}")
async def get_agent(agent_id: str):
    a = agent_store.get_agent(agent_id)
    if not a:
        raise HTTPException(404, "no such agent")
    return _with_lint(a)


@router.put("/{agent_id}")
async def update_agent(agent_id: str, body: AgentIn):
    a = agent_store.update_agent(agent_id, body.model_dump(exclude_unset=True))
    if not a:
        raise HTTPException(404, "no such agent")
    return _with_lint(a)


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    if agent_id == agent_store.DEFAULT_AGENT_ID:
        raise HTTPException(400, "cannot delete the default agent")
    if not agent_store.delete_agent(agent_id):
        raise HTTPException(404, "no such agent")
    return {"deleted": agent_id}


@router.post("/{agent_id}/apply-template")
async def apply_template_route(agent_id: str, body: ApplyIn, dryRun: int = 0):
    if not agent_store.get_agent(agent_id):
        raise HTTPException(404, "no such agent")
    if dryRun:
        return apply_template.dry_run_apply(agent_id, body.variables, body.secrets, body.adoptions)
    try:
        return _with_lint(apply_template.apply(
            agent_id, body.pins, body.variables, body.confirmDuty,
            body.secrets, body.adoptions,
        ))
    except apply_template.ApplyError as error:
        raise HTTPException(
            error.status,
            detail={
                "code": error.code,
                "message": error.message,
                **(error.extra or {}),
            },
        ) from error


@router.post("/{agent_id}/restore-template")
async def restore_template_route(agent_id: str, body: ApplyIn, dryRun: int = 0):
    if not agent_store.get_agent(agent_id):
        raise HTTPException(404, "no such agent")
    if dryRun:
        return apply_template.dry_run_restore(agent_id)
    try:
        return _with_lint(apply_template.restore(
            agent_id, body.pins, body.confirmDuty
        ))
    except apply_template.ApplyError as error:
        raise HTTPException(
            error.status,
            detail={
                "code": error.code,
                "message": error.message,
                **(error.extra or {}),
            },
        ) from error
