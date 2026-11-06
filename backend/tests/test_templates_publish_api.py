"""Publishing flow HTTP API (PRD-29 M1.5, FR-15c) — hermetic route tests.

Covers the export endpoint's new ``targetTemplateId``/``version`` params and the
``GET /api/templates/{id}/download`` endpoint, including the 404/400 error
mapping. Stores are isolated with ``monkeypatch.setattr(config, "APP_HOME", …)``.
"""

import json

import pytest
from fastapi.testclient import TestClient

import config
import storage.agent_store as ags
from storage import template_store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(ags, "_seeded", False)
    import app
    return TestClient(app.app)


def _manifest(**overrides):
    value = {
        "name": "Inventory",
        "persona": "You operate {{warehouse}}.",
        "templateId": "inventory",
        "version": "1.0.0",
        "sourceId": "S",
    }
    value.update(overrides)
    return value


def test_export_with_target_embeds_lineage_and_updates_in_place(client):
    imported = template_store.import_template(_manifest())["template"]
    agent = template_store.deploy_template(imported["id"], {"warehouse": "SG"})

    res = client.get(
        f"/api/templates/from-agent/{agent['id']}",
        params={"targetTemplateId": imported["id"], "version": "1.0.1"})
    assert res.status_code == 200
    tpl = res.json()
    assert tpl["sourceId"] == "S"
    assert tpl["templateId"] == "inventory"
    assert tpl["version"] == "1.0.1"

    outcome = client.post("/api/templates/import", json=tpl).json()
    assert outcome["outcome"] == "updated"
    assert outcome["template"]["id"] == imported["id"]


def test_export_without_target_emits_no_lineage(client):
    agent = template_store.deploy_template("intern", {})
    tpl = client.get(f"/api/templates/from-agent/{agent['id']}").json()
    assert "sourceId" not in tpl


def test_export_unknown_target_is_404(client):
    agent = template_store.deploy_template("intern", {})
    res = client.get(f"/api/templates/from-agent/{agent['id']}",
                     params={"targetTemplateId": "nope"})
    assert res.status_code == 404


def test_export_builtin_target_is_400(client):
    agent = template_store.deploy_template("intern", {})
    res = client.get(f"/api/templates/from-agent/{agent['id']}",
                     params={"targetTemplateId": "intern"})
    assert res.status_code == 400


def test_export_malformed_version_is_400(client):
    agent = template_store.deploy_template("intern", {})
    res = client.get(f"/api/templates/from-agent/{agent['id']}",
                     params={"version": "1.2"})
    assert res.status_code == 400
    assert "1.2" in res.json()["detail"]


def test_download_user_template_is_verbatim_with_lineage(client):
    imported = template_store.import_template(_manifest())["template"]
    res = client.get(f"/api/templates/{imported['id']}/download")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    assert f"{imported['id']}-v1.0.0.json" in res.headers["content-disposition"]
    stored = json.loads(res.content)
    assert stored["sourceId"] == "S"


def test_download_builtin_has_no_lineage(client):
    res = client.get("/api/templates/intern/download")
    assert res.status_code == 200
    assert "sourceId" not in json.loads(res.content)


def test_download_unknown_is_404(client):
    assert client.get("/api/templates/nope-xyz/download").status_code == 404


def test_export_with_new_template_id(client):
    imported = client.post("/api/templates/import", json=_manifest()).json()
    agent = client.post(
        f"/api/templates/{imported['template']['id']}/deploy", json={}).json()
    r = client.get(f"/api/templates/from-agent/{agent['id']}",
                   params={"templateId": "inventory-two", "name": "Inventory Two"})
    assert r.status_code == 200
    body = r.json()
    assert body["templateId"] == "inventory-two"
    assert "sourceId" not in body


def test_export_bad_new_template_id_is_400(client):
    imported = client.post("/api/templates/import", json=_manifest()).json()
    agent = client.post(
        f"/api/templates/{imported['template']['id']}/deploy", json={}).json()
    r = client.get(f"/api/templates/from-agent/{agent['id']}",
                   params={"templateId": "Inventory On-Duty"})
    assert r.status_code == 400
    assert "slug" in r.json()["detail"]


def test_export_both_target_and_new_id_is_400(client):
    imported = client.post("/api/templates/import", json=_manifest()).json()
    agent = client.post(
        f"/api/templates/{imported['template']['id']}/deploy", json={}).json()
    r = client.get(f"/api/templates/from-agent/{agent['id']}",
                   params={"templateId": "inventory-two",
                           "targetTemplateId": imported["template"]["id"]})
    assert r.status_code == 400
    assert "not both" in r.json()["detail"]


def test_import_non_slug_template_id_is_400(client):
    r = client.post("/api/templates/import", json=_manifest(templateId="Inventory On-Duty"))
    assert r.status_code == 400
    assert "slug" in r.json()["detail"]
