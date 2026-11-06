"""Agent store — default seed + CRUD."""

import pytest

import config
import storage.agent_store as ags


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(ags, "_seeded", False)  # fresh seed per test db
    return ags


def test_default_assistant_seeded(store):
    agents = store.list_agents()
    assert len(agents) == 1
    a = agents[0]
    assert a["id"] == store.DEFAULT_AGENT_ID
    assert a["name"] == "Assistant"
    assert store.get_default_agent()["id"] == store.DEFAULT_AGENT_ID


def test_create_and_get(store):
    a = store.create_agent({
        "name": "Researcher", "persona": "You research things.",
        "temperature": 0.3, "tools": ["web_search"], "skills": ["refund-handler"],
    })
    assert a["name"] == "Researcher"
    assert a["temperature"] == 0.3
    assert a["tools"] == ["web_search"]
    assert a["skills"] == ["refund-handler"]
    got = store.get_agent(a["id"])
    assert got["persona"] == "You research things."


def test_web_search_defaults_and_persists(store):
    # Default is SearXNG with empty key/url (falls back to the global env at run time).
    assert store.get_default_agent()["webSearch"] == {
        "provider": "searxng", "apiKey": "", "searxngUrl": ""}
    a = store.create_agent({"name": "S", "webSearch": {"provider": "tavily", "apiKey": " k "}})
    assert a["webSearch"]["provider"] == "tavily"
    assert a["webSearch"]["apiKey"] == "k"          # trimmed
    # Partial update merges over existing (key preserved when only provider changes).
    upd = store.update_agent(a["id"], {"webSearch": {"provider": "brave"}})
    assert upd["webSearch"]["provider"] == "brave" and upd["webSearch"]["apiKey"] == "k"
    # Unknown provider falls back to searxng.
    b = store.create_agent({"name": "B", "webSearch": {"provider": "bogus"}})
    assert b["webSearch"]["provider"] == "searxng"


def test_wikis_attachment_persists(store):
    assert store.get_default_agent()["wikis"] == []
    a = store.create_agent({"name": "W", "wikis": ["wiki123"]})
    assert a["wikis"] == ["wiki123"]
    upd = store.update_agent(a["id"], {"persona": "changed"})
    assert upd["wikis"] == ["wiki123"]        # untouched on unrelated update


def test_model_source_defaults_and_persists(store):
    # Default agent + a plain create both default to the local source.
    assert store.get_default_agent()["modelSource"] == "local"
    a = store.create_agent({"name": "Ext", "modelSource": "byok",
                            "provider": "deepseek", "model": "deepseek-chat"})
    assert a["modelSource"] == "byok"
    assert a["provider"] == "deepseek"
    assert a["model"] == "deepseek-chat"
    # Switching back to local clears the stale external model id (model=None).
    back = store.update_agent(a["id"], {"modelSource": "local", "provider": "", "model": None})
    assert back["modelSource"] == "local"
    assert back["provider"] == ""
    assert back["model"] is None


def test_model_source_invalid_coerced_to_local(store):
    a = store.create_agent({"name": "Y", "modelSource": "bogus"})
    assert a["modelSource"] == "local"


def test_update_partial(store):
    a = store.create_agent({"name": "X", "tools": ["web_search"]})
    updated = store.update_agent(a["id"], {"persona": "new persona"})
    assert updated["persona"] == "new persona"
    assert updated["tools"] == ["web_search"]  # untouched
    # clearing a list works (empty != None)
    cleared = store.update_agent(a["id"], {"tools": []})
    assert cleared["tools"] == []


def test_sampling_params_roundtrip(store):
    a = store.create_agent({"name": "S", "topP": 0.95, "topK": 64})
    assert a["topP"] == 0.95
    assert a["topK"] == 64
    # blank/unset -> None (model default)
    b = store.create_agent({"name": "B"})
    assert b["topP"] is None and b["topK"] is None
    # explicit clear back to default
    cleared = store.update_agent(a["id"], {"topP": None, "topK": None})
    assert cleared["topP"] is None and cleared["topK"] is None


def test_delete(store):
    a = store.create_agent({"name": "Temp"})
    assert store.delete_agent(a["id"]) is True
    assert store.get_agent(a["id"]) is None
    assert store.delete_agent("missing") is False


def test_default_agent_falls_back_when_assistant_deleted(store):
    store.list_agents()  # seed
    other = store.create_agent({"name": "Other"})
    store.delete_agent(store.DEFAULT_AGENT_ID)
    assert store.get_default_agent()["id"] == other["id"]


# ─── connectorGrants + askBudget ─────────────────────────────────────────────

def test_connector_grants_default_empty(store):
    a = store.create_agent({"name": "A"})
    assert a["connectorGrants"] == {}
    assert store.get_agent(a["id"])["connectorGrants"] == {}


def test_connector_grants_persist_and_normalize(store):
    a = store.create_agent({"name": "A", "connectorGrants": {
        "gitlab": {"write": True, "merge": True},
        "jira": {"write": True},          # merge fills to False
        "bad": "nope",                     # non-dict dropped
    }})
    assert a["connectorGrants"] == {
        "gitlab": {"write": True, "merge": True},
        "jira": {"write": True, "merge": False},
    }
    # preserved on a partial update, replaced when supplied
    upd = store.update_agent(a["id"], {"description": "x"})
    assert upd["connectorGrants"]["gitlab"] == {"write": True, "merge": True}
    upd = store.update_agent(a["id"], {"connectorGrants": {"gitlab": {"write": True}}})
    assert upd["connectorGrants"] == {"gitlab": {"write": True, "merge": False}}


def test_ask_budget_default_none_and_clamped(store):
    a = store.create_agent({"name": "A"})
    assert a["askBudget"] is None
    a = store.create_agent({"name": "B", "askBudget": 5})
    assert a["askBudget"] == 5
    assert store.create_agent({"name": "C", "askBudget": 99})["askBudget"] == 20
    assert store.create_agent({"name": "D", "askBudget": -3})["askBudget"] == 0
    assert store.create_agent({"name": "E", "askBudget": "nope"})["askBudget"] is None
    # preserved on partial update
    upd = store.update_agent(a["id"], {"description": "x"})
    assert upd["askBudget"] == 5
