"""virtual worker Templates — built-ins, deploy (with variable substitution),
export-from-agent (round-trip + secret scrub), and import validation (PRD-05)."""

import pytest

import config
import storage.agent_store as ags
import storage.template_store as ts


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(ags, "_seeded", False)  # fresh seeded db per test
    return ts


# --------------------------------------------------------------------------- #
# Built-in gallery
# --------------------------------------------------------------------------- #

def test_builtins_listed(store):
    templates = store.list_templates()
    ids = {t["id"] for t in templates}
    assert {"customer-support-rep", "research-analyst", "code-reviewer",
            "personal-assistant"}.issubset(ids)
    for t in templates:
        assert t["source"] == "builtin"
        # Every built-in carries the normalized guardrail floor.
        assert set(t["guardrails"]) >= {"shell", "verify", "maxIterations", "dailyCap"}


def test_get_builtin_and_missing(store):
    assert store.get_template("research-analyst")["role"] == "Research Analyst"
    assert store.get_template("does-not-exist") is None


# --------------------------------------------------------------------------- #
# Deploy + variable substitution
# --------------------------------------------------------------------------- #

def test_deploy_creates_agent_with_substitution(store):
    agent = store.deploy_template("customer-support-rep",
                                  {"business_name": "Warung Kopi"})
    assert agent is not None
    # The {{business_name}} token is substituted into name + persona.
    assert "Warung Kopi" in agent["name"]
    assert "Warung Kopi" in agent["persona"]
    assert "{{" not in agent["persona"]
    assert agent["role"] == "Customer Support Representative"
    assert agent["tools"] == ["web_search", "web_fetch"]
    # The agent is a real, fetchable row.
    assert ags.get_agent(agent["id"])["name"] == agent["name"]


def test_deploy_uses_defaults_when_no_variables(store):
    agent = store.deploy_template("customer-support-rep", {})
    assert "Acme" in agent["persona"]  # the declared default


def test_deploy_maps_working_dir_and_seeds_tasks(store):
    agent = store.deploy_template("customer-support-rep",
                                  {"business_name": "Acme"})
    # The support rep ships one starter task seeded onto the board.
    import storage.task_store as tks
    tasks = tks.list_for(agent["id"])
    assert any("Acme" in t["title"] for t in tasks)


def test_deploy_twice_disambiguates_names(store):
    a = store.deploy_template("research-analyst", {})
    b = store.deploy_template("research-analyst", {})
    assert a["id"] != b["id"]
    assert a["name"] != b["name"]  # second gets a numeric suffix


def test_deploy_missing_template_returns_none(store):
    assert store.deploy_template("nope", {}) is None


def test_deploy_working_dir_variable(store):
    agent = store.deploy_template("code-reviewer", {"working_dir": "/tmp/repo"})
    assert agent["workingDir"] == "/tmp/repo"
    assert agent["guardrails"]["shell"] == "readonly"


# --------------------------------------------------------------------------- #
# Export from agent (round-trip + secret scrub)
# --------------------------------------------------------------------------- #

def test_export_from_agent_round_trips(store):
    src = ags.create_agent({
        "name": "Tuned Analyst", "persona": "You analyze {{domain}} deeply.",
        "role": "Analyst", "temperature": 0.3, "tools": ["web_search"],
        "guardrails": {"shell": "readonly"},
    })
    tpl = store.template_from_agent(src["id"])
    assert tpl["name"] == "Tuned Analyst"
    assert tpl["persona"] == "You analyze {{domain}} deeply."
    assert tpl["tools"] == ["web_search"]
    assert tpl["guardrails"]["shell"] == "readonly"

    # Importing it then deploying recreates an equivalent agent.
    imported = store.import_template(tpl)
    redeployed = store.deploy_template(imported["id"], {"domain": "markets"})
    assert redeployed["role"] == "Analyst"
    assert "markets" in redeployed["persona"]


def test_export_missing_agent(store):
    assert store.template_from_agent("ghost") is None


def test_scrub_secrets_removes_sensitive_keys(store):
    dirty = {
        "name": "Leaky",
        "apiKey": "sk-secret",
        "guardrails": {"shell": "off", "auth_token": "abc"},
        "nested": [{"password": "hunter2", "keep": "ok"}],
    }
    clean = store.scrub_secrets(dirty)
    assert "apiKey" not in clean
    assert "auth_token" not in clean["guardrails"]
    assert "password" not in clean["nested"][0]
    assert clean["nested"][0]["keep"] == "ok"
    assert clean["name"] == "Leaky"


def test_export_scrubs_secrets_via_normalize(store):
    src = ags.create_agent({"name": "Sec", "persona": "Hi."})
    tpl = store.template_from_agent(src["id"])
    # Inject a secret then re-normalize (export path runs scrub_secrets).
    tpl["api_key"] = "sk-leak"
    safe = store.validate_template(tpl)
    assert "api_key" not in safe


# --------------------------------------------------------------------------- #
# Import validation
# --------------------------------------------------------------------------- #

def test_import_validates_and_persists(store):
    tpl = store.import_template({
        "name": "House Support", "persona": "You support {{business_name}}.",
        "tools": ["web_fetch"], "variables": [
            {"key": "business_name", "label": "Business", "default": "Co"}],
    })
    assert tpl["source"] == "imported"
    # It now appears in the gallery as a user/imported template.
    listed = {t["id"]: t for t in store.list_templates()}
    assert tpl["id"] in listed
    assert listed[tpl["id"]]["source"] == "imported"


def test_import_rejects_non_dict(store):
    with pytest.raises(store.TemplateError):
        store.import_template("not a template")


def test_import_rejects_missing_persona(store):
    with pytest.raises(store.TemplateError):
        store.import_template({"name": "No persona"})


def test_import_rejects_missing_name(store):
    with pytest.raises(store.TemplateError):
        store.import_template({"persona": "Hi."})


def test_delete_user_template_but_not_builtin(store):
    tpl = store.import_template({"name": "Temp", "persona": "Hi."})
    assert store.delete_template(tpl["id"]) is True
    assert store.delete_template(tpl["id"]) is False  # already gone
    assert store.delete_template("research-analyst") is False  # built-in is read-only


def test_import_disambiguates_colliding_ids(store):
    a = store.import_template({"name": "Same Name", "persona": "Hi."})
    b = store.import_template({"name": "Same Name", "persona": "Hi."})
    assert a["id"] != b["id"]
