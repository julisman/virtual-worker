"""Apply and restore the managed fields of a deployed template (PRD-29 M1)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from storage import agent_store, template_store
from storage import template_reconcile


class ApplyError(Exception):
    """An expected conflict or validation error while applying a template."""

    def __init__(self, code: str, message: str, status: int = 409,
                 extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


_MANAGED_FIELDS = (
    "persona", "role", "description", "temperature", "topP", "topK",
    "tools", "skills", "guardrails",
    "customTools", "connectors", "wikis", "knowledgeBases", "mcpServers",
    "mcpToolAllow",
)

# Attachments are ledger-scoped, not template-owned prose: the template only
# governs the resources in its ledger, so agent-local additions are neither
# "divergence" nor overwritable on update.
_ATTACHMENT_FIELDS = ("customTools", "connectors", "wikis", "knowledgeBases", "mcpServers")


def _merge_unowned_attachments(agent: dict, prior_ref: dict, resources: dict) -> dict:
    """Template-reconciled attachments plus the agent's unowned ones.

    An update may only add/replace resources the template's prior ledger owned;
    anything the user attached locally rides through untouched.
    """
    prior_owned: dict[str, set] = {
        "customTool": set(), "connector": set(), "wiki": set(),
        "knowledgeBase": set(), "mcpServer": set(),
    }
    for entry in (prior_ref.get("ledger") or []):
        if entry.get("type") in prior_owned:
            prior_owned[entry["type"]].add(entry.get("id"))
    merged = {}
    for field, ledger_type in (
        ("customTools", "customTool"), ("connectors", "connector"),
        ("wikis", "wiki"), ("knowledgeBases", "knowledgeBase"),
        ("mcpServers", "mcpServer"),
    ):
        managed = list(resources[field])
        seen = set(managed)
        for attached in (agent.get(field) or []):
            if attached not in seen and attached not in prior_owned[ledger_type]:
                managed.append(attached)
                seen.add(attached)
        merged[field] = managed
    managed_allow = dict(resources.get("mcpToolAllow") or {})
    prior_mcp = set(prior_owned["mcpServer"])
    merged_allow = dict(agent.get("mcpToolAllow") or {})
    for server_id, allow in managed_allow.items():
        merged_allow[server_id] = allow
    # An allowlist belonging to a prior ledger-owned server is replaced with
    # the new template value; all locally attached server entries survive.
    for server_id in prior_mcp:
        if server_id not in managed_allow:
            merged_allow.pop(server_id, None)
    merged["mcpToolAllow"] = merged_allow
    return merged


def _find_installed_template(ref: dict) -> dict | None:
    """Return the installed template matching an agent's source lineage."""
    source_id = ref.get("sourceId")
    template_id = ref.get("templateId")
    for template in template_store.list_templates():
        if (template.get("sourceId") == source_id
                and template.get("templateId") == template_id):
            return template
    return None


def _managed_of_agent(agent: dict) -> dict:
    """Extract exactly the fields controlled by a deployed template."""
    return {field: agent[field] for field in _MANAGED_FIELDS}


def _resolve_ctx(tpl: dict, ref: dict, extra: dict | None) -> dict:
    merged = {**(ref.get("variables") or {}), **(extra or {})}
    return template_store.resolve_variables(tpl, merged)


def _missing_variables(tpl: dict, ctx: dict) -> list[dict]:
    missing = []
    for variable in tpl.get("variables", []):
        if (variable.get("required")
                and not str(ctx.get(variable["key"]) or "").strip()):
            missing.append({
                "key": variable["key"],
                "reason": "required",
                "type": variable.get("type", "text"),
            })
    return missing


_VARIABLE_TOKEN_RE = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


