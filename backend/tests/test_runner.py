"""The single run entry point (agent/runner.py): stream_run composes the agent
and drives loop.run; execute_run collects it into (text, error, run_id). All run
surfaces (chat/worker/scheduler/triggers/chat channel) funnel through this."""

import asyncio

import pytest

import config
from agent import runner
from storage import agent_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(agent_store, "_seeded", False)
    return tmp_path


def _patch(monkeypatch, events):
    async def fake_resolve(agent, requested=None):
        return "test-model", None
    monkeypatch.setattr("gateway.providers.resolve_target", fake_resolve)

    async def fake_run(messages, model, registry, temperature, **kw):
        fake_run.seen = {"model": model, "session_id": kw.get("session_id"),
                         "task_id": kw.get("task_id"), "agent_id": kw.get("agent_id")}
        for ev in events:
            yield ev
    from agent import loop as loop_mod
    monkeypatch.setattr(loop_mod, "run", fake_run)
    return fake_run


def test_execute_run_collects_text_and_run_id(db, monkeypatch):
    a = agent_store.create_agent({"name": "A", "persona": "p"})
    _patch(monkeypatch, [{"type": "text", "delta": "hel"},
                         {"type": "text", "delta": "lo", "runId": "r-9"}])
    text, err, run_id = asyncio.run(
        runner.execute_run(a, "hi", session_id="s1"))
    assert text == "hello" and err is None and run_id == "r-9"


def test_execute_run_keeps_only_final_segment_after_tool_calls(db, monkeypatch):
    # Narration streamed before a tool call ("Let me read…") must NOT leak into
    # the collected reply — chat channel/trigger deliveries send this text verbatim.
    a = agent_store.create_agent({"name": "A", "persona": "p"})
    _patch(monkeypatch, [
        {"type": "text", "delta": "Let me check the config."},
        {"type": "tool_call", "id": "c1", "name": "wiki_read", "args": {}},
        {"type": "tool_result", "id": "c1", "name": "wiki_read", "result": "…"},
        {"type": "text", "delta": "Let me read the key detail."},
        {"type": "tool_call", "id": "c2", "name": "wiki_read", "args": {}},
        {"type": "tool_result", "id": "c2", "name": "wiki_read", "result": "…"},
        {"type": "text", "delta": "The answer is 42.", "runId": "r-1"},
    ])
    text, err, run_id = asyncio.run(runner.execute_run(a, "hi", session_id="s1"))
    assert text == "The answer is 42." and err is None and run_id == "r-1"


def test_execute_run_streamed_events_still_reach_on_event(db, monkeypatch):
    a = agent_store.create_agent({"name": "A", "persona": "p"})
    _patch(monkeypatch, [
        {"type": "text", "delta": "narration"},
        {"type": "tool_call", "id": "c1", "name": "grep", "args": {}},
        {"type": "tool_result", "id": "c1", "name": "grep", "result": "…"},
        {"type": "text", "delta": "final"},
    ])
    seen = []

    async def on_event(ev):
        seen.append(ev["type"])
    text, _, _ = asyncio.run(runner.execute_run_streamed(
        a, "hi", session_id="s1", on_event=on_event))
    assert text == "final" and seen == ["tool_call", "tool_result"]


def test_execute_run_surfaces_error(db, monkeypatch):
    a = agent_store.create_agent({"name": "A", "persona": "p"})
    _patch(monkeypatch, [{"type": "error", "message": "boom"}])
    text, err, _ = asyncio.run(runner.execute_run(a, "hi", session_id="s1"))
    assert err == "boom"


def test_stream_run_threads_resolved_and_task_id(db, monkeypatch):
    # The worker passes a pre-resolved target + a task_id; both must reach loop.run
    # (and the pre-resolved target means resolve_target is NOT called again).
    a = agent_store.create_agent({"name": "A", "persona": "p"})

    async def boom_resolve(agent, requested=None):
        raise AssertionError("resolve_target must not be called when resolved= is passed")
    monkeypatch.setattr("gateway.providers.resolve_target", boom_resolve)

    async def fake_run(messages, model, registry, temperature, **kw):
        fake_run.seen = {"model": model, "task_id": kw.get("task_id")}
        yield {"type": "done"}
    from agent import loop as loop_mod
    monkeypatch.setattr(loop_mod, "run", fake_run)

    async def _drive():
        async for _ in runner.stream_run(a, [{"role": "user", "content": "x"}],
                                         session_id="worker:1",
                                         task_id="t-1", resolved=("m2", None)):
            pass
    asyncio.run(_drive())
    assert fake_run.seen == {"model": "m2", "task_id": "t-1"}
