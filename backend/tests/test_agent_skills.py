"""Skill context (progressive disclosure) + agent-driven loop e2e (fake upstream)."""

import asyncio
import json

import pytest

import io
import zipfile

import config
import storage.skill_store as ss
from agent import loop
from agent.skills import (
    build_skill_context,
    make_load_skill_resource_tool,
    make_load_skill_tool,
)

SAMPLE = """---
name: Refund Handler
description: Process customer refunds.
when_to_use: When a customer asks for a refund.
version: 2.0.0
---
# Refund steps
Always verify the order first.
"""


@pytest.fixture()
def skills(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    ss.save_markdown("refund.md", SAMPLE.encode())
    return ss


@pytest.fixture()
def skill_with_resource(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("refund/SKILL.md", SAMPLE)
        zf.writestr("refund/references/policy.txt", "Refunds within 14 days.")
    ss.save_zip("refund.zip", buf.getvalue())
    return ss


def test_build_skill_context_lists_descriptions_only(skills):
    cat = build_skill_context(["refund-handler"])
    assert "Refund Handler" in cat
    assert "Process customer refunds" in cat
    assert "load_skill" in cat
    assert "Always verify the order first" not in cat  # body NOT in catalog


def test_build_skill_context_empty():
    assert build_skill_context([]) == ""


def test_load_skill_tool_returns_body(skills):
    tool = make_load_skill_tool(["refund-handler"])
    out = asyncio.run(tool["fn"](slug="Refund Handler"))  # by display name
    assert "Always verify the order first" in out
    assert "v2.0.0" in out  # version shown in the L2 header
    out2 = asyncio.run(tool["fn"](slug="refund-handler"))  # by slug
    assert "Always verify the order first" in out2
    miss = asyncio.run(tool["fn"](slug="nope"))
    assert "unknown skill" in miss


def test_load_skill_lists_resources(skill_with_resource):
    tool = make_load_skill_tool(["refund-handler"])
    out = asyncio.run(tool["fn"](slug="refund-handler"))
    assert "## Resources" in out
    assert "references/policy.txt" in out


def test_load_skill_resource_tool(skill_with_resource):
    tool = make_load_skill_resource_tool(["refund-handler"])
    out = asyncio.run(tool["fn"](slug="refund-handler", path="references/policy.txt"))
    assert "Refunds within 14 days." in out
    # traversal / missing rejected
    bad = asyncio.run(tool["fn"](slug="refund-handler", path="../../etc/passwd"))
    assert "not found" in bad
    miss = asyncio.run(tool["fn"](slug="nope", path="x"))
    assert "unknown skill" in miss


# --------------------------------------------------------------------------- #
# e2e: agent with persona + skill -> model calls load_skill -> final answer
# --------------------------------------------------------------------------- #

def _chunk(delta=None, finish=None, usage=None):
    ch = {"choices": [{"delta": delta or {}, "finish_reason": finish}]}
    if usage:
        ch["usage"] = usage
    return ch


def _fake_upstream_factory():
    """Two scripted turns: 1) emit a load_skill tool call, 2) emit the answer."""
    calls = {"n": 0}

    async def fake_stream(payload):
        turn = calls["n"]
        calls["n"] += 1
        if turn == 0:
            yield _chunk(delta={"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "load_skill", "arguments": json.dumps({"name": "refund-handler"})},
            }]})
            yield _chunk(finish="tool_calls", usage={"completion_tokens": 3})
        else:
            yield _chunk(delta={"content": "Refund issued."})
            yield _chunk(finish="stop", usage={"completion_tokens": 5})

    return fake_stream, calls


def test_agent_loop_loads_skill_then_answers(skills, monkeypatch):
    monkeypatch.setattr(loop, "chat_completion_stream", _fake_upstream_factory()[0])

    registry = {"load_skill": make_load_skill_tool(["refund-handler"])}
    system = "You are a support agent.\n\n" + build_skill_context(["refund-handler"])

    async def collect():
        events = []
        async for ev in loop.run(
            [{"role": "user", "content": "I want a refund"}],
            model="local", tools=registry, system=system, session_id="test:e2e",
        ):
            events.append(ev)
        return events

    events = asyncio.run(collect())
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "done"
    # the load_skill result carried the body back into the loop
    tr = next(e for e in events if e["type"] == "tool_result")
    assert "verify the order" in tr["result"].lower()
    text = "".join(e["delta"] for e in events if e["type"] == "text")
    assert "Refund issued." in text


def test_registry_from_names_filters_unknown():
    reg = loop.registry_from_names(["web_search", "nope"])
    assert set(reg) == {"web_search"}


def test_sampling_params_reach_payload(monkeypatch):
    captured = {}

    async def fake_stream(payload):
        captured.update(payload)
        yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
               "usage": {"completion_tokens": 1}}

    monkeypatch.setattr(loop, "chat_completion_stream", fake_stream)

    async def run():
        async for _ in loop.run([{"role": "user", "content": "hi"}], model="m",
                                temperature=1.0, top_p=0.95, top_k=64):
            pass
    asyncio.run(run())
    assert captured["temperature"] == 1.0
    assert captured["top_p"] == 0.95
    assert captured["top_k"] == 64


def test_sampling_params_omitted_when_none(monkeypatch):
    captured = {}

    async def fake_stream(payload):
        captured.update(payload)
        yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(loop, "chat_completion_stream", fake_stream)

    async def run():
        async for _ in loop.run([{"role": "user", "content": "hi"}], model="m"):
            pass
    asyncio.run(run())
    assert "top_p" not in captured
    assert "top_k" not in captured