def _covered_folder_variables(plan: dict) -> set[str]:
    """Folder variables that only exist to CREATE a wiki/KB.

    Covered — not asked, not required — only when EVERY wiki/KB that names the
    variable is reused (owned match, or an unowned name-match adopted this
    run) AND the template never substitutes it anywhere else (``{{token}}``
    in the persona, tool urls/headers, overrides, …). Hand-authored manifests
    may dual-use a variable; hiding it then would silently render a blank.
    Applies to deploy AND update.
    """
    usages: dict[str, list[bool]] = {}
    for section, var_field, rtype in (
            ("wikis", "pathVariable", "wiki"),
            ("knowledgeBases", "sourceFolderVariable", "knowledgeBase")):
        for item in plan.get(section, []) or []:
            variable = item.get(var_field)
            if not variable:
                continue
            adopted_now = (rtype, item.get("name")) in plan.get("adoptions", set())
            reused = item.get("existing") is not None and (
                not item.get("collision") or adopted_now)
            usages.setdefault(str(variable), []).append(reused)
    if not usages:
        return set()
    # ``pathVariable``/``sourceFolderVariable`` hold bare names, so any
    # ``{{token}}`` in the serialized template is by definition a *substitution*
    # use somewhere else — that keeps the variable asked.
    substituted = set(_VARIABLE_TOKEN_RE.findall(
        json.dumps(plan.get("template", {}), default=str)))
    return {variable for variable, reused_flags in usages.items()
            if all(reused_flags) and variable not in substituted}


def _resource_plan(tpl: dict, ctx: dict, ref: dict, secrets: dict | None,
                   adoptions: list[dict] | None) -> dict:
    return template_reconcile.validate_reconciliation(
        tpl, ctx, secrets or {}, adoptions or [], template_ref=ref,
        update_mode=True,
    )


