"""Wrap-up steering near the tool-step cap: a pre-warn as the run approaches its
limit, and a real synthesized conclusion turn (instead of a static "stopped"
note) once the cap is spent. See agent/loop.py and storage/agent_store.py."""

import asyncio
import json

import pytest

import config
import storage.debug_db as debug_db
from agent import loop
from storage.agent_store import DEFAULT_GUARDRAILS, _guardrails


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _chunk(delta=None, finish=None, usage=None):
    ch = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        ch["usage"] = usage
    return ch


def _registry():
    """A tool that records every call it receives (distinct args each turn keep
    the loop away from the dedup guard)."""
    calls = []

    async def probe(x=0):
        calls.append(x)
        return f"probe-{x}"

    entry = {"fn": probe, "schema": {"type": "function", "function": {
        "name": "probe", "parameters": {"type": "object",
                                         "properties": {"x": {"type": "integer"}}}}}}
    return {"probe": entry}, calls


def _make_stream(*, conclusion="text"):
    """Normal turns always emit one distinct tool call; the conclusion turn (the
    one carrying the step-limit notice) behaves per ``conclusion``."""
    rec = {"payloads": [], "tool_turns": 0, "conclusion_calls": 0}

    async def stream(payload, provider=None):
        msgs = payload.get("messages", [])
        rec["payloads"].append({"messages": [dict(m) for m in msgs],
                                "has_tools": "tools" in payload})
        last = msgs[-1].get("content", "") if msgs else ""
        if isinstance(last, str) and "tool-step limit" in last:
            rec["conclusion_calls"] += 1
            if conclusion == "text":
                yield _chunk(delta={"content": "here is my best answer"})
                yield _chunk(finish="stop", usage={"completion_tokens": 5})
            elif conclusion == "blank":
                yield _chunk(delta={"content": "   \n  "})
                yield _chunk(finish="stop", usage={"completion_tokens": 1})
            elif conclusion == "toolcall":
                yield _chunk(delta={"tool_calls": [
                    {"index": 0, "id": "cc", "type": "function",
                     "function": {"name": "probe", "arguments": json.dumps({"x": 999})}}]})
                yield _chunk(finish="tool_calls", usage={"completion_tokens": 2})
            elif conclusion == "raise":
                raise RuntimeError("boom")
                yield  # pragma: no cover
            return
        i = rec["tool_turns"]
        rec["tool_turns"] += 1
        yield _chunk(delta={"tool_calls": [
            {"index": 0, "id": f"c{i}", "type": "function",
             "function": {"name": "probe", "arguments": json.dumps({"x": i})}}]})
        yield _chunk(finish="tool_calls", usage={"completion_tokens": 1})

    return stream, rec


def _drive(monkeypatch, stream, *, max_iterations, wrapup_warn_at=None):
    registry, calls = _registry()
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        events = []
        async for ev in loop.run([{"role": "user", "content": "go"}],
                                 model="local", tools=registry,
                                 max_iterations=max_iterations,
                                 wrapup_warn_at=wrapup_warn_at):
            events.append(ev)
        return events

    return asyncio.run(collect()), calls


_WARN_FRAGMENT = "tool call(s) left in this run before it is stopped"


def _warn_count(payloads):
    """How many user messages carry the wrap-up pre-warn in the last payload
    (accumulates in convo, so >1 would mean it was injected more than once)."""
    if not payloads:
        return 0
    last = payloads[-1]["messages"]
    return sum(1 for m in last
               if m.get("role") == "user" and _WARN_FRAGMENT in str(m.get("content", "")))


def test_prewarn_injected_once_as_user_message(home, monkeypatch):
    stream, rec = _make_stream()
    _events, _calls = _drive(monkeypatch, stream, max_iterations=4, wrapup_warn_at=1)
    # effective_warn = min(1, 4-1) = 1 → fires when (limit - iter) == 2, i.e. turn 3.
    assert _warn_count(rec["payloads"]) == 1
    # And it is a user-role message (chat-template compatibility).
    warn_msgs = [m for p in rec["payloads"] for m in p["messages"]
                 if _WARN_FRAGMENT in str(m.get("content", ""))]
    assert warn_msgs and all(m["role"] == "user" for m in warn_msgs)
    assert "at most 1 tool call(s) left" in warn_msgs[0]["content"]
    # Transparency: the model is told to disclose WHY it is wrapping up (the
    # step limit) so the user knows they can ask it to continue.
    assert "near this run's step limit" in warn_msgs[0]["content"]
    assert "continue digging" in warn_msgs[0]["content"]


