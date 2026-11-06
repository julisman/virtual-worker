"""Cross-run delegation loop guard — ``assign_to`` persists the visited chain in
the task's metadata (``delegationChain``) and refuses a handoff to an agent
already in the chain, so an A→B→A assignment ping-pong across worker runs is
broken (the synchronous ``ask_agent`` chain guard never covered board handoffs,
which resume in a fresh run at depth 0)."""

import asyncio

import pytest

import config
from agent import team
from storage import agent_store, task_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def _call(reg, name, **kw):
    return asyncio.run(reg[name]["fn"](**kw))


def test_assign_to_persists_delegation_chain(db):
    a = agent_store.create_agent({"name": "A"})
    b = agent_store.create_agent({"name": "B"})
    _call(team.make_team_tools(a["id"]), "assign_to",
          agent=b["id"], task_title="do the thing")
    t = task_store.list_for(b["id"])[0]
    assert t["metadata"]["delegationChain"] == [a["id"], b["id"]]


def test_assign_back_to_originator_is_refused(db):
    a = agent_store.create_agent({"name": "A"})
    b = agent_store.create_agent({"name": "B"})
    # B's worker run executes a task assigned by A: the persisted chain (A, B)
    # is threaded back in by the worker.
    reg = team.make_team_tools(b["id"], chain=(a["id"], b["id"]))
    out = _call(reg, "assign_to", agent=a["id"], task_title="bounce back")
    assert "cycle" in out
    assert task_store.list_for(a["id"]) == []  # nothing landed on A's board


def test_handoff_to_third_agent_extends_chain(db):
    a = agent_store.create_agent({"name": "A"})
    b = agent_store.create_agent({"name": "B"})
    c = agent_store.create_agent({"name": "C"})
    reg = team.make_team_tools(b["id"], chain=(a["id"], b["id"]))
    out = _call(reg, "assign_to", agent=c["id"], task_title="fan out")
    assert "assigned" in out
    t = task_store.list_for(c["id"])[0]
    assert t["metadata"]["delegationChain"] == [a["id"], b["id"], c["id"]]


def test_chain_always_contains_the_caller(db):
    # A chain restored from task metadata might predate the caller — the factory
    # normalizes it so the cycle guard can never miss the executing agent itself.
    a = agent_store.create_agent({"name": "A"})
    b = agent_store.create_agent({"name": "B"})
    reg = team.make_team_tools(b["id"], chain=(a["id"],))  # caller B missing
    out = _call(reg, "assign_to", agent=b["id"], task_title="self")
    assert "refused" in out


def test_compose_threads_delegation_chain(db):
    from agent.compose import compose
    a = agent_store.create_agent({"name": "A"})
    b = agent_store.create_agent({"name": "B"})
    _, reg, *_ = compose(b, delegation_chain=(a["id"], b["id"]))
    out = _call(reg, "assign_to", agent=a["id"], task_title="bounce back")
    assert "cycle" in out
