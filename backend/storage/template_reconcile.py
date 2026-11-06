"""Structural reconciliation for schema-v2 template resources.

This module intentionally keeps planning (validation and rendering) separate
from mutation.  Deploy uses :func:`reconcile_template_resources`; the later
template-apply path can use the same plan/apply pair with a journal.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from connectors import PRESETS, preset_base_url
from connectors.store import _SECRET_SLOTS
from connectors import store as connector_store
from storage import agent_store, skill_store, tool_store, wiki_store, kb_store, mcp_store
from storage.template_store import _subst


_TOKEN_RE = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


class ReconciliationError(ValueError):
    """A validation failure with structured reconciliation details."""

    def __init__(self, message: str, *, collisions: list[dict] | None = None):
        super().__init__(message)
        self.collisions = collisions or []


def _provenance(source_id: str, template_id: str, version: str,
                resource_key: str) -> dict[str, str]:
    return {
        "sourceId": source_id,
        "templateId": template_id,
        "version": version,
        "resourceKey": resource_key,
    }


def _owned(value: dict | None, source_id: str, template_id: str,
           resource_key: str) -> bool:
    provenance = (value or {}).get("provenance") or {}
    return (provenance.get("sourceId") == source_id
            and provenance.get("templateId") == template_id
            and provenance.get("resourceKey") == resource_key)


def _adoption_set(adoptions: Iterable[dict] | None) -> set[tuple[str, str]]:
    return {
        (str(item.get("type") or ""), str(item.get("resourceKey") or ""))
        for item in (adoptions or [])
        if isinstance(item, dict)
    }


def _render_value(value: Any, ctx: dict) -> Any:
    if isinstance(value, str):
        return _subst(value, ctx)
    if isinstance(value, dict):
        return {key: _render_value(nested, ctx) for key, nested in value.items()}
    if isinstance(value, list):
        return [_render_value(nested, ctx) for nested in value]
    return value


def _render_headers(headers: dict, ctx: dict, secrets: dict[str, str],
                    declared_secrets: set[str], missing: set[str]) -> tuple[dict, dict[str, str]]:
    rendered: dict[str, str] = {}
    bindings: dict[str, str] = {}
    for header, raw in headers.items():
        if not isinstance(raw, str):
            rendered[header] = raw
            continue
        def replace(match: re.Match) -> str:
            key = match.group(1).strip()
            if key in secrets:
                bindings.setdefault(key, header)
                return str(secrets[key])
            if key in ctx:
                return str(ctx[key])
            # A declared secret with no supplied value is a validation error;
            # unknown non-secret placeholders retain the existing M1 behavior.
            if key in declared_secrets:
                missing.add(key)
            return match.group(0)
        rendered[header] = _TOKEN_RE.sub(replace, raw)
    return rendered, bindings


def _tool_name(name: str, template_id: str) -> str:
    """Produce a valid custom-tool identifier for a collision copy."""
    suffix = re.sub(r"[^a-z0-9_]+", "_", template_id.lower()).strip("_") or "template"
    base = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "tool"
    if not base[0].isalpha():
        base = "tool_" + base
    return f"{base}_{suffix}"


def _connector_name(name: str, template_id: str, existing: set[str]) -> str:
    candidate = f"{name} ({template_id})"
    if candidate not in existing:
        return candidate
    index = 2
    while f"{candidate} {index}" in existing:
        index += 1
    return f"{candidate} {index}"


def _find_tool(template: dict, item: dict) -> tuple[dict | None, bool]:
    source_id, template_id = template["sourceId"], template["templateId"]
    for tool in tool_store.list_tools():
        if _owned(tool, source_id, template_id, item["key"]):
            return tool, False
    for tool in tool_store.list_tools():
        if tool["name"] == item["name"]:
            return tool, True
    return None, False


def _find_connector(template: dict, item: dict) -> tuple[dict | None, bool]:
    source_id, template_id = template["sourceId"], template["templateId"]
    for connector in connector_store.list_connectors():
        if _owned(connector, source_id, template_id, item["name"]):
            return connector, False
    for connector in connector_store.list_connectors():
        if connector.get("name") == item["name"]:
            return connector, True
    return None, False


def _find_named_resource(template: dict, item: dict, kind: str,
                         template_ref: dict | None = None) -> tuple[dict | None, bool]:
    store = {"wiki": wiki_store, "knowledgeBase": kb_store}[kind]
    getter = store.get_wiki if kind == "wiki" else store.get_kb
    records = store.list_wikis() if kind == "wiki" else store.list_kbs()
    resource_key = item["name"]
    for entry in (template_ref or {}).get("ledger", []) or []:
        if entry.get("type") == kind and entry.get("resourceKey") == resource_key:
            owned = getter(entry.get("id"))
            if owned:
                return owned, False
    for record in records:
        if record.get("name") == resource_key:
            return record, True
    return None, False


def _find_mcp_server(name: str) -> dict | None:
    return next((server for server in mcp_store.list_servers()
                 if server.get("name") == name), None)


def _disambiguated_name(name: str, template_id: str, existing: set[str]) -> str:
    candidate = f"{name} ({template_id})"
    if candidate not in existing:
        return candidate
    index = 2
    while f"{candidate} {index}" in existing:
        index += 1
    return f"{candidate} {index}"


def _prior_secret_target(binding: dict | None) -> tuple[dict | None, str | None]:
    """Resolve a prior binding to its raw store record and leaf value."""
    if not binding:
        return None, None
    if binding.get("store") == "customTool":
        target = tool_store.get_tool(binding.get("resourceId"))
        field = str(binding.get("field") or "")
        if target and field.startswith("headers."):
            leaf = field.split(".", 1)[1]
            return (target, target.get("headers", {}).get(leaf)) if leaf in (target.get("headers") or {}) else (None, None)
    elif binding.get("store") == "connector":
        target = connector_store.get_connector(binding.get("resourceId"))
        field = str(binding.get("field") or "")
        if target and field in target:
            return target, target.get(field)
    return None, None


def _collision_details(plan_items: Iterable[dict], kind: str) -> list[dict]:
    result = []
    for item in plan_items:
        if item.get("collision") and item.get("existing"):
            resource_id = item["existing"].get("id") if kind == "customTool" else item["existing"].get("name")
            users = (affected_agents(resource_id) if kind == "customTool"
                     else agents_using_connector(resource_id))
            result.append({
                "type": kind,
                "name": item.get("name"),
                "resourceKey": item.get("key") or item.get("name"),
                "affectedAgents": [agent.get("name") for agent in users],
            })
    return result


def validate_reconciliation(template: dict, ctx: dict,
                            secrets: dict[str, str] | None = None,
                            adoptions: Iterable[dict] | None = None,
                            *, template_ref: dict | None = None,
                            update_mode: bool = False,
                            strict_adoptions: bool = False,
                            raise_on_missing: bool = True) -> dict:
    """Validate and render a normalized manifest without changing stores.

    ``raise_on_missing=False`` turns an unresolved-secret hard error into plan
    data (``missingSecrets``) — the deploy PREVIEW path, which must render the
    form even before the operator has typed anything.
    """
    secrets = secrets or {}
    adoption_set = _adoption_set(adoptions)
    variables = {item.get("key") for item in template.get("variables", [])
                 if item.get("type") == "secret"}
    unknown_secrets = sorted(set(secrets) - variables)
    # Supplied secrets not declared by the manifest are harmless input, but are
    # never consumed or persisted in a template reference.
    missing: set[str] = set()
    reentry: set[str] = set()
    retained: set[str] = set()
    prior_bindings = (template_ref or {}).get("bindings") or {}
    tools: list[dict] = []
    for item in template.get("customTools", []) or []:
        headers: dict = {}
        header_bindings: dict[str, str] = {}
        retained_headers: dict[str, str] = {}
        reentry_headers: set[str] = set()
        secret_headers: set[str] = set()
        for header, raw in (item.get("headers") or {}).items():
            if not isinstance(raw, str):
                headers[header] = raw
                continue
            def replace(match: re.Match) -> str:
                key = match.group(1).strip()
                if key in variables:
                    secret_headers.add(header)
                if key in secrets and secrets.get(key) not in (None, ""):
                    header_bindings[key] = header
                    return str(secrets[key])
                if key in ctx and key not in variables:
                    return str(ctx[key])
                if key in variables:
                    target = value = None
                    prior_field = ""
                    if update_mode:
                        prior = prior_bindings.get(key)
                        target, value = _prior_secret_target(prior)
                        prior_field = str((prior or {}).get("field") or "")
                        if not (target and prior_field == f"headers.{header}" and value):
                            target = value = None
                            prior_field = ""
                    if not target:
                        # Retention from the record this run will actually reuse
                        # — the owned match, or an unowned name-match being
                        # adopted. Applies on DEPLOY too: a machine that already
                        # has the tool never re-enters its credential. (On
                        # update, older refs store one binding per variable, so
                        # a variable used by both a tool and connector can point
                        # at the latter; the owned resource is the unambiguous
                        # fallback.)
                        owned_tool, tool_collision = _find_tool(template, item)
                        tool_reusable = owned_tool is not None and (
                            _owned(owned_tool, template["sourceId"],
                                   template["templateId"], item["key"])
                            or (tool_collision and ("customTool", item["key"]) in adoption_set))
                        if tool_reusable:
                            candidate = (owned_tool.get("headers") or {}).get(header)
                            if candidate:
                                target, value = owned_tool, candidate
                                prior_field = f"headers.{header}"
                    if target and prior_field == f"headers.{header}" and value:
                        stored = str(value)
                        prefix, suffix = raw[:match.start()], raw[match.end():]
                        if ((prefix and not stored.startswith(prefix))
                                or (suffix and not stored.endswith(suffix))):
                            # The literal framing changed, so retaining this
                            # rendered value would silently corrupt the header.
                            # Require an explicit (re-)entry instead.
                            if update_mode:
                                reentry_headers.add(header)
                                reentry.add(key)
                                return ""
                            missing.add(key)
                            return match.group(0)
                        # Keep the existing stored header verbatim. Never
                        # splice a new template prefix/suffix around it.
                        retained_headers[header] = stored
                        retained.add(key)
                        return stored
                    if update_mode:
                        reentry_headers.add(header)
                        reentry.add(key)
                        return ""
                    missing.add(key)
                return match.group(0)
            rendered_header = _TOKEN_RE.sub(replace, raw)
            if header in retained_headers:
                rendered_header = retained_headers[header]
            if header not in reentry_headers:
                headers[header] = rendered_header
        rendered = _render_value({k: item.get(k) for k in (
            "key", "name", "description", "transport", "method", "url",
            "parameters", "fixedParams", "timeout", "allowPrivate")}, ctx)
        rendered["headers"] = headers
        rendered["headerBindings"] = header_bindings
        rendered["retainedHeaders"] = retained_headers
        rendered["reentryHeaders"] = sorted(reentry_headers)
        rendered["secretHeaders"] = sorted(secret_headers)
        local, collision = _find_tool(template, item)
        rendered["existing"] = local
        rendered["collision"] = collision and not _owned(local, template["sourceId"], template["templateId"], item["key"])
        tools.append(rendered)

    connectors: list[dict] = []
    for item in template.get("connectors", []) or []:
        if item.get("preset") not in PRESETS:
            raise ValueError(f"unknown connector preset: {item.get('preset')}")
        binding_map = item.get("secretBindings") or {}
        local, collision = _find_connector(template, item)
        for slot, variable_key in binding_map.items():
            if slot not in _SECRET_SLOTS:
                raise ValueError(f"connector {item.get('name')!r} uses unknown secret slot {slot!r}")
            if variable_key not in variables:
                raise ValueError(f"connector {item.get('name')!r} binds unknown secret variable {variable_key!r}")
            if variable_key not in secrets or secrets.get(variable_key) in (None, ""):
                prior = prior_bindings.get(variable_key) if update_mode else None
                target, value = _prior_secret_target(prior) if prior else (None, None)
                prior_connector = target and (prior or {}).get("store") == "connector" and value
                if prior_connector:
                    retained.add(variable_key)
                else:
                    # Retention keys off the record this run will actually
                    # reuse — the owned match, or an unowned name-match being
                    # adopted. Its stored secret survives an upsert that omits
                    # the slot, so a credential already on the box is never
                    # re-asked — on DEPLOY as well as update. ``local`` is a
                    # *masked* record: presence is witnessed by the
                    # ``has<Slot>`` flag, never a value.
                    unowned = collision and not _owned(
                        local, template["sourceId"], template["templateId"],
                        item["name"])
                    adopted_now = ("connector", item["name"]) in adoption_set
                    reusable = local is not None and (not unowned or adopted_now)
                    has_flag = "has" + slot[0].upper() + slot[1:]
                    if reusable and (local.get(has_flag) or local.get(slot)):
                        retained.add(variable_key)
                    elif update_mode:
                        reentry.add(variable_key)
                    else:
                        missing.add(variable_key)
        connectors.append({
            **copy.deepcopy(item),
            "overrides": _render_value(item.get("overrides") or {}, ctx),
            "existing": local,
            "collision": collision and not _owned(local, template["sourceId"], template["templateId"], item["name"]),
        })
    wikis: list[dict] = []
    for item in template.get("wikis", []) or []:
        local, collision = _find_named_resource(template, item, "wiki", template_ref)
        wikis.append({**copy.deepcopy(item), "existing": local,
                      "collision": bool(collision)})
    knowledge_bases: list[dict] = []
    for item in template.get("knowledgeBases", []) or []:
        local, collision = _find_named_resource(template, item, "knowledgeBase", template_ref)
        knowledge_bases.append({**copy.deepcopy(item), "existing": local,
                                "collision": bool(collision)})
    mcp_servers: list[dict] = []
    for item in template.get("mcpServers", []) or []:
        mcp_servers.append({**copy.deepcopy(item), "existing": _find_mcp_server(item["name"]),
                            "collision": False})
    if missing and raise_on_missing:
        raise ValueError("unresolved secret variable(s): " + ", ".join(sorted(missing)))
    plan = {
        "template": template,
        "ctx": dict(ctx),
        "secrets": dict(secrets),
        "adoptions": adoption_set,
        "tools": tools,
        "connectors": connectors,
        "wikis": wikis,
        "knowledgeBases": knowledge_bases,
        "mcpServers": mcp_servers,
        "unknownSecrets": unknown_secrets,
        "updateMode": update_mode,
        "missingSecrets": sorted(missing),
        "reentrySecrets": sorted(reentry),
        "retainedSecrets": sorted(retained),
    }
    collisions = (_collision_details(tools, "customTool")
                  + _collision_details(connectors, "connector"))
    for items, kind in ((wikis, "wiki"), (knowledge_bases, "knowledgeBase")):
        for item in items:
            if item.get("collision") and item.get("existing"):
                resource = item["existing"]
                users = (agents_using_wiki(resource.get("id")) if kind == "wiki"
                         else agents_using_knowledge_base(resource.get("id")))
                collisions.append({"type": kind, "name": item["name"],
                                   "resourceKey": item["name"],
                                   "affectedAgents": [a.get("name") for a in users]})
    plan["collisions"] = collisions
    if strict_adoptions and collisions:
        unresolved = [item for item in collisions
                      if (item["type"], item["resourceKey"]) not in adoption_set]
        if unresolved:
            raise ReconciliationError("adoption confirmation is required", collisions=unresolved)
    return plan


def agents_using_tool(tool_id: str) -> list[dict]:
    """Return every agent whose custom-tool attachment contains ``tool_id``."""
    return [agent for agent in agent_store.list_agents()
            if tool_id in (agent.get("customTools") or [])
            or tool_id in (agent.get("tools") or [])]


def affected_agents(tool_id: str) -> list[dict]:
    """Alias used by dry-run callers for the tool impact list."""
    return agents_using_tool(tool_id)


def agents_using_connector(connector_name: str) -> list[dict]:
    return [agent for agent in agent_store.list_agents()
            if connector_name in (agent.get("connectors") or [])]


def agents_using_wiki(wiki_id: str) -> list[dict]:
    return [agent for agent in agent_store.list_agents()
            if wiki_id in (agent.get("wikis") or [])]


def agents_using_knowledge_base(kb_id: str) -> list[dict]:
    return [agent for agent in agent_store.list_agents()
            if kb_id in (agent.get("knowledgeBases") or [])]


def _reconcile_skills(template: dict) -> tuple[list[str], list[dict], list[dict], dict]:
    attached: list[str] = []
    ledger: list[dict] = []
    attention: list[dict] = []
    installed: list[str] = []
    builtin = {item.get("slug") for item in skill_store.list_builtin()}
    for slug in template.get("skills", []) or []:
        if skill_store.get_skill(slug):
            pass
        elif slug in builtin:
            result = skill_store.install_builtin(slug)
            installed.extend(result.get("installed") or [])
            for tool in result.get("missingTools") or []:
                attention.append({"kind": "cli-tool-missing", "id": slug,
                                  "message": f"skill {slug} requires missing CLI tool {tool}"})
        else:
            # M3 registry seam: a synced registry can satisfy an otherwise
            # unresolved skill without changing the deploy ordering contract.
            resolved_from_registry = False
            if template.get("source") == "registry" and template.get("registry"):
                from storage import registry_store
                skill_dir = registry_store.find_registry_skill_dir(
                    template["registry"], slug)
                if skill_dir:
                    try:
                        result = skill_store.install_from_dir(skill_dir, slug)
                    except ValueError:
                        pass
                    else:
                        resolved_from_registry = True
                        installed.extend(result.get("installed") or [])
                        for tool in result.get("missingTools") or []:
                            attention.append({
                                "kind": "cli-tool-missing", "id": slug,
                                "message": f"skill {slug} requires missing CLI tool {tool}",
                            })
            if not resolved_from_registry:
                attention.append({"kind": "skill-unresolved", "id": slug,
                                  "message": f"skill {slug} is not installed or builtin"})
        if slug not in attached:
            attached.append(slug)
        ledger.append({"type": "skill", "id": slug, "resourceKey": slug,
                       "adopted": False,
                       # Preserve the original skill-ledger contract: the
                       # attachment identifies resourceKey, while provenance
                       # remains lineage-only (resourceKey is not duplicated).
                       "provenance": {"sourceId": template["sourceId"],
                                      "templateId": template["templateId"],
                                      "version": template["version"]}})
    return attached, ledger, attention, {"installed": len(installed), "attached": len(attached)}


def _journal_tool_before(before: dict | None, item: dict,
                         prior_bindings: dict | None = None) -> tuple[dict | None, list[str]]:
    """Copy a tool before-image without persisting bound header values.

    The old resource is authoritative for the before-image: include any header
    leaf bound to this tool in the prior ref, even when the new manifest drops,
    renames, or de-secrets that header.
    """
    if not before:
        return None, []
    safe = copy.deepcopy(before)
    headers = safe.get("headers") or {}
    secret_headers = set(item.get("secretHeaders", []))
    for binding in (prior_bindings or {}).values():
        if (isinstance(binding, dict)
                and binding.get("store") == "customTool"
                and binding.get("resourceId") == before.get("id")):
            field = str(binding.get("field") or "")
            if field.startswith("headers."):
                secret_headers.add(field.split(".", 1)[1])
    fields = [f"headers.{header}" for header in sorted(secret_headers)]
    for header in secret_headers:
        headers.pop(header, None)
    safe["headers"] = headers
    return safe, fields


def _journal_connector_before(before: dict | None,
                              item: dict) -> tuple[dict | None, list[str]]:
    """Copy a connector before-image without any write-only secret slots."""
    if not before:
        return None, []
    fields = list((item.get("secretBindings") or {}).keys())
    safe = {key: value for key, value in before.items() if key not in _SECRET_SLOTS}
    return safe, fields


def _scrub_repo_url(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        parsed = urlsplit(value)
        if parsed.username is None and parsed.password is None:
            return value
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return value


def _journal_wiki_before(before: dict | None) -> tuple[dict | None, list[str]]:
    if not before:
        return None, []
    safe = copy.deepcopy(before)
    for key in ("repoUrl", "rootPath"):
        if key in safe:
            safe[key] = _scrub_repo_url(safe[key])
    return safe, []


def _journal_kb_before(before: dict | None) -> tuple[dict | None, list[str]]:
    return copy.deepcopy(before) if before else None, []


def apply_reconciliation(plan: dict, *, template_ref: dict | None = None,
                         journal: list[dict] | None = None) -> dict:
    """Apply a validated plan and return attachments, ledger, summary, warnings."""
    template = plan["template"]
    adoption_set = plan["adoptions"]
    source_id, template_id, version = template["sourceId"], template["templateId"], template["version"]
    attached_tools: list[str] = []
    attached_connectors: list[str] = []
    ledger: list[dict] = []
    bindings: dict[str, dict] = {}
    attention: list[dict] = []
    summary = {"customTools": {"created": 0, "adopted": 0},
               "connectors": {"created": 0, "attached": 0},
               "skills": {"installed": 0, "attached": 0}}
    if template.get("wikis"):
        summary["wikis"] = {"created": 0, "attached": 0, "adopted": 0}
    if template.get("knowledgeBases"):
        summary["knowledgeBases"] = {"created": 0, "attached": 0, "adopted": 0}
    if template.get("mcpServers"):
        summary["mcpServers"] = {"attached": 0}
    for secret_key in plan.get("reentrySecrets", []):
        attention.append({
            "kind": "binding-reentry", "id": str(secret_key),
            "message": f"binding for {secret_key} requires secret re-entry",
        })

    # A prior apply/deploy may have recorded a binding whose target was deleted
    # or whose secret slot was revoked.  Keep this visible to the caller; the
    # current resource pass can still repair it when a replacement is created.
    for secret_key, binding in (template_ref or {}).get("bindings", {}).items():
        target = None
        if binding.get("store") == "customTool":
            target = tool_store.get_tool(binding.get("resourceId"))
            field = str(binding.get("field") or "")
            if target and field.startswith("headers."):
                target = target if field.split(".", 1)[1] in (target.get("headers") or {}) else None
        elif binding.get("store") == "connector":
            target = connector_store.get_connector(binding.get("resourceId"))
            if target and binding.get("field") not in target:
                target = None
        if not target:
            attention.append({"kind": "binding-reentry", "id": str(secret_key),
                              "message": f"binding target for {secret_key} was deleted or revoked"})

    # Fallible-first ordering: skills, tools, connectors, then assembly.
    attached_skills, skill_ledger, skill_attention, skill_summary = _reconcile_skills(template)
    ledger.extend(skill_ledger)
    attention.extend(skill_attention)
    summary["skills"] = skill_summary

    for item in plan["tools"]:
        definition = {key: item[key] for key in (
            "name", "description", "transport", "method", "url", "headers",
            "parameters", "fixedParams", "timeout", "allowPrivate")}
        # The manifest treats ``name`` as a display name, while the existing
        # custom-tool store requires a callable identifier. Preserve valid
        # identifiers verbatim and make human-readable names safe on creation.
        if not re.fullmatch(r"^[a-z][a-z0-9_]*$", definition["name"] or ""):
            definition["name"] = _tool_name(definition["name"], template_id)
        existing = item.get("existing")
        adopted = False
        if existing and item.get("collision"):
            if ("customTool", item["key"]) in adoption_set:
                adopted = True
            else:
                existing = None
                names = {tool["name"] for tool in tool_store.list_tools()}
                definition["name"] = _tool_name(item["name"], template_id)
                while definition["name"] in names:
                    definition["name"] += "_2"
        elif existing:
            # Preserve a previously disambiguated owned name on redeploy.  The
            # manifest display name may still be occupied by an unowned tool.
            definition["name"] = existing["name"]
        provenance = _provenance(source_id, template_id, version, item["key"])
        definition["provenance"] = provenance
        if plan.get("updateMode"):
            for header in item.get("reentryHeaders", []):
                # Do not write a placeholder or blank secret when the old
                # binding target was deleted/revoked.
                definition["headers"].pop(header, None)
        before = tool_store.get_tool(existing["id"]) if existing else None
        journal_before = before
        if (plan.get("updateMode") and before
                and plan.get("priorVersion") != version
                and (before.get("provenance") or {}).get("version") == version):
            # A previous run may have completed this fallible mutation but
            # lost the final agent CAS. Treat it as a create for the durable
            # journal so restore can remove the orphaned artifact.
            journal_before = None
        safe_before, secret_fields = _journal_tool_before(
            journal_before, item, (template_ref or {}).get("bindings")
        )
        if existing:
            tool = tool_store.update_tool(existing["id"], definition)
            if not tool:
                raise RuntimeError(f"custom tool {existing['id']} disappeared during reconciliation")
        else:
            tool = tool_store.create_tool(definition)
        if journal is not None:
            journal.append({"store": "customTool", "op": "update" if journal_before else "create",
                            "id": tool["id"], "resourceKey": item["key"],
                            "before": safe_before, "secretFields": secret_fields})
        tool_id = tool["id"]
        attached_tools.append(tool_id)
        summary["customTools"]["adopted" if adopted else "created"] += 1
        current_secret_fields = {
            f"headers.{header}" for header in item.get("secretHeaders", [])
        }
        for field in secret_fields:
            if field not in current_secret_fields:
                attention.append({
                    "kind": "binding-reentry", "id": f"{tool_id}:{field}",
                    "message": f"{field} for {tool_id} requires secret re-entry",
                })
        for secret_key, header in item.get("headerBindings", {}).items():
            bindings[secret_key] = {"store": "customTool", "resourceId": tool_id,
                                    "field": f"headers.{header}"}
        ledger.append({"type": "customTool", "id": tool_id, "resourceKey": item["key"],
                       "adopted": adopted, "provenance": provenance})

    existing_connector_names = {item.get("name") for item in connector_store.list_connectors()}
    for item in plan["connectors"]:
        existing = item.get("existing")
        adopted = False
        name = item["name"]
        if existing and item.get("collision"):
            if ("connector", item["name"]) in adoption_set:
                adopted = True
            else:
                name = _connector_name(name, template_id, existing_connector_names)
                existing = None
        provenance = _provenance(source_id, template_id, version, item["name"])
        data = {**(item.get("overrides") or {}), "preset": item["preset"],
                "baseUrl": (item.get("overrides") or {}).get("baseUrl") or preset_base_url(item["preset"]),
                "provenance": provenance}
        target_name = existing["name"] if existing else name
        for slot, secret_key in (item.get("secretBindings") or {}).items():
            if secret_key in plan["secrets"] and plan["secrets"].get(secret_key) not in (None, ""):
                data[slot] = plan["secrets"][secret_key]
            # An owned resource may have a disambiguated stored name.  Bind to
            # that actual record, never back to the manifest display name.
            if secret_key in plan["secrets"] and plan["secrets"].get(secret_key) not in (None, ""):
                bindings[secret_key] = {"store": "connector", "resourceId": target_name, "field": slot}
            elif secret_key in (template_ref or {}).get("bindings", {}):
                bindings[secret_key] = (template_ref or {})["bindings"][secret_key]
        before = connector_store.get_connector(target_name)
        journal_before = before
        if (plan.get("updateMode") and before
                and plan.get("priorVersion") != version
                and (before.get("provenance") or {}).get("version") == version):
            journal_before = None
        safe_before, secret_fields = _journal_connector_before(journal_before, item)
        connector_store.upsert(target_name, data)
        if journal is not None:
            journal.append({"store": "connector", "op": "update" if journal_before else "create",
                            "id": target_name, "resourceKey": item["name"],
                            "before": safe_before, "secretFields": secret_fields})
        record = connector_store.get_connector(target_name) or {}
        if not existing:
            existing_connector_names.add(name)
            summary["connectors"]["created"] += 1
            if record.get("status") == "untested":
                attention.append({"kind": "connector-untested", "id": target_name,
                                  "message": f"connector {target_name} has not been tested"})
        else:
            summary["connectors"]["attached"] += 1
        attached_connectors.append(target_name)
        ledger.append({"type": "connector", "id": target_name, "resourceKey": item["name"],
                       "adopted": adopted, "provenance": provenance})

    existing_wiki_names = {item.get("name") for item in wiki_store.list_wikis()}
    attached_wikis: list[str] = []
    for item in plan.get("wikis", []):
        existing = item.get("existing")
        adopted = False
        name = item["name"]
        if existing and item.get("collision"):
            if ("wiki", item["name"]) in adoption_set:
                adopted = True
            else:
                name = _disambiguated_name(name, template_id, existing_wiki_names)
                existing = None
        provenance = _provenance(source_id, template_id, version, item["name"])
        before = wiki_store.get_wiki(existing["id"]) if existing else None
        safe_before, secret_fields = _journal_wiki_before(before)
        if existing:
            wiki = existing
        else:
            path_variable = item.get("pathVariable")
            repo_path = str(plan.get("ctx", {}).get(path_variable) or "").strip() if path_variable else ""
            if not repo_path or not Path(repo_path).expanduser().is_dir():
                attention.append({"kind": "wiki-path-missing", "id": item["name"],
                                  "message": f"wiki path variable {path_variable or 'pathVariable'} is missing or unresolved"})
                continue
            try:
                # Leave trust_tier at the store default and trust no operations;
                # the template cannot escalate trust beyond the local floor.
                wiki = wiki_store.create_linked_wiki(
                    name, repo_path=repo_path, wiki_root=item.get("wikiRoot") or ".",
                    description=item["name"])
            except Exception as error:
                attention.append({"kind": "wiki-path-missing", "id": item["name"],
                                  "message": f"wiki path could not be linked: {error}"})
                continue
            existing_wiki_names.add(name)
            summary["wikis"]["created"] += 1
        if journal is not None:
            journal.append({"store": "wiki", "op": "update" if before else "create",
                            "id": wiki["id"], "resourceKey": item["name"],
                            "before": safe_before, "secretFields": secret_fields})
        attached_wikis.append(wiki["id"])
        if existing:
            summary["wikis"]["adopted" if adopted else "attached"] += 1
        ledger.append({"type": "wiki", "id": wiki["id"], "resourceKey": item["name"],
                       "adopted": adopted, "provenance": provenance})

    existing_kb_names = {item.get("name") for item in kb_store.list_kbs()}
    attached_kbs: list[str] = []
    for item in plan.get("knowledgeBases", []):
        existing = item.get("existing")
        adopted = False
        name = item["name"]
        if existing and item.get("collision"):
            if ("knowledgeBase", item["name"]) in adoption_set:
                adopted = True
            else:
                name = _disambiguated_name(name, template_id, existing_kb_names)
                existing = None
        provenance = _provenance(source_id, template_id, version, item["name"])
        before = kb_store.get_kb(existing["id"]) if existing else None
        safe_before, secret_fields = _journal_kb_before(before)
        if existing:
            kb = existing
            summary["knowledgeBases"]["adopted" if adopted else "attached"] += 1
        else:
            kb = kb_store.create_kb(name, item.get("description") or "",
                                    embed_model=item.get("embeddingModel") or "")
            existing_kb_names.add(name)
            summary["knowledgeBases"]["created"] += 1
            attention.append({"kind": "kb-empty", "id": item["name"],
                              "message": "created empty — ingest your source folder"})
        if journal is not None:
            journal.append({"store": "knowledgeBase", "op": "update" if before else "create",
                            "id": kb["id"], "resourceKey": item["name"],
                            "before": safe_before, "secretFields": secret_fields})
        attached_kbs.append(kb["id"])
        ledger.append({"type": "knowledgeBase", "id": kb["id"], "resourceKey": item["name"],
                       "adopted": adopted, "provenance": provenance})

    attached_mcp: list[str] = []
    mcp_allow: dict[str, list[str]] = {}
    for item in plan.get("mcpServers", []):
        server = item.get("existing")
        if not server:
            attention.append({"kind": "mcp-server-unmatched", "id": item["name"],
                              "message": "not configured on this machine"})
            continue
        server_id = server["id"]
        provenance = _provenance(source_id, template_id, version, item["name"])
        attached_mcp.append(server_id)
        if item.get("mcpToolAllow") is not None:
            mcp_allow[server_id] = list(item["mcpToolAllow"])
        if journal is not None:
            journal.append({"store": "mcpAttachment", "op": "attach", "id": server_id,
                            "resourceKey": item["name"],
                            "before": copy.deepcopy((plan.get("attachmentBefore") or {}).get("mcpToolAllow", {})),
                            "secretFields": []})
        summary["mcpServers"]["attached"] += 1
        ledger.append({"type": "mcpServer", "id": server_id, "resourceKey": item["name"],
                       "adopted": False, "provenance": provenance})

    if template_ref is not None:
        merged_bindings = dict(template_ref.get("bindings") or {})
        merged_bindings.update(bindings)
        template_ref["bindings"] = merged_bindings
        template_ref["ledger"] = ledger
    return {"customTools": attached_tools, "connectors": attached_connectors,
            "wikis": attached_wikis, "knowledgeBases": attached_kbs,
            "mcpServers": attached_mcp, "mcpToolAllow": mcp_allow,
            "skills": attached_skills, "ledger": ledger, "bindings": bindings,
            "needsAttention": attention, "setupSummary": summary}


def reconcile_template_resources(template: dict, ctx: dict,
                                 secrets: dict[str, str] | None = None,
                                 adoptions: Iterable[dict] | None = None,
                                 *, template_ref: dict | None = None,
                                 update_mode: bool = False,
                                 strict_adoptions: bool = False,
                                 journal: list[dict] | None = None) -> dict:
    """Validate then mutate all manifest resources in contract order."""
    return apply_reconciliation(
        validate_reconciliation(
            template, ctx, secrets, adoptions, template_ref=template_ref,
            update_mode=update_mode, strict_adoptions=strict_adoptions,
        ),
        template_ref=template_ref,
        journal=journal,
    )
