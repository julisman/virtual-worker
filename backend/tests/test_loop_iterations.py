"""Iteration-cap semantics in agent/loop.py: max_iterations None → legacy
default (MAX_ITERATIONS), 0 → unlimited (runaway backstop only), N → hard cap.
The 0 case is what the Guardrails "Max steps / task = unlimited" setting sends."""

import asyncio
import json

import pytest

import config
import storage.debug_db as debug_db
from agent import loop


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


def _stream_n_distinct_calls(n):
    """Model makes ``n`` tool calls with DISTINCT args (so the repeat-call
    guard never trips), then answers. Counts turns actually granted."""
    seen = {"turn": 0}

    async def stream(payload, provider=None):
        i = seen["turn"]
        seen["turn"] += 1
        if i < n:
            yield _chunk(delta={"tool_calls": [{
                "index": 0, "id": f"c{i}", "type": "function",
                "function": {"name": "step", "arguments": json.dumps({"x": i})}}]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 2})
        else:
            yield _chunk(delta={"content": "done"})
            yield _chunk(finish="stop", usage={"completion_tokens": 2})

    return stream, seen


def _registry():
    async def step(**kw):
        return f"ok {kw.get('x')}"
    return {"step": {"fn": step, "schema": {"type": "function", "function": {
        "name": "step", "parameters": {"type": "object",
                                       "properties": {"x": {"type": "integer"}}}}}}}


def _drive(monkeypatch, max_iterations, model_turns):
    stream, seen = _stream_n_distinct_calls(model_turns)
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        out = []
        async for ev in loop.run([{"role": "user", "content": "go"}],
                                 model="local", tools=_registry(),
                                 max_iterations=max_iterations):
            out.append(ev)
        return out

    events = asyncio.run(collect())
    return seen["turn"], events


def test_none_keeps_legacy_default_cap(home, monkeypatch):
    turns, _ = _drive(monkeypatch, max_iterations=None, model_turns=30)
    # Stopped by the cap, not the model: MAX_ITERATIONS tool turns + 1 extra
    # (non-tool) conclusion turn that synthesizes the final answer at the limit.
    assert turns == loop.MAX_ITERATIONS + 1


def test_zero_means_unlimited(home, monkeypatch):
    # 20 tool turns + the final answer — far past the legacy cap of 6.
    turns, events = _drive(monkeypatch, max_iterations=0, model_turns=20)
    assert turns == 21
    assert any(ev.get("type") == "text" and "done" in (ev.get("delta") or "")
               for ev in events)


def test_explicit_cap_respected(home, monkeypatch):
    turns, _ = _drive(monkeypatch, max_iterations=3, model_turns=30)
    assert turns == 4      # 3 capped tool turns + 1 conclusion turn at the limit
