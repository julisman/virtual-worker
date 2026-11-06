import json

import pytest

import config
from storage import template_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return template_store


def manifest(**overrides):
    value = {
        "name": "Inventory",
        "persona": "You operate {{warehouse}}.",
        "templateId": "inventory",
        "version": "1.0.0",
        "variables": [
            {"key": "warehouse", "type": "text", "default": "ID"},
            {"key": "token", "type": "secret", "default": "do-not-store"},
        ],
        "skills": ["inventory"],
        "guardrails": {"shell": "off"},
    }
    value.update(overrides)
    return value


def test_import_without_source_id_creates_new_lineage_each_time(store):
    raw = manifest()
    first = store.import_template(raw)
    second = store.import_template(raw)
    assert first["outcome"] == "created"
    assert second["outcome"] == "created"
    assert first["template"]["sourceId"] != second["template"]["sourceId"]
    assert {p.name for p in (config.APP_HOME / "templates").iterdir()} == {
        first["template"]["id"], second["template"]["id"]
    }


def test_import_versions_follow_source_lineage(store):
    raw = manifest(sourceId="S")
    first = store.import_template(raw)
    assert first["outcome"] == "created"
    same = store.import_template(dict(raw))
    assert same["outcome"] == "unchanged"

    different = dict(raw, description="different")
    with pytest.raises(store.TemplateError, match="republish with a version bump"):
        store.import_template(different)

    updated = store.import_template(dict(raw, version="1.1.0"))
    assert updated["outcome"] == "updated"
    assert updated["previousVersion"] == "1.0.0"
    assert updated["template"]["id"] == first["template"]["id"]

    with pytest.raises(store.TemplateError, match=r"v0\.9\.0.*v1\.1\.0"):
        store.import_template(dict(raw, version="0.9.0"))


def test_same_template_id_from_different_source_creates_separate_template(store):
    first = store.import_template(manifest(sourceId="S"))
    second = store.import_template(manifest(sourceId="OTHER", version="0.1.0"))
    assert second["outcome"] == "created"
    assert second["template"]["id"] != first["template"]["id"]


def test_manifest_rejections_are_user_facing(store):
    with pytest.raises(store.TemplateError, match="newer Virtual Worker build"):
        store.import_template(manifest(schemaVersion=3))
    with pytest.raises(store.TemplateError, match="Unknown template section 'customTools'"):
        store.import_template(manifest(customTools=[]))


def test_malformed_versions_are_rejected_before_writing(store):
    templates_dir = config.APP_HOME / "templates"
    for version in ("1.0", "v1.2.3"):
        with pytest.raises(store.TemplateError, match=version):
            store.import_template(manifest(version=version, sourceId=version))
        assert not templates_dir.exists() or not any(templates_dir.iterdir())


def test_semver_prerelease_precedence():
    assert store_semver("1.0.0-alpha", "1.0.0") < 0
    assert store_semver("1.0.0-alpha.2", "1.0.0-alpha.10") < 0
    assert store_semver("1.0.0-alpha", "1.0.0-alpha.1") < 0
    assert store_semver("1.0.0+build.1", "1.0.0+build.2") == 0


def store_semver(a, b):
    return template_store.semver_compare(a, b)


def test_deploy_stamps_template_ref_without_secret_answers(store):
    imported = store.import_template(manifest(sourceId="S"))["template"]
    agent = store.deploy_template(imported["id"], {"warehouse": "SG", "token": "secret"})
    ref = agent["templateRef"]
    assert ref["sourceId"] == "S"
    assert ref["templateId"] == "inventory"
    assert ref["version"] == "1.0.0"
    assert ref["digest"] == imported["digest"]
    assert ref["variables"] == {"warehouse": "SG"}
    assert ref["bindings"] == {}
    assert ref["snapshot"] is None
    assert ref["ledger"] == [{
        "type": "skill", "id": "inventory", "resourceKey": "inventory", "adopted": False,
        "provenance": {"sourceId": "S", "templateId": "inventory", "version": "1.0.0"},
    }]
    assert set(ref["baseline"]) == {
        "persona", "role", "description", "temperature", "topP", "topK",
        "tools", "skills", "guardrails",
    }
    assert "token" not in json.dumps(ref)


