"""Template-safety invariants for the agent loop's LLM requests.

Qwen3.5's official chat template raises "System message must be at the
beginning." for a system message that isn't messages[0] — the request 400s
and the run dies with no user-visible reply (seen live 2026-07-24, run
06095600, mlx backend). So: whatever the loop appends mid-run (empty-reply
nudge, follow-through nudge) must never use the system role, and every
request must carry an explicit max_tokens (the server default — 512-ish on
mlx_lm.server — truncates thinking models mid-answer otherwise).
"""
import asyncio
import json

import pytest

import config
from agent import loop
from storage import debug_db


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(debug_db, "_schema_ready", False)
    return tmp_path


def _chunk(delta=None, finish=None):
    return {"choices": [{"delta": delta or {}, "finish_reason": finish}]}


ECHO_TOOL = {"echo": {"schema": {"type": "function", "function": {
    "name": "echo", "parameters": {"type": "object", "properties": {}}}},
    "fn": None}}


def _run(monkeypatch, replies):
    """Drive loop.run with scripted replies; capture every request payload."""
    payloads = []

    async def stream(payload, provider=None):
        payloads.append(json.loads(json.dumps(payload)))
        text = replies.pop(0)
        yield _chunk(delta={"content": text})
        yield _chunk(finish="stop")

    monkeypatch.setattr(loop.egress, "stream", stream)

    async def go():
        async for _ in loop.run([{"role": "user", "content": "check my email"}],
                                model="m", tools=dict(ECHO_TOOL), system="sys"):
            pass
    asyncio.run(go())
    return payloads


def test_nudges_never_inject_mid_conversation_system(home, monkeypatch):
    # Reply 1 announces an action but calls nothing -> the follow-through nudge
    # extends the convo; request 2 must still have system ONLY at position 0.
    payloads = _run(monkeypatch, ["let me check that for you", "done — inbox empty"])
    assert len(payloads) == 2
    roles = [m["role"] for m in payloads[1]["messages"]]
    assert roles[0] == "system"
    assert "system" not in roles[1:], roles


def test_every_request_has_explicit_max_tokens(home, monkeypatch):
    payloads = _run(monkeypatch, ["all good"])
    assert payloads and all(p.get("max_tokens") == loop.MAX_OUTPUT_TOKENS for p in payloads)
