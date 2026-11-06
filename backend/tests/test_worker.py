"""Always-on worker (M3) — control flow with a mocked model loop."""

import asyncio

import pytest

import config
from agent import worker as worker_mod
from storage import agent_store, task_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(agent_store, "_seeded", False)
    return tmp_path


@pytest.fixture(autouse=True)
def _hermetic_verify(monkeypatch):
    """Keep the Verify pillar from making a real reviewer call.

    These tests mock the working model loop; the verify pass is a *separate*
    model call (verify.verify_task), so it must be mocked too. Otherwise, when a
    local model server happens to be running, a strict reviewer judges the
    trivial test task NEEDS_WORK and sends it back to `todo` — making the
    'agent closed it' test flaky. Default to VERIFIED so the reconcile path is
    what's under test, not the reviewer."""
    async def fake_verify(model, task, *, context="", provider=None):
        return True, "ok (test)"
    monkeypatch.setattr(worker_mod.verify, "verify_task", fake_verify)


def _intern():
    return agent_store.create_agent({"name": "Hasan", "role": "SWE Intern", "persona": "x"})


def _patch_model(monkeypatch):
    async def fake_resolve(agent, requested=None):
        return "local-test-model", None
    monkeypatch.setattr(worker_mod, "resolve_target", fake_resolve)


def _patch_loop(monkeypatch, behavior):
    """Replace loop.run with an async generator that runs ``behavior(agent_id,
    task_id)`` (which may mutate the task) and yields a done event."""
    async def fake_run(messages, model, registry, temperature, **kw):
        behavior(kw.get("agent_id"), kw.get("task_id"))
        yield {"type": "done"}
    # The run now goes through agent.runner.stream_run, which calls agent.loop.run.
    from agent import loop as loop_mod
    monkeypatch.setattr(loop_mod, "run", fake_run)


def test_worker_completes_task_when_agent_closes_it(db, monkeypatch):
    a = _intern()
    t = task_store.create(a["id"], {"title": "do it", "status": "todo"})
    _patch_model(monkeypatch)
    # The "agent" closes the task to done during its pass.
    _patch_loop(monkeypatch, lambda aid, tid: task_store.update(aid, tid, {"status": "done", "resultSummary": "done"}))

    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._work_one(agent_store.get_agent(a["id"]), t))
    assert task_store.get(a["id"], t["id"])["status"] == "done"


def test_worker_parks_task_if_not_closed(db, monkeypatch):
    a = _intern()
    t = task_store.create(a["id"], {"title": "do it", "status": "todo"})
    _patch_model(monkeypatch)
    _patch_loop(monkeypatch, lambda aid, tid: None)  # agent leaves it in_progress

    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._work_one(agent_store.get_agent(a["id"]), t))
    # Not silently stuck: parked to blocked so it won't be re-pulled.
    assert task_store.get(a["id"], t["id"])["status"] == "blocked"


def test_daily_pass_count_recovers_after_restart(db):
    # A pass logs one `task_claimed`; a fresh WorkerManager (simulating a backend
    # restart) must recover today's count from the persisted activity feed rather
    # than starting the intern's daily budget over.
    from datetime import datetime, timezone
    from storage import activity_store

    a = _intern()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for _ in range(3):  # three tasks already worked today
        activity_store.log(a["id"], "task_claimed", "picked up")

    mgr = worker_mod.WorkerManager()          # cold counter, as after a restart
    assert mgr._bump_passes(a["id"]) == 4     # seeded from 3, then this pass
    assert mgr._bump_passes(a["id"]) == 5     # in-memory from here on


def test_worker_blocks_when_no_model(db, monkeypatch):
    a = _intern()
    t = task_store.create(a["id"], {"title": "do it", "status": "todo"})

    async def no_model(agent, requested=None):
        return None, None
    monkeypatch.setattr(worker_mod, "resolve_target", no_model)
    monkeypatch.setattr(worker_mod, "ERROR_BACKOFF_S", 0)

    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._work_one(agent_store.get_agent(a["id"]), t))
    assert task_store.get(a["id"], t["id"])["status"] == "blocked"


