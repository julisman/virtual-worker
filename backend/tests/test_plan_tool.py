"""Step checklist — the opt-in ``set_plan`` tool + its compose() gate."""

import asyncio

import pytest

import config
from agent.compose import compose
from agent.plan_tool import make_plan_tool


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


def _fn():
    return make_plan_tool()["set_plan"]["fn"]


# --------------------------------------------------------------------------- #
# The tool registry entry
# --------------------------------------------------------------------------- #

def test_registry_shape():
    reg = make_plan_tool()
    assert set(reg) == {"set_plan"}
    entry = reg["set_plan"]
    assert entry["kind"] == "plan"
    assert entry["schema"]["function"]["name"] == "set_plan"
    assert callable(entry["fn"])
    # Full-replacement semantics are stated in the description.
    assert "replace" in entry["schema"]["function"]["description"].lower()


# --------------------------------------------------------------------------- #
# fn validation
# --------------------------------------------------------------------------- #

def test_ok_path_reports_count():
    out = _run(_fn()(steps=[
        {"text": "Look up order", "status": "done"},
        {"text": "Check stock", "status": "active"},
        {"text": "Reply", "status": "pending"},
    ]))
    assert out == "ok (3 steps)"


def test_caps_at_ten_steps():
    steps = [{"text": f"step {i}", "status": "pending"} for i in range(25)]
    assert _run(_fn()(steps=steps)) == "ok (10 steps)"


def test_text_truncated_and_status_coerced():
    # A long text and a bogus status are accepted (truncated / coerced), so the
    # call still succeeds — it's not treated as malformed.
    out = _run(_fn()(steps=[{"text": "x" * 500, "status": "bogus"}]))
    assert out == "ok (1 steps)"


def test_empty_returns_error_string():
    assert "set_plan needs" in _run(_fn()(steps=[]))
    assert "set_plan needs" in _run(_fn()(steps=None))
    assert "set_plan needs" in _run(_fn()())


def test_malformed_items_only_returns_error_string():
    # A list of unusable items (no text) is malformed → error string, not a raise.
    out = _run(_fn()(steps=[{"status": "done"}, "nope", 42]))
    assert "no usable steps" in out


def test_never_raises_on_garbage():
    assert isinstance(_run(_fn()(steps="not a list")), str)
    assert isinstance(_run(_fn()(steps=123)), str)


# --------------------------------------------------------------------------- #
# compose() gate — tool + guidance present iff the stepChecklist guardrail is on
# --------------------------------------------------------------------------- #

def test_compose_registers_plan_when_enabled(home):
    agent = {"id": "a", "persona": "P", "guardrails": {"stepChecklist": True}}
    system, registry, *_ = compose(agent)
    assert "set_plan" in registry
    assert "set_plan tool" in system


def test_compose_omits_plan_by_default(home):
    _, registry, *_ = compose({"id": "a", "persona": "P"})
    assert "set_plan" not in registry


def test_compose_omits_plan_when_flag_false(home):
    agent = {"id": "a", "persona": "P", "guardrails": {"stepChecklist": False}}
    system, registry, *_ = compose(agent)
    assert "set_plan" not in registry
    assert "set_plan tool" not in system