def _resource_preview(plan: dict) -> tuple[dict, list[dict]]:
    custom = {"created": [], "updated": [], "adoptable": [],
              "retainedSecrets": [], "reentryRequired": []}
    connectors = {"created": [], "attached": [], "adoptable": [],
                  "reentryRequired": []}
    for item in plan.get("tools", []):
        key = item.get("key")
        if item.get("collision"):
            existing = item.get("existing") or {}
            custom["adoptable"].append({
                "name": item.get("name"), "resourceKey": key,
                "affectedAgents": [a.get("name") for a in
                                    template_reconcile.affected_agents(existing.get("id"))],
            })
        elif item.get("existing"):
            custom["updated"].append(key)
        else:
            custom["created"].append(key)
        if item.get("reentryHeaders"):
            custom["reentryRequired"].append(key)
    for item in plan.get("connectors", []):
        name = item.get("name")
        if item.get("collision"):
            existing = item.get("existing") or {}
            custom_key = existing.get("name")
            connectors["adoptable"].append({
                "name": name, "resourceKey": name,
                "affectedAgents": [a.get("name") for a in
                                    template_reconcile.agents_using_connector(custom_key)],
            })
        elif item.get("existing"):
            connectors["attached"].append(name)
        else:
            connectors["created"].append(name)
        if any(key in plan.get("reentrySecrets", [])
               for key in (item.get("secretBindings") or {}).values()):
            connectors["reentryRequired"].append(name)
    custom["retainedSecrets"] = list(plan.get("retainedSecrets", []))

    # Unified per-resource rows (one row = one connector/tool with its own
    # status + the secret-variable keys it needs or keeps) — the shape the
    # update modal renders; the legacy per-mechanism lists above are kept for
    # API stability.
    import re as _re
    token_re = _re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")
    template = plan.get("template", {})
    secret_vars = {v.get("key") for v in template.get("variables", [])
                   if v.get("type") == "secret"}
    # A row's secretKeys are the keys its operator still has to type: re-entry
    # (update mode) plus unresolved-missing (deploy preview mode).
    reentry = set(plan.get("reentrySecrets", [])) | set(plan.get("missingSecrets", []))
    retained = set(plan.get("retainedSecrets", []))
    raw_tools = {t.get("key"): t for t in template.get("customTools") or []}
    adoption_set = plan.get("adoptions") or set()
    resources: list[dict] = []
    for item in plan.get("tools", []):
        raw = raw_tools.get(item.get("key")) or {}
        referenced = {token for value in (raw.get("headers") or {}).values()
                      if isinstance(value, str)
                      for token in token_re.findall(value) if token in secret_vars}
        adopted_now = ("customTool", item.get("key")) in adoption_set
        if item.get("collision") and not adopted_now:
            status, affected = "adopt", [
                a.get("name") for a in template_reconcile.affected_agents(
                    (item.get("existing") or {}).get("id"))]
        elif item.get("collision"):
            status, affected = "reuse", []
        elif item.get("existing"):
            status, affected = "update", []
        else:
            status, affected = "create", []
        resources.append({
            "type": "customTool", "name": item.get("name"),
            "resourceKey": item.get("key"), "status": status,
            "adopted": bool(item.get("collision") and adopted_now),
            "affectedAgents": affected,
            "secretKeys": sorted(referenced & reentry),
            "retainedKeys": sorted(referenced & retained),
        })
    for section, kind in (("wikis", "wiki"), ("knowledgeBases", "knowledgeBase"),
                          ("mcpServers", "mcpServer")):
        for item in plan.get(section, []):
            existing = item.get("existing")
            adopted_now = (kind, item.get("name")) in adoption_set
            if kind == "mcpServer" and not existing:
                continue
            if item.get("collision") and not adopted_now:
                status = "adopt"
                existing_id = (existing or {}).get("id")
                affected = [a.get("name") for a in (
                    template_reconcile.agents_using_wiki(existing_id)
                    if kind == "wiki" else template_reconcile.agents_using_knowledge_base(existing_id))]
            elif item.get("collision"):
                status, affected = "reuse", []
            elif existing:
                status, affected = "reuse", []
            else:
                status, affected = "create", []
            resources.append({
                "type": kind, "name": item.get("name"),
                "resourceKey": item.get("name"), "status": status,
                "adopted": bool(item.get("collision") and adopted_now),
                "affectedAgents": affected, "secretKeys": [], "retainedKeys": [],
            })
    for item in plan.get("connectors", []):
        referenced = {key for key in (item.get("secretBindings") or {}).values()
                      if key in secret_vars}
        adopted_now = ("connector", item.get("name")) in adoption_set
        if item.get("collision") and not adopted_now:
            status, affected = "adopt", [
                a.get("name") for a in template_reconcile.agents_using_connector(
                    (item.get("existing") or {}).get("name"))]
        elif item.get("collision"):
            status, affected = "reuse", []
        elif item.get("existing"):
            status, affected = "reuse", []
        else:
            status, affected = "create", []
        resources.append({
            "type": "connector", "name": item.get("name"),
            "resourceKey": item.get("name"), "status": status,
            "adopted": bool(item.get("collision") and adopted_now),
            "affectedAgents": affected,
            "secretKeys": sorted(referenced & reentry),
            "retainedKeys": sorted(referenced & retained),
        })
    attention = [
        {"kind": "binding-reentry", "id": key,
         "message": f"binding target for {key} was deleted or revoked"}
        for key in plan.get("reentrySecrets", [])
    ]
    for item in plan.get("connectors", []):
        if not item.get("existing"):
            attention.append({"kind": "connector-untested", "id": item.get("name"),
                              "message": f"connector {item.get('name')} has not been tested"})
    for slug in plan.get("template", {}).get("skills", []) or []:
        if (not template_reconcile.skill_store.get_skill(slug)
                and slug not in {item.get("slug") for item in
                                 template_reconcile.skill_store.list_builtin()}):
            attention.append({"kind": "skill-unresolved", "id": slug,
                              "message": f"skill {slug} is not installed or builtin"})
    for item in plan.get("wikis", []):
        if not item.get("existing"):
            path_variable = item.get("pathVariable")
            path_value = str(plan.get("ctx", {}).get(path_variable) or "").strip() if path_variable else ""
            if not path_value or not Path(path_value).expanduser().is_dir():
                attention.append({"kind": "wiki-path-missing", "id": item.get("name"),
                                  "message": f"wiki path variable {path_variable or 'pathVariable'} is missing or unresolved"})
    for item in plan.get("knowledgeBases", []):
        if not item.get("existing"):
            attention.append({"kind": "kb-empty", "id": item.get("name"),
                              "message": "created empty — ingest your source folder"})
    for item in plan.get("mcpServers", []):
        if not item.get("existing"):
            attention.append({"kind": "mcp-server-unmatched", "id": item.get("name"),
                              "message": "not configured on this machine"})
    return {"customTools": custom, "connectors": connectors,
            "resources": resources}, attention


