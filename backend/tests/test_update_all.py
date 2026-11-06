import pytest

import config
from storage import agent_store, apply_template, template_store


def _manifest(**overrides):
    value = {
        "sourceId": "fleet-source",
        "templateId": "fleet-template",
        "version": "1.0.0",
        "name": "Fleet {{warehouse}}",
        "description": "Fleet agent for {{warehouse}}",
        "persona": "Operate {{warehouse}}",
        "role": "Operator",
        "temperature": 0.2,
        "topP": 0.9,
        "topK": 20,
        "tools": [],
        "skills": [],
        "guardrails": {"shell": "off", "dailyCap": 10},
        "variables": [
            {"key": "warehouse", "type": "text", "default": "ID", "required": True},
        ],
        "starterTasks": [],
    }
    value.update(overrides)
    return value


@pytest.fixture()
def installed_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    imported = template_store.import_template(_manifest())
    return imported["template"]


def test_update_all_updates_all_agents_in_lineage(installed_lineage):
    old_template = installed_lineage
    first = template_store.deploy_template(old_template["id"], {"warehouse": "ID"})
    second = template_store.deploy_template(old_template["id"], {"warehouse": "SG"})
    new_template = template_store.import_template(_manifest(version="2.0.0"))["template"]

    result = apply_template.update_all(new_template["id"])

    assert result["templateId"] == new_template["id"]
    assert result["toVersion"] == "2.0.0"
    assert result["stopped"] is False
    assert [item["status"] for item in result["results"]] == ["updated", "updated"]
    assert {
        agent_store.get_agent(first["id"])["templateRef"]["version"],
        agent_store.get_agent(second["id"])["templateRef"]["version"],
    } == {"2.0.0"}


def test_update_all_skips_up_to_date_and_omits_other_lineages(installed_lineage):
    new_template = template_store.import_template(
        _manifest(version="2.0.0")
    )["template"]
    current = template_store.deploy_template(new_template["id"], {"warehouse": "ID"})
    other_manifest = _manifest(
        sourceId="other-source", templateId="other-template", version="2.0.0"
    )
    other_template = template_store.import_template(other_manifest)["template"]
    other = template_store.deploy_template(other_template["id"], {"warehouse": "MY"})

    result = apply_template.update_all(new_template["id"])

    assert result["stopped"] is False
    assert result["results"] == [{
        "agentId": current["id"],
        "name": current["name"],
        "status": "skipped",
        "reason": "up_to_date",
    }]
    assert agent_store.get_agent(other["id"])["templateRef"]["version"] == "2.0.0"


def test_update_all_stops_after_first_failure(installed_lineage):
    old_template = installed_lineage
    first = template_store.deploy_template(old_template["id"], {"warehouse": "ID"})
    later = template_store.deploy_template(old_template["id"], {"warehouse": "SG"})
    new_template = template_store.import_template(_manifest(version="2.0.0"))["template"]
    # list_agents is newest-first; make the first returned lineage agent active.
    ordered = [agent for agent in agent_store.list_agents()
               if agent["id"] in {first["id"], later["id"]}]
    active = ordered[0]
    untouched = ordered[1]
    agent_store.update_agent(active["id"], {"dutyStatus": "active"})

    result = apply_template.update_all(new_template["id"])

    assert result["stopped"] is True
    assert result["results"] == [{
        "agentId": active["id"],
        "name": active["name"],
        "status": "failed",
        "code": "confirm_duty_required",
        "message": "this agent is on duty now — confirm to proceed",
    }]
    assert agent_store.get_agent(untouched["id"])["templateRef"]["version"] == "1.0.0"


def test_update_all_rejects_unknown_and_builtin_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)

    with pytest.raises(apply_template.ApplyError) as unknown:
        apply_template.update_all("missing-template")
    assert unknown.value.code == "source_not_available"
    assert unknown.value.status == 404

    with pytest.raises(apply_template.ApplyError) as builtin:
        apply_template.update_all("intern")
    assert builtin.value.code == "read_only"
    assert builtin.value.status == 400