def test_prewarn_disabled_when_zero(home, monkeypatch):
    stream, rec = _make_stream()
    _drive(monkeypatch, stream, max_iterations=4, wrapup_warn_at=0)
    assert _warn_count(rec["payloads"]) == 0


def test_prewarn_skipped_on_unlimited_backstop(home, monkeypatch):
    # max_iterations=0 → unlimited backstop; the stream would run forever, so make
    # the model conclude on its own after a couple of tool turns.
    rec = {"payloads": [], "tool_turns": 0}

    async def stream(payload, provider=None):
        rec["payloads"].append({"messages": [dict(m) for m in payload.get("messages", [])]})
        i = rec["tool_turns"]
        rec["tool_turns"] += 1
        if i >= 2:
            yield _chunk(delta={"content": "all done"})
            yield _chunk(finish="stop", usage={"completion_tokens": 2})
            return
        yield _chunk(delta={"tool_calls": [
            {"index": 0, "id": f"c{i}", "type": "function",
             "function": {"name": "probe", "arguments": json.dumps({"x": i})}}]})
        yield _chunk(finish="tool_calls", usage={"completion_tokens": 1})

    registry, _calls = _registry()
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        async for _ in loop.run([{"role": "user", "content": "go"}], model="local",
                                tools=registry, max_iterations=0, wrapup_warn_at=2):
            pass
    asyncio.run(collect())
    assert _warn_count(rec["payloads"]) == 0


def test_conclusion_turn_runs_with_tools_and_text_becomes_final(home, monkeypatch):
    stream, rec = _make_stream(conclusion="text")
    events, _calls = _drive(monkeypatch, stream, max_iterations=2, wrapup_warn_at=0)
    assert rec["conclusion_calls"] == 1
    # The conclusion request keeps the normal payload shape — tools included.
    assert rec["payloads"][-1]["has_tools"] is True
    texts = [e["delta"] for e in events if e.get("type") == "text"]
    assert "here is my best answer" in texts
    # No static "stopped" note, no fallback suffix.
    assert not any("stopped: reached max tool iterations" in t for t in texts)
    assert not any("answer may be incomplete" in t for t in texts)
    assert events[-1]["type"] == "done"


def test_conclusion_tool_calls_are_not_executed(home, monkeypatch):
    stream, rec = _make_stream(conclusion="toolcall")
    events, calls = _drive(monkeypatch, stream, max_iterations=2, wrapup_warn_at=0)
    # Two normal tool turns ran (x=0, x=1); the conclusion's x=999 call is ignored.
    assert calls == [0, 1]
    assert 999 not in calls
    # No usable text → the deterministic fallback still tells the user something.
    texts = [e["delta"] for e in events if e.get("type") == "text"]
    assert any("answer may be incomplete — hit the step limit" in t for t in texts)
    assert events[-1]["type"] == "done"


def test_blank_conclusion_falls_back_to_suffix(home, monkeypatch):
    stream, _rec = _make_stream(conclusion="blank")
    events, _calls = _drive(monkeypatch, stream, max_iterations=2, wrapup_warn_at=0)
    texts = [e["delta"] for e in events if e.get("type") == "text"]
    assert any("answer may be incomplete — hit the step limit" in t for t in texts)
    assert events[-1]["type"] == "done"


def test_conclusion_error_degrades_to_suffix(home, monkeypatch):
    stream, _rec = _make_stream(conclusion="raise")
    events, _calls = _drive(monkeypatch, stream, max_iterations=2, wrapup_warn_at=0)
    texts = [e["delta"] for e in events if e.get("type") == "text"]
    assert any("answer may be incomplete — hit the step limit" in t for t in texts)
    assert events[-1]["type"] == "done"


def test_guardrail_wrapupwarnat_default_and_coercion():
    assert DEFAULT_GUARDRAILS["wrapupWarnAt"] == 2
    # default present when unset
    assert _guardrails(json.dumps({}))["wrapupWarnAt"] == 2
    # garbage → default 2
    assert _guardrails(json.dumps({"wrapupWarnAt": "nonsense"}))["wrapupWarnAt"] == 2
    # clamp to [0, 100]
    assert _guardrails(json.dumps({"wrapupWarnAt": -5}))["wrapupWarnAt"] == 0
    assert _guardrails(json.dumps({"wrapupWarnAt": 999}))["wrapupWarnAt"] == 100
    assert _guardrails(json.dumps({"wrapupWarnAt": 3}))["wrapupWarnAt"] == 3