def dry_run_deploy(template_id: str, variables: dict | None = None,
                   secrets: dict[str, str] | None = None,
                   adoptions: list[dict] | None = None) -> dict | None:
    """Preview what a DEPLOY would set up — no store is touched.

    Returns the same unified per-resource rows the update dry-run uses, so the
    deploy dialog can offer "reuse yours" (adoption, credential kept) versus
    "create new" (credential required) before anything is typed. ``None`` for
    an unknown template.
    """
    tpl = template_store.get_template(template_id)
    if not tpl:
        return None
    ctx = template_store.resolve_variables(tpl, variables or {})
    try:
        plan = template_reconcile.validate_reconciliation(
            tpl, ctx, secrets or {}, adoptions or [], template_ref=None,
            update_mode=False, raise_on_missing=False)
    except ValueError as error:
        return {"error": str(error), "resources": [],
                "missingSecrets": [], "retainedSecrets": []}
    resource_diff, attention = _resource_preview(plan)
    covered = _covered_folder_variables(plan)
    return {
        "resources": resource_diff.get("resources", []),
        "missingSecrets": plan.get("missingSecrets", []),
        "retainedSecrets": plan.get("retainedSecrets", []),
        "coveredVariables": sorted(covered),
        "needsAttention": attention,
    }


def dry_run_apply(agent_id: str, extra_variables: dict | None = None,
                  secrets: dict[str, str] | None = None,
                  adoptions: list[dict] | None = None) -> dict:
    agent = agent_store.get_agent(agent_id)
    if not agent:
        return {"error": "not_found"}
    ref = agent.get("templateRef")
    if not ref:
        return {"hasTemplate": False, "sourceAvailable": True}
    tpl = _find_installed_template(ref)
    if not tpl:
        return {
            "hasTemplate": True,
            "sourceAvailable": False,
            "fromVersion": ref.get("version"),
        }

    ctx = _resolve_ctx(tpl, ref, extra_variables)
    current = _managed_of_agent(agent)
    rendered = template_store.render_managed_fields(tpl, ctx)
    # Resource attachments are reconciled separately; previewing managed
    # fields keeps the current attachment IDs for a stable diff shape.
    rendered.setdefault("customTools", current["customTools"])
    rendered.setdefault("connectors", current["connectors"])
    rendered.setdefault("wikis", current["wikis"])
    rendered.setdefault("knowledgeBases", current["knowledgeBases"])
    rendered.setdefault("mcpServers", current["mcpServers"])
    rendered.setdefault("mcpToolAllow", current["mcpToolAllow"])
    managed_diff = {
        field: {"old": current[field], "new": rendered[field]}
        for field in current
    }
    cur_sk, new_sk = set(current["skills"]), set(rendered["skills"])
    skills = {
        "added": sorted(new_sk - cur_sk),
        "removed": sorted(cur_sk - new_sk),
        "kept": sorted(cur_sk & new_sk),
    }

    baseline = ref.get("baseline") or {}
    local = []
    if baseline:
        for field in current:
            if field in _ATTACHMENT_FIELDS or field == "mcpToolAllow":
                continue
            if field in baseline and current[field] != baseline[field]:
                local.append({
                    "field": field,
                    "current": current[field],
                    "oldTemplate": baseline[field],
                })

    try:
        plan = _resource_plan(tpl, ctx, ref, secrets, adoptions)
        plan["priorVersion"] = ref.get("version")
        plan["attachmentBefore"] = {
            "wikis": list(agent.get("wikis") or []),
            "knowledgeBases": list(agent.get("knowledgeBases") or []),
            "mcpServers": list(agent.get("mcpServers") or []),
            "mcpToolAllow": dict(agent.get("mcpToolAllow") or {}),
        }
        resource_diff, resource_attention = _resource_preview(plan)
    except ValueError as error:
        # Dry run is still a preview for malformed presets/manifest values.
        resource_diff, resource_attention = ({"customTools": {"created": [], "updated": [], "adoptable": [], "retainedSecrets": [], "reentryRequired": []},
                                               "connectors": {"created": [], "attached": [], "adoptable": [], "reentryRequired": []},
                                               "resources": []},
                                              [{"kind": "binding-reentry", "id": "manifest", "message": str(error)}])
        plan = {"reentrySecrets": [], "retainedSecrets": []}
    covered = _covered_folder_variables(plan)
    missing_variables = [item for item in _missing_variables(tpl, ctx)
                         if item.get("type") != "secret"
                         and item.get("key") not in covered]
    for key in plan.get("reentrySecrets", []):
        missing_variables.append({"key": key, "reason": "binding-reentry", "type": "secret"})
    return {
        "hasTemplate": True,
        "sourceAvailable": True,
        "fromVersion": ref.get("version"),
        "toVersion": tpl["version"],
        "pins": {
            "digest": tpl["digest"],
            "agentRevision": agent["updatedAt"],
            "dutyStatus": agent["dutyStatus"],
        },
        "managed": managed_diff,
        "skills": skills,
        "localDivergence": local,
        "missingVariables": missing_variables,
        "hasSnapshot": bool(ref.get("snapshot")),
        "dutyActive": agent["dutyStatus"] == "active",
        **resource_diff,
        "needsAttention": resource_attention,
    }


