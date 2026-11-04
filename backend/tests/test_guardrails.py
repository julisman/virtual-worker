"""Per-agent guardrails — store defaults, shell modes, loop cap, worker honoring."""

import asyncio

import pytest

import config
from agent import worker as worker_mod
from agent.shell_tool import make_shell_tool
from storage import agent_store, task_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(agent_store, "_seeded", False)
    return tmp_path


def _call(tool, **kw):
    return asyncio.run(tool["fn"](**kw))


# --- store ------------------------------------------------------------------ #

def test_defaults_applied(db):
    a = agent_store.create_agent({"name": "X", "role": "Intern"})
    g = a["guardrails"]
    assert g == {"shell": "auto", "verify": True, "maxIterations": 6, "dailyCap": 60, "extraDenied": [], "channelSend": False}


def test_partial_update_merges_and_coerces(db):
    a = agent_store.create_agent({"name": "X", "role": "Intern"})
    upd = agent_store.update_agent(a["id"], {"guardrails": {"shell": "readonly", "maxIterations": 999, "verify": False}})
    g = upd["guardrails"]
    assert g["shell"] == "readonly" and g["verify"] is False
    assert g["maxIterations"] == 50        # clamped to max
    assert g["dailyCap"] == 60             # untouched key preserved


def test_bad_shell_value_falls_back(db):
    a = agent_store.create_agent({"name": "X", "guardrails": {"shell": "bogus"}})
    assert a["guardrails"]["shell"] == "auto"


# --- shell modes ------------------------------------------------------------ #

def test_shell_mode_readonly_blocks_sideeffect():
    t = make_shell_tool("/tmp", mode="readonly")
    assert "refused" in _call(t, command="git commit -m x")


def test_shell_mode_workspace_allows_sideeffect():
    t = make_shell_tool("/tmp", mode="workspace")
    assert "refused" not in _call(t, command="echo ok")


def test_extra_denied_blocks():
    t = make_shell_tool("/tmp", mode="workspace", extra_denied=["git"])
    assert "guardrails" in _call(t, command="git status")


def test_denylist_still_enforced_regardless_of_mode():
    for m in ("readonly", "workspace", "auto"):
        t = make_shell_tool("/tmp", mode=m)
        assert "refused" in _call(t, command="rm -rf /")


# --- worker honors guardrails ----------------------------------------------- #

def _stub_model_and_loop(monkeypatch, marks_done=True):
    async def fake_resolve(_):
        return "m"
    monkeypatch.setattr(worker_mod, "resolve_model", fake_resolve)

    async def fake_run(messages, model, registry, temperature, **kw):
        if marks_done:
            task_store.update(kw["agent_id"], kw["task_id"], {"status": "done"})
        yield {"type": "done"}
    monkeypatch.setattr(worker_mod.loop, "run", fake_run)


def test_worker_skips_verify_when_disabled(db, monkeypatch):
    a = agent_store.create_agent({"name": "X", "role": "Intern", "guardrails": {"verify": False}})
    t = task_store.create(a["id"], {"title": "x", "status": "todo"})
    _stub_model_and_loop(monkeypatch)
    called = []

    async def spy(self, *a, **k):
        called.append(True)
    monkeypatch.setattr(worker_mod.WorkerManager, "_verify", spy)

    asyncio.run(worker_mod.WorkerManager()._work_one(agent_store.get_agent(a["id"]), t))
    assert called == []  # verify disabled -> not invoked
    assert task_store.get(a["id"], t["id"])["status"] == "done"


def test_worker_runs_verify_when_enabled(db, monkeypatch):
    a = agent_store.create_agent({"name": "X", "role": "Intern"})  # verify defaults on
    t = task_store.create(a["id"], {"title": "x", "status": "todo"})
    _stub_model_and_loop(monkeypatch)
    called = []

    async def spy(self, *a, **k):
        called.append(True)
    monkeypatch.setattr(worker_mod.WorkerManager, "_verify", spy)

    asyncio.run(worker_mod.WorkerManager()._work_one(agent_store.get_agent(a["id"]), t))
    assert called == [True]  # verify on -> invoked on closeout
