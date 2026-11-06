"""Per-agent, per-server MCP tool selection: store round-trip, route round-trip,
and the runner merge helper (server ceiling ∩ agent selection)."""

import pytest

import config
import storage.agent_store as ags
from agent.runner import merge_mcp_allow


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(ags, "_seeded", False)  # fresh seed per test db
    return ags


# --------------------------------------------------------------------------- #
# agent_store round-trip
# --------------------------------------------------------------------------- #

def test_mcp_tool_allow_round_trip(store):
    a = store.create_agent({"name": "A", "mcpToolAllow": {"srv1": ["read", "write"]}})
    assert a["mcpToolAllow"] == {"srv1": ["read", "write"]}
    assert store.get_agent(a["id"])["mcpToolAllow"] == {"srv1": ["read", "write"]}


def test_mcp_tool_allow_default_empty(store):
    a = store.create_agent({"name": "Plain"})
    assert a["mcpToolAllow"] == {}


def test_mcp_tool_allow_update_preserves_when_absent(store):
    a = store.create_agent({"name": "A", "mcpToolAllow": {"srv1": ["read"]}})
    updated = store.update_agent(a["id"], {"name": "A2"})
    assert updated["mcpToolAllow"] == {"srv1": ["read"]}


def test_mcp_tool_allow_update_overwrites(store):
    a = store.create_agent({"name": "A", "mcpToolAllow": {"srv1": ["read"]}})
    updated = store.update_agent(a["id"], {"mcpToolAllow": {"srv1": ["write"]}})
    assert updated["mcpToolAllow"] == {"srv1": ["write"]}


def test_mcp_tool_allow_invalid_normalizes(store):
    a = store.create_agent({"name": "A", "mcpToolAllow": {
        "srv1": ["  keep ", "", 3, "keep"],   # stripped; blanks/non-strings dropped
        "srv2": [],                            # empty list KEPT — explicit "none"
        "srv3": "notalist",                    # non-list value dropped
        7: ["x"],                              # non-string key dropped
    }})
    assert a["mcpToolAllow"] == {"srv1": ["keep", "3", "keep"], "srv2": []}


def test_mcp_tool_allow_garbage_normalizes_to_empty(store):
    a = store.create_agent({"name": "A", "mcpToolAllow": "banana"})
    assert a["mcpToolAllow"] == {}


# --------------------------------------------------------------------------- #
# route round-trip (regression: AgentIn must carry the field — Pydantic
# silently drops unknown keys)
# --------------------------------------------------------------------------- #

def test_agents_route_round_trips_mcp_tool_allow(store):
    from fastapi.testclient import TestClient
    import app
    client = TestClient(app.app)
    created = client.post("/api/agents", json={"name": "R"}).json()
    sel = {"srv1": ["read"]}
    updated = client.put(f"/api/agents/{created['id']}", json={"mcpToolAllow": sel}).json()
    assert updated["mcpToolAllow"] == sel
    got = client.get(f"/api/agents/{created['id']}").json()
    assert got["mcpToolAllow"] == sel


def test_agents_route_round_trips_connector_grants_and_ask_budget(store):
    from fastapi.testclient import TestClient
    import app
    client = TestClient(app.app)
    created = client.post("/api/agents", json={"name": "R"}).json()
    assert created["connectorGrants"] == {} and created["askBudget"] is None
    grants = {"gitlab": {"write": True, "merge": True}}
    updated = client.put(f"/api/agents/{created['id']}",
                         json={"connectorGrants": grants, "askBudget": 7}).json()
    assert updated["connectorGrants"] == grants
    assert updated["askBudget"] == 7
    got = client.get(f"/api/agents/{created['id']}").json()
    assert got["connectorGrants"] == grants and got["askBudget"] == 7


# --------------------------------------------------------------------------- #
# runner merge helper
# --------------------------------------------------------------------------- #

def test_merge_neither_set_returns_cfg_unchanged():
    cfg = {"id": "s", "slug": "s"}
    assert merge_mcp_allow(cfg, None) is cfg


def test_merge_explicit_empty_selection_drops_server():
    # A present-but-empty selection is the UI's "uncheck all" → expose nothing.
    assert merge_mcp_allow({"id": "s", "slug": "s"}, []) is None
    assert merge_mcp_allow({"id": "s", "allowedTools": ["a"]}, []) is None


def test_merge_only_ceiling_unchanged():
    cfg = {"id": "s", "allowedTools": ["a", "b"]}
    assert merge_mcp_allow(cfg, None) is cfg


def test_merge_only_selection_sets_allowed():
    cfg = {"id": "s"}
    out = merge_mcp_allow(cfg, ["a", "b"])
    assert out["allowedTools"] == ["a", "b"]
    assert cfg.get("allowedTools") is None  # original untouched (copy)


def test_merge_both_intersects():
    cfg = {"id": "s", "allowedTools": ["a", "b", "c"]}
    out = merge_mcp_allow(cfg, ["b", "c", "z"])
    assert out["allowedTools"] == ["b", "c"]


def test_merge_empty_intersection_drops_server():
    cfg = {"id": "s", "allowedTools": ["a", "b"]}
    assert merge_mcp_allow(cfg, ["x", "y"]) is None


def test_runner_overlay_skips_dropped_server():
    # Mirrors the overlay loop in stream_run: a server whose selection is wholly
    # outside its ceiling is dropped; the others carry their effective allowlist.
    servers = {
        "s1": {"id": "s1", "allowedTools": ["a", "b"]},   # ceiling only
        "s2": {"id": "s2"},                                # no ceiling
        "s3": {"id": "s3", "allowedTools": ["a", "b"]},   # selection outside ceiling
    }
    tool_allow = {"s1": ["a"], "s2": ["z"], "s3": ["x"]}
    out = []
    for i in ["s1", "s2", "s3"]:
        merged = merge_mcp_allow(servers[i], tool_allow.get(i))
        if merged is not None:
            out.append(merged)
    assert [c["id"] for c in out] == ["s1", "s2"]
    assert out[0]["allowedTools"] == ["a"]
    assert out[1]["allowedTools"] == ["z"]
