"""Per-agent / per-chat reasoning effort: store round-trip + egress translation."""

import pytest

import config
import storage.agent_store as ags
from gateway.egress import (
    _openai_body,
    _to_anthropic_request,
    _to_codex_request,
    _to_responses_request,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(ags, "_seeded", False)  # fresh seed per test db
    return ags


# --------------------------------------------------------------------------- #
# agent_store round-trip
# --------------------------------------------------------------------------- #

def test_reasoning_effort_round_trip(store):
    a = store.create_agent({"name": "Reasoner", "reasoningEffort": "high"})
    assert a["reasoningEffort"] == "high"
    assert store.get_agent(a["id"])["reasoningEffort"] == "high"


def test_reasoning_effort_cleared_to_empty(store):
    a = store.create_agent({"name": "Reasoner", "reasoningEffort": "high"})
    updated = store.update_agent(a["id"], {"reasoningEffort": ""})
    assert updated["reasoningEffort"] == ""


def test_reasoning_effort_invalid_normalized(store):
    a = store.create_agent({"name": "Reasoner", "reasoningEffort": "banana"})
    assert a["reasoningEffort"] == ""
    updated = store.update_agent(a["id"], {"reasoningEffort": "banana"})
    assert updated["reasoningEffort"] == ""


def test_reasoning_effort_default_empty(store):
    a = store.create_agent({"name": "Plain"})
    assert a["reasoningEffort"] == ""


# --------------------------------------------------------------------------- #
# egress translation (pure functions, no network)
# --------------------------------------------------------------------------- #

def test_openai_body_effort_drops_sampling_and_keeps_xhigh():
    body = _openai_body({"model": "gpt-5", "reasoning_effort": "xhigh",
                         "temperature": 0.7, "top_p": 0.9, "top_k": 40},
                        {"provider": "openai"})
    assert body["reasoning_effort"] == "xhigh"
    assert "temperature" not in body
    assert "top_p" not in body
    assert "top_k" not in body  # always stripped for openai kind


def test_openai_body_no_effort_keeps_temperature():
    body = _openai_body({"model": "gpt-4o", "temperature": 0.7, "top_k": 40}, {})
    assert body["temperature"] == 0.7
    assert "top_k" not in body
    assert "reasoning_effort" not in body


def test_compatible_openai_gateway_drops_effort_but_keeps_sampling():
    body = _openai_body(
        {"model": "gateway-reasoner", "reasoning_effort": "high", "temperature": 0.7},
        {"provider": "openai-compatible"})
    assert "reasoning_effort" not in body
    assert body["temperature"] == 0.7


def test_anthropic_request_effort_supported():
    req = _to_anthropic_request(
        {"model": "claude-opus-5", "reasoning_effort": "medium",
         "temperature": 0.7, "messages": []}, {})
    assert req["output_config"] == {"effort": "medium"}
    assert "temperature" not in req


def test_anthropic_request_effort_unsupported_model():
    req = _to_anthropic_request(
        {"model": "claude-sonnet-4-20250514", "reasoning_effort": "medium",
         "temperature": 0.7, "messages": []}, {})
    assert "output_config" not in req


def test_anthropic_request_no_effort():
    req = _to_anthropic_request(
        {"model": "claude-opus-5", "temperature": 0.5, "messages": []}, {})
    assert "output_config" not in req


def test_responses_request_effort_drops_sampling():
    body = _to_responses_request(
        {"model": "gpt-5", "reasoning_effort": "high",
         "temperature": 0.7, "top_p": 0.9, "messages": []})
    assert body["reasoning"] == {"effort": "high"}
    assert "temperature" not in body
    assert "top_p" not in body


def test_responses_request_off_uses_native_none_effort():
    body = _to_responses_request(
        {"model": "gpt-5", "reasoning_effort": "off", "temperature": 0.7,
         "messages": []})
    assert body["reasoning"] == {"effort": "none"}
    assert "temperature" not in body


def test_codex_request_uses_effort():
    body = _to_codex_request(
        {"model": "gpt-5-codex", "reasoning_effort": "xhigh", "messages": []})
    assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}


def test_codex_request_default_effort():
    body = _to_codex_request({"model": "gpt-5-codex", "messages": []})
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}


def test_codex_request_off_uses_native_none_effort():
    body = _to_codex_request(
        {"model": "gpt-5-codex", "reasoning_effort": "off", "messages": []})
    assert body["reasoning"] == {"effort": "none", "summary": "auto"}


# --------------------------------------------------------------------------- #
# debug trace round-trip
# --------------------------------------------------------------------------- #

def test_debug_run_records_reasoning_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import storage.debug_db as ddb
    monkeypatch.setattr(ddb, "_schema_ready", False)
    ddb.insert_run({
        "run_id": "r1", "session_id": "s1", "start_time": "2026-08-19T00:00:00",
        "model": "gpt-5.2", "provider": "openai", "reasoning_effort": "high",
    }, [])
    ddb.insert_run({
        "run_id": "r2", "session_id": "s1", "start_time": "2026-08-19T00:01:00",
        "model": "local-9b", "provider": "local",
    }, [])
    runs = {r["runId"]: r for r in ddb.get_session("s1")["runs"]}
    assert runs["r1"]["reasoningEffort"] == "high"
    assert runs["r2"]["reasoningEffort"] is None


# --------------------------------------------------------------------------- #
# route round-trip (regression: AgentIn must carry the field — Pydantic
# silently drops unknown keys, which made saves revert to default)
# --------------------------------------------------------------------------- #

def test_agents_route_round_trips_reasoning_effort(store):
    from fastapi.testclient import TestClient
    import app
    client = TestClient(app.app)
    created = client.post("/api/agents", json={"name": "R"}).json()
    updated = client.put(f"/api/agents/{created['id']}",
                         json={"reasoningEffort": "xhigh"}).json()
    assert updated["reasoningEffort"] == "xhigh"
    got = client.get(f"/api/agents/{created['id']}").json()
    assert got["reasoningEffort"] == "xhigh"
