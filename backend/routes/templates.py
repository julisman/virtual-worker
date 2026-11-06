# Templates — one-click deployable virtual worker blueprints (PRD-05).

"""GET /api/templates, GET /api/templates/{id}, POST /api/templates/{id}/deploy,
GET /api/templates/from-agent/{agentId}, POST /api/templates/import,
DELETE /api/templates/{id}.

Local only, unauthenticated like the rest of /api/*. A template bundles a
persona + recommended model/tools/skills/guardrails + a typed variable schema;
deploy stamps it into a real agent via agent_store; export reads an existing
agent back out (secrets scrubbed); import validates and persists to disk.
"""

import logging

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from storage import template_store

logger = logging.getLogger(__name__)

router = APIRouter()


class DeployIn(BaseModel):
    variables: dict | None = None
    secrets: dict[str, str] = {}
    adoptions: list[dict] = []


class UpdateAllIn(BaseModel):
    confirmDuty: bool = False


class ExportQuery(BaseModel):
    name: str | None = None
    includeStarters: bool = False


@router.get("")
async def list_templates():
    return {"templates": template_store.list_templates()}


@router.get("/from-agent/{agent_id}")
async def export_from_agent(agent_id: str, name: str | None = None,
                            includeStarters: bool = False,
                            targetTemplateId: str | None = None,
                            templateId: str | None = None,
                            version: str | None = None):
    """Export an existing agent AS a template (JSON). Secrets are scrubbed.

    Publish flow (M1.5, FR-15c): ``targetTemplateId`` embeds an installed
    template's lineage so the result updates it in place on import; ``version``
    stamps the manifest's semver. Without a target the export carries no lineage.
    """
    if targetTemplateId and templateId:
        raise HTTPException(
            400, "pass either targetTemplateId (update an existing lineage) or "
            "templateId (id for a new template), not both")
    target_source_id = None
    target_template_id = None
    if targetTemplateId:
        target = template_store.get_template(targetTemplateId)
        if not target:
            raise HTTPException(404, f"no such template: {targetTemplateId}")
        if target.get("source") == "builtin":
            raise HTTPException(
                400, "built-in templates are read-only and have no lineage")
        target_source_id = target["sourceId"]
        target_template_id = target["templateId"]
    try:
        tpl = template_store.template_from_agent(
            agent_id, name=name, include_starters=includeStarters,
            target_source_id=target_source_id,
            target_template_id=target_template_id,
            new_template_id=(templateId or None), version=version)
    except template_store.TemplateError as exc:
        raise HTTPException(400, str(exc)) from exc
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


@router.get("/{template_id}/download")
async def download_template(template_id: str):
    """Download the stored ``template.json`` (M1.5, FR-15c).

    User/registry templates serve byte-for-byte (lineage intact); built-ins
    serve the normalized manifest (no lineage). 404 for unknown ids.
    """
    result = template_store.download_template(template_id)
    if not result:
        raise HTTPException(404, "no such template")
    return Response(
        content=result["content"],
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{result["filename"]}"',
        },
    )


@router.get("/{template_id}/rollout")
async def template_rollout(template_id: str):
    """Show the version/digest rollout across agents in a template lineage."""
    from storage import agent_store
    template = template_store.get_template(template_id)
    if not template:
        raise HTTPException(404, "no such template")
    lineage = (template.get("sourceId"), template.get("templateId"))
    agents = []
    for agent in agent_store.list_agents():
        ref = agent.get("templateRef") or {}
        if (ref.get("sourceId"), ref.get("templateId")) != lineage:
            continue
        agents.append({
            "agentId": agent["id"], "name": agent["name"],
            "version": ref.get("version"), "commit": ref.get("commit", ""),
            "digest": ref.get("digest", ""),
            "status": ("behind" if template_store.semver_compare(
                template.get("version", "1.0.0"), ref.get("version") or "0.0.0") > 0
                       else "up-to-date"),
            "onDuty": agent.get("dutyStatus") == "active",
        })
    return {"template": {
        "templateId": template.get("templateId"), "sourceId": template.get("sourceId"),
        "version": template.get("version"), "digest": template.get("digest"),
        "name": template.get("name"),
    }, "agents": agents}


@router.get("/{template_id}")
async def get_template(template_id: str):
    t = template_store.get_template(template_id)
    if not t:
        raise HTTPException(404, "no such template")
    return t


@router.post("/{template_id}/deploy-preview")
async def deploy_preview(template_id: str, body: DeployIn):
    """Read-only preview of a deploy: unified resource rows (reuse/adopt/create)
    plus which secret variables still need entry after reuse (PRD-29)."""
    from storage import apply_template
    result = apply_template.dry_run_deploy(
        template_id, body.variables, body.secrets, body.adoptions)
    if result is None:
        raise HTTPException(404, "no such template")
    return result


@router.post("/{template_id}/deploy")
async def deploy_template(template_id: str, body: DeployIn):
    """Deploy a template → create a new agent, returning it."""
    try:
        agent = template_store.deploy_template(
            template_id, body.variables or {}, body.secrets, body.adoptions)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if agent is None:
        raise HTTPException(404, "no such template")
    return agent


@router.post("/{template_id}/update-all")
async def update_all_route(template_id: str, body: UpdateAllIn):
    from storage import apply_template
    try:
        return apply_template.update_all(template_id, confirm_duty=body.confirmDuty)
    except apply_template.ApplyError as error:
        raise HTTPException(
            error.status,
            detail={"code": error.code, "message": error.message},
        )


@router.delete("/{template_id}")
async def delete_template(template_id: str):
    if not template_store.delete_template(template_id):
        raise HTTPException(404, "no such template, or it is a read-only built-in")
    return {"deleted": template_id}
