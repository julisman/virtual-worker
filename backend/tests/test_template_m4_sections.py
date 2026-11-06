"""PRD-29 M4: portable wiki, knowledge-base, and MCP sections."""

import asyncio
import json

import pytest

import config
from storage import agent_store, apply_template, kb_store, mcp_store, template_manifest
from storage import template_store, wiki_store
from routes.templates import template_rollout


def _manifest(**overrides):
    value = {
        "schemaVersion": 2,
        "sourceId": "m4-source",
        "templateId": "m4-template",
        "version": "1.0.0",
        "name": "M4 template",
        "persona": "portable persona",
        "variables": [{"key": "repo", "type": "folder", "required": True}],
        "wikis": [{"name": "Docs", "repoUrl": "https://example.invalid/docs",
                    "pathVariable": "repo"}],
        "knowledgeBases": [{"name": "Handbook", "description": "desc",
                             "embeddingModel": "local"}],
        "mcpServers": [{"name": "Local MCP", "transport": "stdio",
                        "mcpToolAllow": ["search"]}],
    }
    value.update(overrides)
    return value


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def test_manifest_m4_sections_and_mcp_refusal():
    normalized = template_manifest.validate_and_normalize(_manifest())
    assert normalized["requiresFeatures"] == ["knowledgeBases", "mcpServers", "wikis"]
    assert normalized["wikis"][0]["wikiRoot"] == "."
    for forbidden in ("env", "command", "url", "args", "token"):
        with pytest.raises(template_manifest.ManifestError, match="configured manually"):
            template_manifest.validate_and_normalize(
                _manifest(mcpServers=[{"name": "Local MCP", forbidden: "secret"}]))
    with pytest.raises(template_manifest.ManifestError, match="Unknown template section"):
        template_manifest.validate_and_normalize(_manifest(unknownSection=[]))


def test_deploy_m4_resources_and_attention(home):
    repo = home / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# docs", encoding="utf-8")
    mcp = mcp_store.create_server({"name": "Local MCP", "transport": "stdio",
                                   "command": "configured-locally"})
    imported = template_store.import_template(_manifest())
    agent = template_store.deploy_template(imported["id"], {"repo": str(repo)})
    assert agent["wikis"]
    assert agent["knowledgeBases"]
    assert agent["mcpServers"] == [mcp["id"]]
    assert agent["mcpToolAllow"] == {mcp["id"]: ["search"]}
    assert {item["kind"] for item in agent["needsAttention"]} == {"kb-empty"}

    missing = template_store.import_template(
        _manifest(templateId="m4-missing", mcpServers=[{"name": "Absent"}]))
    second = template_store.deploy_template(missing["id"], {"repo": str(repo)})
    assert "mcp-server-unmatched" in {item["kind"] for item in second["needsAttention"]}
    assert not second["mcpServers"]


def test_unowned_collision_requires_adoption_on_update(home):
    wiki = wiki_store.create_wiki("Docs")
    kb = kb_store.create_kb("Handbook")
    imported = template_store.import_template(_manifest(wikis=[], knowledgeBases=[]))
    agent = template_store.deploy_template(imported["id"], {"repo": str(home)})
    template_store.import_template(_manifest(version="1.0.1"))
    preview = apply_template.dry_run_apply(agent["id"])
    assert {row["type"] for row in preview["resources"] if row["status"] == "adopt"} >= {"wiki", "knowledgeBase"}


def test_update_preserves_unowned_wiki_kb_and_mcp_attachments(home):
    imported = template_store.import_template(
        _manifest(wikis=[], knowledgeBases=[], mcpServers=[]))
    agent = template_store.deploy_template(imported["id"], {"repo": str(home)})
    wiki = wiki_store.create_wiki("Hand attached")
    kb = kb_store.create_kb("Hand attached KB")
    mcp = mcp_store.create_server({"name": "Hand attached MCP"})
    agent_store.update_agent(agent["id"], {
        "wikis": [wiki["id"]], "knowledgeBases": [kb["id"]],
        "mcpServers": [mcp["id"]], "mcpToolAllow": {mcp["id"]: ["keep"]},
    })
    template_store.import_template(
        _manifest(version="1.1.0", wikis=[], knowledgeBases=[], mcpServers=[]))
    current = agent_store.get_agent(agent["id"])
    updated = apply_template.apply(agent["id"], {
        "digest": template_store.get_template(imported["id"])["digest"],
        "agentRevision": current["updatedAt"], "dutyStatus": current["dutyStatus"],
    })
    assert updated["wikis"] == [wiki["id"]]
    assert updated["knowledgeBases"] == [kb["id"]]
    assert updated["mcpServers"] == [mcp["id"]]
    assert updated["mcpToolAllow"] == {mcp["id"]: ["keep"]}


