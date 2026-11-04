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
