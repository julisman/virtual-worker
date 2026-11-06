import pytest

import config
from storage import agent_store, apply_template, template_store


def _manifest(**overrides):
    value = {
        "sourceId": "apply-source",
        "templateId": "apply-template",
        "version": "1.0.0",
        "name": "Apply {{warehouse}}",
        "description": "Description v1 for {{warehouse}}",
        "persona": "Persona v1 for {{warehouse}}",
        "role": "Role v1",
        "temperature": 0.2,
        "topP": 0.9,
        "topK": 20,
        "tools": ["old_tool"],
        "skills": ["old_skill"],
        "guardrails": {"shell": "off", "dailyCap": 10},
        "variables": [
            {"key": "warehouse", "type": "text", "default": "ID", "required": True},
            {"key": "working_dir", "type": "folder", "default": "", "required": False},
        ],
        "starterTasks": [],
    }
    value.update(overrides)
    return value


@pytest.fixture()
def deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    first = template_store.import_template(_manifest())
    template = first["template"]
    agent = template_store.deploy_template(
        template["id"], {"warehouse": "SG", "working_dir": "/tmp/apply-work"}
    )
    upgraded = template_store.import_template(_manifest(
        version="2.0.0",
        description="Description v2 for {{warehouse}}",
        persona="Persona v2 for {{warehouse}}",
        role="Role v2",
        temperature=0.8,
        topP=0.7,
        topK=40,
        tools=["new_tool"],
        skills=["new_skill"],
        guardrails={"shell": "readonly", "dailyCap": 20},
    ))
    return agent, template, upgraded["template"]


def test_apply_preview_and_update_preserve_unmanaged_fields(deployed):
    agent, _old_template, new_template = deployed
    preview = apply_template.dry_run_apply(agent["id"])

    assert preview["fromVersion"] == "1.0.0"
    assert preview["toVersion"] == new_template["version"]
    assert preview["managed"]["persona"] == {
        "old": "Persona v1 for SG", "new": "Persona v2 for SG"
    }
    assert preview["skills"] == {"added": ["new_skill"], "removed": ["old_skill"], "kept": []}

    updated = apply_template.apply(agent["id"], preview["pins"])
    assert updated["persona"] == "Persona v2 for SG"
    assert updated["tools"] == ["new_tool"]
    assert updated["templateRef"]["version"] == "2.0.0"
    assert updated["workingDir"] == "/tmp/apply-work"
    assert updated["templateRef"]["snapshot"]["managed"]["persona"] == "Persona v1 for SG"
    assert set(agent_store.DEFAULT_GUARDRAILS).issubset(updated["guardrails"])


def test_apply_uses_compare_and_swap_pins(deployed):
    agent, _old_template, _new_template = deployed
    preview = apply_template.dry_run_apply(agent["id"])
    agent_store.update_agent(agent["id"], {"description": "member edit"})

    with pytest.raises(apply_template.ApplyError) as error:
        apply_template.apply(agent["id"], preview["pins"])
    assert error.value.code == "re_preview_required"


def test_preview_reports_local_divergence(deployed):
    agent, _old_template, _new_template = deployed
    agent_store.update_agent(agent["id"], {"temperature": 0.6})

    preview = apply_template.dry_run_apply(agent["id"])
    divergence = next(item for item in preview["localDivergence"] if item["field"] == "temperature")
    assert divergence["current"] == 0.6
    assert divergence["oldTemplate"] == 0.2


def test_apply_requires_confirmation_for_active_agent(deployed):
    agent, _old_template, _new_template = deployed
    agent_store.update_agent(agent["id"], {"dutyStatus": "active"})
    preview = apply_template.dry_run_apply(agent["id"])

    with pytest.raises(apply_template.ApplyError) as error:
        apply_template.apply(agent["id"], preview["pins"])
    assert error.value.code == "confirm_duty_required"

    updated = apply_template.apply(agent["id"], preview["pins"], confirm_duty=True)
    assert updated["templateRef"]["version"] == "2.0.0"


def test_required_variable_migration_requires_and_merges_answer(deployed):
    agent, _old_template, _new_template = deployed
    migrated = template_store.import_template(_manifest(
        version="3.0.0",
        variables=[
            {"key": "warehouse", "type": "text", "default": "ID", "required": True},
            {"key": "working_dir", "type": "folder", "default": "", "required": False},
            {"key": "team", "type": "text", "required": True},
        ],
    ))["template"]
    preview = apply_template.dry_run_apply(agent["id"])
    assert preview["toVersion"] == migrated["version"]
    assert preview["missingVariables"] == [{"key": "team", "reason": "required", "type": "text"}]

    with pytest.raises(apply_template.ApplyError) as error:
        apply_template.apply(agent["id"], preview["pins"])
    assert error.value.code == "variables_required"
    assert error.value.extra["missingVariables"] == preview["missingVariables"]

    updated = apply_template.apply(agent["id"], preview["pins"], {"team": "Inventory"})
    assert updated["templateRef"]["variables"]["team"] == "Inventory"


def test_restore_reverts_snapshot_and_keeps_guardrail_floor(deployed):
    agent, _old_template, _new_template = deployed
    preview = apply_template.dry_run_apply(agent["id"])
    applied = apply_template.apply(agent["id"], preview["pins"])
    restore_preview = apply_template.dry_run_restore(applied["id"])
    assert restore_preview["hasSnapshot"] is True

    restored = apply_template.restore(applied["id"], restore_preview["pins"])
    assert restored["persona"] == "Persona v1 for SG"
    assert restored["templateRef"]["version"] == "1.0.0"
    assert restored["templateRef"]["snapshot"] is None
    assert set(agent_store.DEFAULT_GUARDRAILS).issubset(restored["guardrails"])


def test_source_unavailable_is_reported(deployed):
    agent, old_template, _new_template = deployed
    assert template_store.delete_template(old_template["id"])

    preview = apply_template.dry_run_apply(agent["id"])
    assert preview["sourceAvailable"] is False
    assert preview["fromVersion"] == "1.0.0"
    with pytest.raises(apply_template.ApplyError) as error:
        apply_template.apply(agent["id"], preview.get("pins"))
    assert error.value.code == "source_not_available"