def apply(agent_id: str, pins: dict | None, extra_variables: dict | None = None,
          confirm_duty: bool = False, secrets: dict[str, str] | None = None,
          adoptions: list[dict] | None = None) -> dict:
    agent = agent_store.get_agent(agent_id)
    if not agent:
        raise ApplyError("source_not_available", "no such agent", 404)
    ref = agent.get("templateRef")
    if not ref:
        raise ApplyError("source_not_available", "agent has no template", 409)
    tpl = _find_installed_template(ref)
    if not tpl:
        raise ApplyError(
            "source_not_available",
            "the template source is no longer available",
            409,
        )

    pins = pins or {}
    if (pins.get("digest") != tpl["digest"]
            or pins.get("agentRevision") != agent["updatedAt"]
            or pins.get("dutyStatus") != agent["dutyStatus"]):
        raise ApplyError(
            "re_preview_required",
            "the template or agent changed — re-preview required",
            409,
        )
    if agent["dutyStatus"] == "active" and not confirm_duty:
        raise ApplyError(
            "confirm_duty_required",
            "this agent is on duty now — confirm to proceed",
            409,
        )

    ctx = _resolve_ctx(tpl, ref, extra_variables)
    try:
        plan = _resource_plan(tpl, ctx, ref, secrets, adoptions)
        # Required-variable enforcement needs the plan: a folder variable
        # covered by a reused wiki/KB (owned or adopted) is not required.
        covered = _covered_folder_variables(plan)
        missing = [item for item in _missing_variables(tpl, ctx)
                   if item.get("type") != "secret"
                   and item.get("key") not in covered]
        if missing:
            raise ApplyError(
                "variables_required",
                "additional answers are required to update",
                409,
                {"missingVariables": missing},
            )
        plan["priorVersion"] = ref.get("version")
        plan["attachmentBefore"] = {
            "wikis": list(agent.get("wikis") or []),
            "knowledgeBases": list(agent.get("knowledgeBases") or []),
            "mcpServers": list(agent.get("mcpServers") or []),
            "mcpToolAllow": dict(agent.get("mcpToolAllow") or {}),
        }
        # Apply is strict about an unowned collision. The dry run carries the
        # affected-agent list so the operator can confirm adoption explicitly.
        unresolved = [item for item in plan.get("collisions", [])
                      if (item["type"], item["resourceKey"]) not in
                      {(str(a.get("type") or ""), str(a.get("resourceKey") or ""))
                       for a in (adoptions or [])}]
        if unresolved:
            raise ApplyError("adoption_required", "adoption confirmation is required",
                             409, {"collisions": unresolved})
    except ApplyError:
        raise
    except ValueError as error:
        raise ApplyError("reconciliation_invalid", str(error), 409) from error

    rendered = template_store.render_managed_fields(tpl, ctx)
    variable_types = {
        variable.get("key"): variable.get("type")
        for variable in tpl.get("variables", [])
    }
    ref_vars = {
        key: value for key, value in ctx.items()
        if key in variable_types and variable_types[key] != "secret"
    }
    prior_ref = {key: value for key, value in ref.items() if key != "snapshot"}
    source_id = ref["sourceId"]
    template_id = ref["templateId"]
    new_ref = {
        "sourceId": source_id,
        "templateId": template_id,
        "version": tpl["version"],
        "commit": "",
        "digest": tpl["digest"],
        "variables": ref_vars,
        "bindings": dict(ref.get("bindings") or {}),
        "ledger": [
            {
                "type": "skill",
                "id": skill,
                "resourceKey": skill,
                "adopted": False,
                "provenance": {
                    "sourceId": source_id,
                    "templateId": template_id,
                    "version": tpl["version"],
                },
            }
            for skill in rendered["skills"]
        ],
        "baseline": rendered,
        "snapshot": None,
    }
    journal: list[dict] = []
    try:
        resources = template_reconcile.apply_reconciliation(
            plan, template_ref=new_ref, journal=journal,
        )
        rendered.update(_merge_unowned_attachments(agent, ref, resources))
        new_ref["baseline"] = rendered
        new_ref["snapshot"] = {"managed": _managed_of_agent(agent), "ref": prior_ref,
                                "journal": journal}
        updated = agent_store.apply_managed_update(
            agent_id, rendered, new_ref, expected_updated_at=pins["agentRevision"]
        )
    except ApplyError:
        raise
    except Exception as error:
        raise ApplyError("apply_failed", f"template resource apply failed: {error}", 409) from error
    if updated is None:
        raise ApplyError(
            "re_preview_required",
            "the agent changed — re-preview required",
            409,
        )
    updated["needsAttention"] = resources["needsAttention"]
    return updated


