# Templates — one-click deployable Virtual Employee blueprints (PRD-05).

"""GET /api/templates, GET /api/templates/{id}, POST /api/templates/{id}/deploy,
GET /api/templates/from-agent/{agentId}, POST /api/templates/import,
DELETE /api/templates/{id}.

Local only, unauthenticated like the rest of /api/*. A template bundles a
persona + recommended model/tools/skills/guardrails + a typed variable schema;
deploy stamps it into a real agent via agent_store; export reads an existing
agent back out (secrets scrubbed); import validates and persists to disk.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import template_store

logger = logging.getLogger(__name__)

router = APIRouter()


class DeployIn(BaseModel):
    variables: dict | None = None


class ExportQuery(BaseModel):
    name: str | None = None
    includeStarters: bool = False


@router.get("")
async def list_templates():
    return {"templates": template_store.list_templates()}


@router.get("/from-agent/{agent_id}")
async def export_from_agent(agent_id: str, name: str | None = None,
                            includeStarters: bool = False):
    """Export an existing agent AS a template (JSON). Secrets are scrubbed."""
    tpl = template_store.template_from_agent(
        agent_id, name=name, include_starters=includeStarters)
    if not tpl:
        raise HTTPException(404, "no such agent")
    return tpl


@router.post("/import")
async def import_template(body: dict):
    """Import (validate + persist) a template JSON into the user gallery."""
    try:
        return template_store.import_template(body)
    except template_store.TemplateError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{template_id}")
async def get_template(template_id: str):
    t = template_store.get_template(template_id)
    if not t:
        raise HTTPException(404, "no such template")
    return t


@router.post("/{template_id}/deploy")
async def deploy_template(template_id: str, body: DeployIn):
    """Deploy a template → create a new agent, returning it."""
    agent = template_store.deploy_template(template_id, body.variables or {})
    if agent is None:
        raise HTTPException(404, "no such template")
    return agent


@router.delete("/{template_id}")
async def delete_template(template_id: str):
    if not template_store.delete_template(template_id):
        raise HTTPException(404, "no such template, or it is a read-only built-in")
    return {"deleted": template_id}
