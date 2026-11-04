"""PRD-02 — team delegation tools: assign_to, ask_agent, team_list + loop guards."""

import asyncio

import pytest

import config
from agent import loop, team
from storage import activity_store, agent_store, task_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def _call(reg, name, **kw):
    return asyncio.run(reg[name]["fn"](**kw))


def _two_agents():
    mgr = agent_store.create_agent({"name": "Manager", "role": "lead"})
    wkr = agent_store.create_agent({"name": "Worker", "role": "intern"})
    return mgr, wkr


# --- team_list -------------------------------------------------------------- #

def test_team_list_excludes_self(db):
    mgr, wkr = _two_agents()
    out = _call(team.make_team_tools(mgr["id"]), "team_list")
    assert wkr["name"] in out and wkr["id"] in out
    assert mgr["id"] not in out  # the caller never lists itself


# --- assign_to -------------------------------------------------------------- #

def test_assign_to_creates_task_on_target_board_with_origin(db):
    mgr, wkr = _two_agents()
    out = _call(team.make_team_tools(mgr["id"]), "assign_to",
                agent=wkr["id"], task_title="Draft the report", task_description="2 pages",
                priority="high")
    assert "assigned" in out

    # The task lands on the TARGET's board (not the caller's), as a fresh todo.
    assert task_store.list_for(mgr["id"]) == []
    tasks = task_store.list_for(wkr["id"])
    assert len(tasks) == 1
    t = tasks[0]
    assert t["status"] == "todo" and t["priority"] == "high"
    assert t["origin"] == "agent"                       # came from another agent
    assert t["metadata"]["assignedBy"] == mgr["id"]     # origin agent recorded
    assert t["metadata"]["assignedByName"] == "Manager"

    # Both feeds journal the handoff (observability on both sides).
    mgr_deleg = activity_store.list_for(mgr["id"], type="delegation")
    wkr_deleg = activity_store.list_for(wkr["id"], type="delegation")
    assert len(mgr_deleg) == 1 and mgr_deleg[0]["detail"]["to"] == wkr["id"]
    assert len(wkr_deleg) == 1 and wkr_deleg[0]["detail"]["from"] == mgr["id"]
    assert wkr_deleg[0]["taskId"] == t["id"]


def test_assign_to_rejects_self_and_unknown(db):
    mgr, _ = _two_agents()
    reg = team.make_team_tools(mgr["id"])
    assert "cannot assign a task to yourself" in _call(reg, "assign_to", agent=mgr["id"], task_title="x")
    assert "no such agent" in _call(reg, "assign_to", agent="ghost", task_title="x")


def test_assign_to_resolves_by_name(db):
    mgr, wkr = _two_agents()
    _call(team.make_team_tools(mgr["id"]), "assign_to", agent="Worker", task_title="By name")
    assert task_store.list_for(wkr["id"])[0]["title"] == "By name"


# --- ask_agent (synchronous one-shot; model stubbed) ------------------------ #

def _stub_model(monkeypatch):
    async def fake_resolve(_):
        return "stub-model"
    monkeypatch.setattr("gateway.models.resolve_model", fake_resolve)


def test_ask_agent_runs_target_and_returns_text(db, monkeypatch):
    mgr, wkr = _two_agents()
    _stub_model(monkeypatch)

    seen = {}

    async def fake_run(messages, model, registry, *a, **kw):
        seen["agent_id"] = kw.get("agent_id")
        seen["question"] = messages[0]["content"]
        yield {"type": "text", "delta": "42 is the "}
        yield {"type": "text", "delta": "answer"}
        yield {"type": "done"}

    monkeypatch.setattr(loop, "run", fake_run)

    out = _call(team.make_team_tools(mgr["id"]), "ask_agent",
                agent=wkr["id"], question="what is the answer?")
    assert out == "42 is the answer"
    assert seen["agent_id"] == wkr["id"]               # the TARGET ran, in its own context
    assert seen["question"] == "what is the answer?"

    # Both feeds record the consult.
    assert activity_store.list_for(mgr["id"], type="delegation")  # asked
    assert activity_store.list_for(wkr["id"], type="delegation")  # answered


def test_ask_agent_refuses_at_depth_cap(db, monkeypatch):
    """A 3rd-level delegation (a tool built at the max depth) is refused — no run."""
    mgr, wkr = _two_agents()
    _stub_model(monkeypatch)

    ran = {"n": 0}

    async def fake_run(*a, **kw):
        ran["n"] += 1
        yield {"type": "done"}

    monkeypatch.setattr(loop, "run", fake_run)

    reg = team.make_team_tools(mgr["id"], depth=team.MAX_DELEGATION_DEPTH)
    out = _call(reg, "ask_agent", agent=wkr["id"], question="anything?")
    assert "depth limit" in out
    assert ran["n"] == 0  # the target was never run


def test_ask_agent_refuses_cycle(db, monkeypatch):
    mgr, wkr = _two_agents()
    _stub_model(monkeypatch)
    # Pretend we're already mid-chain having come *from* the worker.
    reg = team.make_team_tools(mgr["id"], depth=1, chain=(wkr["id"], mgr["id"]))
    out = _call(reg, "ask_agent", agent=wkr["id"], question="loop?")
    assert "cycle detected" in out


def test_ask_agent_budget_exhausts(db, monkeypatch):
    mgr, wkr = _two_agents()
    _stub_model(monkeypatch)

    async def fake_run(*a, **kw):
        yield {"type": "text", "delta": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(loop, "run", fake_run)
    reg = team.make_team_tools(mgr["id"])  # shared budget across calls
    for _ in range(team.MAX_ASKS_PER_RUN):
        assert "budget" not in _call(reg, "ask_agent", agent=wkr["id"], question="q")
    assert "budget" in _call(reg, "ask_agent", agent=wkr["id"], question="one too many")