def update_all(template_id: str, *, confirm_duty: bool = False) -> dict:
    """Update every agent deployed from one template lineage sequentially."""
    tpl = template_store.get_template(template_id)
    if tpl is None:
        raise ApplyError("source_not_available", "no such template", 404)
    if tpl.get("source") == "builtin":
        raise ApplyError(
            "read_only", "built-in templates are not deployable lineages", 400
        )

    lineage = (tpl["sourceId"], tpl["templateId"])
    results = []
    stopped = False
    for agent in agent_store.list_agents():
        ref = agent.get("templateRef") or {}
        if (ref.get("sourceId"), ref.get("templateId")) != lineage:
            continue
        if template_store.semver_compare(tpl["version"], ref.get("version")) <= 0:
            results.append({
                "agentId": agent["id"],
                "name": agent["name"],
                "status": "skipped",
                "reason": "up_to_date",
            })
            continue
        pins = {
            "digest": tpl["digest"],
            "agentRevision": agent["updatedAt"],
            "dutyStatus": agent["dutyStatus"],
        }
        try:
            apply(
                agent["id"],
                pins,
                extra_variables=None,
                confirm_duty=confirm_duty,
            )
        except ApplyError as error:
            results.append({
                "agentId": agent["id"],
                "name": agent["name"],
                "status": "failed",
                "code": error.code,
                "message": error.message,
            })
            stopped = True
            break
        results.append({
            "agentId": agent["id"],
            "name": agent["name"],
            "status": "updated",
        })

    return {
        "templateId": template_id,
        "toVersion": tpl["version"],
        "results": results,
        "stopped": stopped,
    }


