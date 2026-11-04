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


def _intern():
    return agent_store.create_agent({"name": "Hasan", "role": "SWE Intern", "persona": "x"})


def _patch_model(monkeypatch):
    async def fake_resolve(_):
        return "local-test-model"
    monkeypatch.setattr(worker_mod, "resolve_model", fake_resolve)


def _patch_loop(monkeypatch, behavior):
    """Replace loop.run with an async generator that runs ``behavior(agent_id,
    task_id)`` (which may mutate the task) and yields a done event."""
    async def fake_run(messages, model, registry, temperature, **kw):
        behavior(kw.get("agent_id"), kw.get("task_id"))
        yield {"type": "done"}
    monkeypatch.setattr(worker_mod.loop, "run", fake_run)


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


def test_worker_blocks_when_no_model(db, monkeypatch):
    a = _intern()
    t = task_store.create(a["id"], {"title": "do it", "status": "todo"})

    async def no_model(_):
        return None
    monkeypatch.setattr(worker_mod, "resolve_model", no_model)
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
    # Cap from the agent's guardrails; no idle wait so it trips fast.
    agent_store.update_agent(a["id"], {"dutyStatus": "idle", "guardrails": {"dailyCap": 1}})
    monkeypatch.setattr(worker_mod, "IDLE_BACKOFF_S", 0)
    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._run(a["id"]))  # pass 1 idles, pass 2 exceeds cap -> pause
    assert agent_store.get_agent(a["id"])["dutyStatus"] == "paused"