def test_list_rows_include_versioning_metadata(store):
    imported = store.import_template(manifest(sourceId="S"))["template"]
    rows = store.list_templates()
    builtin = next(row for row in rows if row["id"] == "intern")
    user = next(row for row in rows if row["id"] == imported["id"])
    for row, source_id in ((builtin, "builtin:intern"), (user, "S")):
        assert row["source"] in ("builtin", "imported")
        assert row["sourceId"] == source_id
        assert row["schemaVersion"] == 1
        assert row["digest"]
        assert row["templateId"]


def test_export_never_emits_lineage_id(store):
    imported = store.import_template(manifest(sourceId="S"))["template"]
    agent = store.deploy_template(imported["id"], {})
    exported = store.template_from_agent(agent["id"])
    assert "sourceId" not in exported
    assert exported["schemaVersion"] == 1
    assert "requiresFeatures" not in exported


# --------------------------------------------------------------------------- #
# M1.5 — publishing flow (FR-15c)
# --------------------------------------------------------------------------- #

def test_export_with_target_embeds_lineage_and_version(store):
    target = store.import_template(manifest(sourceId="S"))["template"]
    agent = store.deploy_template(target["id"], {})
    exported = store.template_from_agent(
        agent["id"], target_source_id=target["sourceId"],
        target_template_id=target["templateId"], version="1.0.1")
    assert exported["sourceId"] == "S"
    assert exported["templateId"] == "inventory"
    assert exported["version"] == "1.0.1"


def test_export_target_roundtrip_updates_same_lineage(store):
    target = store.import_template(manifest(sourceId="S"))["template"]
    agent = store.deploy_template(target["id"], {})
    # Tweak the agent so the re-export differs from the installed content.
    from storage import agent_store
    agent_store.update_agent(agent["id"], {"description": "tweaked"})
    exported = store.template_from_agent(
        agent["id"], target_source_id=target["sourceId"],
        target_template_id=target["templateId"], version="1.0.1")
    outcome = store.import_template(exported)
    assert outcome["outcome"] == "updated"
    assert outcome["template"]["id"] == target["id"]
    assert outcome["template"]["sourceId"] == "S"
    assert outcome["previousVersion"] == "1.0.0"


def test_export_default_version_is_one_zero_zero(store):
    target = store.import_template(manifest(sourceId="S"))["template"]
    agent = store.deploy_template(target["id"], {})
    exported = store.template_from_agent(
        agent["id"], target_source_id=target["sourceId"],
        target_template_id=target["templateId"])
    assert exported["version"] == "1.0.0"


def test_export_rejects_malformed_version(store):
    agent = store.deploy_template("intern", {})
    with pytest.raises(store.TemplateError, match="1.2"):
        store.template_from_agent(agent["id"], version="1.2")


def test_download_user_template_carries_lineage(store):
    imported = store.import_template(manifest(sourceId="S"))["template"]
    result = store.download_template(imported["id"])
    assert result is not None
    stored = json.loads(result["content"])
    assert stored["sourceId"] == "S"
    assert stored["templateId"] == "inventory"
    assert result["filename"] == f"{imported['id']}-v1.0.0.json"


def test_download_builtin_has_no_lineage(store):
    result = store.download_template("intern")
    assert result is not None
    manifest_out = json.loads(result["content"])
    assert "sourceId" not in manifest_out
    assert result["filename"].startswith("intern-v")


def test_download_unknown_id_returns_none(store):
    assert store.download_template("nope-does-not-exist") is None


# --------------------------------------------------------------------------- #
# Id polish — explicit ids are slugs (dialog id field + manifest boundary)
# --------------------------------------------------------------------------- #

def test_import_rejects_non_slug_template_id(store):
    with pytest.raises(store.TemplateError, match="slug"):
        store.import_template(manifest(templateId="Inventory On-Duty"))


def test_export_with_new_template_id_uses_it(store):
    imported = store.import_template(manifest(sourceId="S"))["template"]
    agent = store.deploy_template(imported["id"], {})
    exported = store.template_from_agent(
        agent["id"], name="Inventory On-Duty v2", new_template_id="inventory-on-duty-v2")
    assert exported["templateId"] == "inventory-on-duty-v2"
    assert exported["name"] == "Inventory On-Duty v2"
    assert "sourceId" not in exported  # a new id never fabricates a lineage


def test_export_with_bad_new_template_id_rejected(store):
    imported = store.import_template(manifest(sourceId="S"))["template"]
    agent = store.deploy_template(imported["id"], {})
    with pytest.raises(store.TemplateError, match="slug"):
        store.template_from_agent(agent["id"], new_template_id="Inventory On-Duty")