def dry_run_restore(agent_id: str) -> dict:
    agent = agent_store.get_agent(agent_id)
    ref = (agent or {}).get("templateRef") or {}
    snapshot = ref.get("snapshot")
    if not snapshot:
        return {"hasSnapshot": False}
    current = _managed_of_agent(agent)
    target = snapshot.get("managed") or {}
    managed_diff = {
        field: {"old": current.get(field), "new": target.get(field)}
        for field in current
    }
    inverse = {"delete": [], "restore": [], "detach": []}
    restore_attention = []
    for entry in reversed(snapshot.get("journal") or []):
        store, resource_id, before = entry.get("store"), entry.get("id"), entry.get("before")
        if entry.get("op") == "attach" and store == "mcpAttachment":
            # MCP servers are pre-existing local configuration; restoring only
            # detaches the agent attachment and never mutates server state.
            inverse["detach"].append({"store": store, "id": resource_id})
            continue
        if entry.get("op") == "update":
            inverse["restore"].append({"store": store, "id": resource_id})
            for field in entry.get("secretFields") or []:
                restore_attention.append({
                    "kind": "binding-reentry", "id": f"{resource_id}:{field}",
                    "message": f"{field} for {resource_id} requires secret re-entry on restore",
                })
            continue
        if store in ("wiki", "knowledgeBase"):
            inverse["delete"].append({"store": store, "id": resource_id})
            continue
        current_resource = (template_reconcile.tool_store.get_tool(resource_id)
                            if store == "customTool"
                            else template_reconcile.connector_store.get_connector(resource_id))
        prov = (current_resource or {}).get("provenance") or {}
        users = (template_reconcile.agents_using_tool(resource_id)
                 if store == "customTool"
                 else template_reconcile.agents_using_connector(resource_id))
        other_users = [agent for agent in users if agent["id"] != agent_id]
        if (prov.get("sourceId") == ref.get("sourceId")
                and prov.get("templateId") == ref.get("templateId")
                and prov.get("resourceKey") == entry.get("resourceKey")
                and not other_users):
            inverse["delete"].append({"store": store, "id": resource_id})
        else:
            inverse["detach"].append({"store": store, "id": resource_id})
    return {
        "hasSnapshot": True,
        "managed": managed_diff,
        "pins": {
            "agentRevision": agent["updatedAt"],
            "dutyStatus": agent["dutyStatus"],
        },
        "dutyActive": agent["dutyStatus"] == "active",
        "journalInverse": inverse,
        "needsAttention": restore_attention,
        "note": "The guardrail floor still applies on restore (guardrails only ever tighten).",
    }