def test_restore_reverts_m4_resources_and_detaches_mcp(home):
    base = template_store.import_template(
        _manifest(wikis=[], knowledgeBases=[], mcpServers=[]))
    agent = template_store.deploy_template(base["id"], {"repo": str(home)})
    mcp = mcp_store.create_server({
        "name": "Local MCP", "transport": "stdio", "command": "configured-locally",
        "args": ["--safe"], "env": {"TOKEN": "machine-secret"},
    })
    template_store.import_template(_manifest(
        version="1.1.0",
        mcpServers=[{"name": "Local MCP", "transport": "stdio",
                     "mcpToolAllow": ["search"]}],
    ))
    preview = apply_template.dry_run_apply(agent["id"])
    updated = apply_template.apply(agent["id"], preview["pins"])
    assert updated["mcpServers"] == [mcp["id"]]
    assert updated["wikis"] and updated["knowledgeBases"]
    # Linked-wiki creation also attaches to the seeded Assistant by store
    # convention; remove that unrelated attachment so restore can delete the
    # template-created resource under the normal no-other-users semantics.
    for other in agent_store.list_agents():
        if other["id"] != updated["id"]:
            agent_store.update_agent(other["id"], {
                "wikis": [wiki_id for wiki_id in other.get("wikis") or []
                          if wiki_id not in updated["wikis"]],
            })

    mcp_before_restore = mcp_store.get_server(mcp["id"])
    restore_preview = apply_template.dry_run_restore(updated["id"])
    journal_inverse = restore_preview["journalInverse"]
    assert any(item["store"] == "mcpAttachment" for item in journal_inverse["detach"])
    assert not any(item["store"] == "mcpAttachment" for item in journal_inverse["restore"])
    restored = apply_template.restore(updated["id"], restore_preview["pins"])

    assert restored["mcpServers"] == []
    assert restored["mcpToolAllow"] == {}
    assert mcp_store.get_server(mcp["id"]) == mcp_before_restore
    for resource_id in updated["wikis"]:
        assert wiki_store.get_wiki(resource_id) is None
    for resource_id in updated["knowledgeBases"]:
        assert kb_store.get_kb(resource_id) is None


def test_export_round_trip_m4_has_no_machine_secrets(home, monkeypatch, tmp_path):
    repo = home / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# docs", encoding="utf-8")
    wiki = wiki_store.create_linked_wiki("Docs", str(repo))
    kb = kb_store.create_kb("Handbook", "desc", "local")
    mcp = mcp_store.create_server({"name": "MCP", "transport": "http",
                                   "url": "https://user:password@host", "env": {"TOKEN": "secret-value"}})
    agent = agent_store.create_agent({"name": "Export", "persona": "p",
                                      "wikis": [wiki["id"]], "knowledgeBases": [kb["id"]],
                                      "mcpServers": [mcp["id"]],
                                      "mcpToolAllow": {mcp["id"]: ["search"]}})
    tpl = template_store.template_from_agent(agent["id"], new_template_id="m4-roundtrip")
    blob = json.dumps(tpl)
    assert tpl["schemaVersion"] == 2
    assert {"wikis", "knowledgeBases", "mcpServers"} <= set(tpl["requiresFeatures"])
    assert "password@host" not in blob
    assert "secret-value" not in blob
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    stored = template_store.import_template(tpl)["template"]
    assert stored["schemaVersion"] == 2


def test_rollout_reports_lineage_status(home):
    imported = template_store.import_template(_manifest())
    agent = template_store.deploy_template(imported["id"], {"repo": str(home)})
    template_store.import_template(_manifest(version="1.1.0"))
    result = asyncio.run(template_rollout(imported["id"]))
    assert result["template"]["version"] == "1.1.0"
    [row] = result["agents"]
    assert row["agentId"] == agent["id"]
    assert row["version"] == "1.0.0"
    assert row["status"] == "behind"
    assert row["onDuty"] is False