def test_start_sets_duty_and_stop_clears(db, monkeypatch):
    a = _intern()
    _patch_model(monkeypatch)
    _patch_loop(monkeypatch, lambda aid, tid: task_store.update(aid, tid, {"status": "done"}))

    async def scenario():
        mgr = worker_mod.WorkerManager()
        await mgr.start(a["id"])
        assert mgr.is_running(a["id"])
        await asyncio.sleep(0.05)  # let it spin (no todos -> idle backoff)
        await mgr.stop(a["id"])
        assert not mgr.is_running(a["id"])
    asyncio.run(scenario())
    assert agent_store.get_agent(a["id"])["dutyStatus"] == "off"


def test_daily_cap_pauses(db, monkeypatch):
    a = _intern()
    # Cap counts task executions: with cap=1 and two todos, the 2nd pass trips it.
    agent_store.update_agent(a["id"], {"dutyStatus": "idle", "guardrails": {"dailyCap": 1, "verify": False}})
    task_store.create(a["id"], {"title": "one", "status": "todo"})
    task_store.create(a["id"], {"title": "two", "status": "todo"})
    _patch_model(monkeypatch)
    _patch_loop(monkeypatch, lambda aid, tid: task_store.update(aid, tid, {"status": "done", "resultSummary": "ok"}))
    monkeypatch.setattr(worker_mod, "IDLE_BACKOFF_S", 0)

    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._run(a["id"]))
    assert agent_store.get_agent(a["id"])["dutyStatus"] == "paused"


def test_idle_polls_do_not_consume_daily_cap(db, monkeypatch):
    a = _intern()
    # On duty with no todos: idle polling must NOT count against the daily cap.
    agent_store.update_agent(a["id"], {"dutyStatus": "idle", "guardrails": {"dailyCap": 1}})
    monkeypatch.setattr(worker_mod, "IDLE_BACKOFF_S", 0.001)
    _patch_model(monkeypatch)

    async def scenario():
        mgr = worker_mod.WorkerManager()
        run = asyncio.create_task(mgr._run(a["id"]))
        await asyncio.sleep(0.03)  # let it idle-poll many times
        agent_store.update_agent(a["id"], {"dutyStatus": "off"})
        await run
        return mgr

    mgr = asyncio.run(scenario())
    # No task ever ran, so the pass counter was never bumped and it never paused.
    assert mgr._passes.get(a["id"]) is None
    assert agent_store.get_agent(a["id"])["dutyStatus"] == "off"


def test_worker_wires_mcp_tools(db, monkeypatch):
    """A task-executing worker run must include the agent's MCP tools (compose()
    omits them) — otherwise a task that needs e.g. a Confluence MCP can't use it."""
    a = agent_store.create_agent({"name": "H", "role": "Intern", "persona": "x",
                                  "mcpServers": ["s1"]})
    t = task_store.create(a["id"], {"title": "do it", "status": "todo"})
    _patch_model(monkeypatch)

    monkeypatch.setattr("storage.mcp_store.get_server",
                        lambda i: {"slug": "srv", "enabled": True})

    class FakeMcp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def build_registry(self, servers):
            if not servers:
                return {}
            return {"mcp__srv__do": {"fn": None, "schema": {"type": "function",
                    "function": {"name": "mcp__srv__do"}}}}
    monkeypatch.setattr("agent.mcp_client.McpManager", FakeMcp)

    captured = {}

    async def fake_run(messages, model, registry, temperature, **kw):
        captured["registry"] = dict(registry)
        task_store.update(kw.get("agent_id"), kw.get("task_id"),
                          {"status": "done", "resultSummary": "ok"})
        yield {"type": "done"}
    from agent import loop as loop_mod
    monkeypatch.setattr(loop_mod, "run", fake_run)

    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._work_one(agent_store.get_agent(a["id"]), t))
    assert "mcp__srv__do" in captured["registry"]