def restore(agent_id: str, pins: dict | None, confirm_duty: bool = False) -> dict:
    agent = agent_store.get_agent(agent_id)
    if not agent:
        raise ApplyError("source_not_available", "no such agent", 404)
    ref = agent.get("templateRef") or {}
    snapshot = ref.get("snapshot")
    if not snapshot:
        raise ApplyError("no_snapshot", "no snapshot to restore", 409)

    pins = pins or {}
    if (pins.get("agentRevision") != agent["updatedAt"]
            or pins.get("dutyStatus") != agent["dutyStatus"]):
        raise ApplyError("re_preview_required", "the agent changed — re-preview required", 409)
    if agent["dutyStatus"] == "active" and not confirm_duty:
        raise ApplyError(
            "confirm_duty_required",
            "this agent is on duty now — confirm to proceed",
            409,
        )

    target = snapshot.get("managed") or {}
    restore_attention = []
    # Replay the resource journal before the agent CAS. The row remains the
    # authoritative commit point; if it conflicts, resources are left intact
    # and the caller must re-preview/retry.
    try:
        for entry in reversed(snapshot.get("journal") or []):
            store, resource_id, before = entry.get("store"), entry.get("id"), entry.get("before")
            if entry.get("op") == "attach" and store == "mcpAttachment":
                # The agent-row CAS below restores the prior attachment map;
                # MCP server configuration must remain untouched.
                continue
            if entry.get("op") == "update":
                if store == "customTool" and before:
                    restore_data = dict(before)
                    current_tool = template_reconcile.tool_store.get_tool(resource_id) or {}
                    headers = dict(restore_data.get("headers") or {})
                    current_headers = current_tool.get("headers") or {}
                    for field in entry.get("secretFields") or []:
                        leaf = field.split(".", 1)[1] if field.startswith("headers.") else ""
                        if leaf and leaf in current_headers:
                            headers[leaf] = current_headers[leaf]
                        restore_attention.append({
                            "kind": "binding-reentry", "id": f"{resource_id}:{field}",
                            "message": f"{field} for {resource_id} requires secret re-entry on restore",
                        })
                    restore_data["headers"] = headers
                    template_reconcile.tool_store.update_tool(resource_id, restore_data)
                elif store == "connector" and before:
                    for field in entry.get("secretFields") or []:
                        restore_attention.append({
                            "kind": "binding-reentry", "id": f"{resource_id}:{field}",
                            "message": f"{field} for {resource_id} requires secret re-entry on restore",
                        })
                    template_reconcile.connector_store.upsert(resource_id, before)
                continue
            if store == "customTool":
                resource = template_reconcile.tool_store.get_tool(resource_id)
                users = [a for a in template_reconcile.agents_using_tool(resource_id)
                         if a["id"] != agent_id]
                provenance = (resource or {}).get("provenance") or {}
                if (resource and provenance.get("sourceId") == ref.get("sourceId")
                        and provenance.get("templateId") == ref.get("templateId")
                        and provenance.get("resourceKey") == entry.get("resourceKey")
                        and not users):
                    template_reconcile.tool_store.delete_tool(resource_id)
            elif store == "connector":
                resource = template_reconcile.connector_store.get_connector(resource_id)
                users = [a for a in template_reconcile.agents_using_connector(resource_id)
                         if a["id"] != agent_id]
                provenance = (resource or {}).get("provenance") or {}
                if (resource and provenance.get("sourceId") == ref.get("sourceId")
                        and provenance.get("templateId") == ref.get("templateId")
                        and provenance.get("resourceKey") == entry.get("resourceKey")
                        and not users):
                    template_reconcile.connector_store.delete(resource_id)
            elif store == "wiki":
                resource = template_reconcile.wiki_store.get_wiki(resource_id)
                users = [a for a in template_reconcile.agents_using_wiki(resource_id)
                         if a["id"] != agent_id]
                if resource and not users:
                    template_reconcile.wiki_store.delete_wiki(resource_id)
            elif store == "knowledgeBase":
                resource = template_reconcile.kb_store.get_kb(resource_id)
                users = [a for a in template_reconcile.agents_using_knowledge_base(resource_id)
                         if a["id"] != agent_id]
                if resource and not users:
                    template_reconcile.kb_store.delete_kb(resource_id)
    except Exception as error:
        raise ApplyError("restore_failed", f"template resource restore failed: {error}", 409) from error
    new_ref = dict(snapshot.get("ref") or {})
    new_ref["snapshot"] = None
    updated = agent_store.apply_managed_update(
        agent_id, target, new_ref, expected_updated_at=pins["agentRevision"]
    )
    if updated is None:
        raise ApplyError("re_preview_required", "the agent changed — re-preview required", 409)
    updated["needsAttention"] = restore_attention
    return updated
