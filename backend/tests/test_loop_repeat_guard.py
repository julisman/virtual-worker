"""Repeat-call guard + empty-final-answer nudge (agent/loop.py).

Small local models loop on the exact same failing tool call and sometimes end
a run with an empty message after tool work. The loop warns on an identical
call with an identical result, refuses to execute the third identical repeat,
and retries once with a system nudge when the final message is empty."""

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
    monkeypatch.setattr(loop, "_SESSION_UNLOCKS", {})
    return tmp_path


def _chunk(delta=None, finish=None, usage=None):
    ch = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        ch["usage"] = usage
    return ch


def _scripted_stream(turns):
    """Each entry: list of (name, args) tool calls, or a string answer, or ''."""
    seen = {"payloads": [], "n": 0}

    async def stream(payload, provider=None):
        i = seen["n"]
        seen["n"] += 1
        seen["payloads"].append(payload)
        step = turns[i] if i < len(turns) else "done"
        if isinstance(step, list):
            for j, (name, args) in enumerate(step):
                yield _chunk(delta={"tool_calls": [{
                    "index": j, "id": f"c{i}_{j}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:
            if step:
                yield _chunk(delta={"content": step})
            yield _chunk(finish="stop", usage={"completion_tokens": 2})

    return stream, seen


def _run(registry, turns, monkeypatch, **kw):
    stream, seen = _scripted_stream(turns)
    monkeypatch.setattr(loop.egress, "stream", stream)

    async def collect():
        out = []
        async for ev in loop.run([{"role": "user", "content": "hi"}],
                                 model="local", tools=registry, **kw):
            out.append(ev)
        return out
    return asyncio.run(collect()), seen


def _counting_registry(results=None):
    calls = {"n": 0}

    async def probe(**kw):
        calls["n"] += 1
        if results:
            return results[min(calls["n"] - 1, len(results) - 1)]
        return "no messages found"

    reg = {"probe": {"fn": probe, "schema": {"type": "function", "function": {
        "name": "probe", "parameters": {"type": "object", "properties": {
            "q": {"type": "string"}}}}}}}
    return reg, calls


def test_third_identical_call_not_executed(home, monkeypatch):
    reg, calls = _counting_registry()
    same = [("probe", {"q": "x"})]
    events, _ = _run(reg, [same, same, same, "giving up"], monkeypatch)
    assert calls["n"] == 2                                  # 3rd repeat skipped
    results = [e for e in events if e.get("type") == "tool_result"]
    assert "NOT executed again" in results[2]["result"]
    assert "Identical call, identical result" in results[1]["result"]


def test_identical_args_changing_result_never_blocked(home, monkeypatch):
    # Stateful tools (browser_screenshot, task_list) repeat args legitimately —
    # a changing result resets the streak.
    reg, calls = _counting_registry(results=["a", "b", "c"])
    same = [("probe", {"q": "x"})]
    events, _ = _run(reg, [same, same, same, "ok"], monkeypatch)
    assert calls["n"] == 3                                  # all executed
    results = [e for e in events if e.get("type") == "tool_result"]
    assert all("NOT executed" not in r["result"] for r in results)


def test_empty_final_answer_retried_with_nudge(home, monkeypatch):
    reg, _ = _counting_registry()
    events, seen = _run(reg, [[("probe", {"q": "x"})], "", "here is the answer"],
                        monkeypatch)
    text = "".join(e.get("delta", "") for e in events if e.get("type") == "text")
    assert "here is the answer" in text
    # the retry round carried the nudge — as a user-role message, since some
    # chat templates (Qwen3.5) reject a mid-conversation system message
    nudge_msgs = [m for m in seen["payloads"][2]["messages"]
                  if m["role"] == "user" and "empty message" in m.get("content", "")]
    assert nudge_msgs


def test_empty_first_turn_without_tool_calls_retried_bounded(home, monkeypatch):
    # A completely silent FIRST turn is retried too (the 2026-08-25 chat channel
    # incident's signature B: a 0-token first call ended the run with no reply).
    # The retry budget is bounded, so an unrecoverable model still terminates.
    reg, _ = _counting_registry()
    events, seen = _run(reg, ["", "recovered"], monkeypatch)
    text = "".join(e.get("delta", "") for e in events if e.get("type") == "text")
    assert text == "recovered"
    assert seen["n"] == 2

    events, seen = _run(reg, ["", "", ""], monkeypatch)
    assert seen["n"] == 3                                   # 2 nudges, then stop
    assert any(e.get("type") == "done" for e in events)
