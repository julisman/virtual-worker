"""FC-4 + FC-5 wiring: the generic vision fallback is threaded into the two
non-chat channel run surfaces. stream_run (agent/runner.py) emits the transient
``vision_status`` event when a helper will describe, and both stream_run and the
compact route (routes/chat.py) run ``describe_message_images`` to transform the
messages before the model call. All hermetic — no network, no model, no store."""

import asyncio

import pytest

from agent import runner


class _FakeMcp:
    """Minimal async-context McpManager stand-in: no servers, empty registry."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def build_registry(self, servers):
        return {}


def _patch_common(monkeypatch):
    """Make stream_run hermetic: stub resolve_target, compose, McpManager."""
    async def fake_resolve(agent, requested=None):
        return "test-model", None
    monkeypatch.setattr("gateway.providers.resolve_target", fake_resolve)

    def fake_compose(agent, **kw):
        # (system, registry, temperature, top_p, top_k, repeat_penalty, effort)
        return ("sys", {}, 0.0, None, None, None, None)
    monkeypatch.setattr("agent.compose.compose", fake_compose)

    monkeypatch.setattr("agent.mcp_client.McpManager", _FakeMcp)


def _patch_loop(monkeypatch):
    async def fake_run(messages, model, registry, temperature, **kw):
        fake_run.seen_messages = messages
        return
        yield  # make this an async generator
    from agent import loop as loop_mod
    monkeypatch.setattr(loop_mod, "run", fake_run)
    return fake_run


def test_stream_run_emits_status_and_transforms_when_helper_describes(monkeypatch):
    _patch_common(monkeypatch)
    fake_run = _patch_loop(monkeypatch)

    sentinel = [{"role": "user", "content": "TRANSFORMED"}]

    async def fake_will(caller, model, target, messages):
        return True

    async def fake_describe(caller, model, target, messages):
        return sentinel

    from agent import vision_helper
    monkeypatch.setattr(vision_helper, "will_describe", fake_will)
    monkeypatch.setattr(vision_helper, "describe_message_images", fake_describe)

    events = []

    async def _drive():
        async for ev in runner.stream_run(
            {"id": "a1"}, [{"role": "user", "content": "orig"}],
            session_id="s1"):
            events.append(ev)
    asyncio.run(_drive())

    assert {"type": "vision_status", "state": "reading_image"} in events
    assert fake_run.seen_messages is sentinel


def test_stream_run_no_status_when_helper_declines_but_still_transforms(monkeypatch):
    _patch_common(monkeypatch)
    fake_run = _patch_loop(monkeypatch)

    passthrough = [{"role": "user", "content": "orig"}]
    called = {"describe": False}

    async def fake_will(caller, model, target, messages):
        return False

    async def fake_describe(caller, model, target, messages):
        called["describe"] = True
        return messages  # no-op passthrough

    from agent import vision_helper
    monkeypatch.setattr(vision_helper, "will_describe", fake_will)
    monkeypatch.setattr(vision_helper, "describe_message_images", fake_describe)

    events = []

    async def _drive():
        async for ev in runner.stream_run({"id": "a1"}, passthrough, session_id="s1"):
            events.append(ev)
    asyncio.run(_drive())

    assert not any(ev.get("type") == "vision_status" for ev in events)
    assert called["describe"] is True
    assert fake_run.seen_messages is passthrough


def test_compact_transforms_head_before_summarize(monkeypatch):
    from routes import chat as chat_route

    async def fake_resolve(agent, requested=None):
        return "test-model", None
    monkeypatch.setattr(chat_route, "resolve_target", fake_resolve)
    monkeypatch.setattr(chat_route.agent_store, "get_default_agent",
                        lambda: {"id": "a1"})

    order = []
    transformed = [{"role": "user", "content": "TRANSFORMED"}]

    async def fake_describe(caller, model, target, head):
        order.append("describe")
        fake_describe.seen_head = head
        return transformed

    async def fake_summarize(model, head, provider=None):
        order.append("summarize")
        fake_summarize.seen_head = head
        return "the summary"

    from agent import vision_helper
    monkeypatch.setattr(vision_helper, "describe_message_images", fake_describe)
    monkeypatch.setattr(chat_route, "summarize_messages", fake_summarize)

    # Two long messages so the head clears the len>=2 / >=1500 char gate.
    big = "x" * 1000
    req = chat_route.CompactRequest(
        messages=[chat_route.Msg(role="user", content=big),
                  chat_route.Msg(role="assistant", content=big)],
        keepRecent=0)

    out = asyncio.run(chat_route.compact(req))

    assert order == ["describe", "summarize"]
    assert fake_summarize.seen_head is transformed
    assert out["summary"] == "the summary"
