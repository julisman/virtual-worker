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

def _stub_model(monkeypatch, target=None):
    # ask_agent resolves the teammate through resolve_target (its OWN backend),
    # so stub that — mirrors runner/worker's resolution path.
    async def fake_resolve(_agent, _requested=None):
        return "stub-model", target
    monkeypatch.setattr("gateway.providers.resolve_target", fake_resolve)


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


def test_ask_agent_runs_teammate_on_its_own_provider(db, monkeypatch):
    """A provider-backed teammate is run on ITS backend — provider=target is threaded."""
    mgr, wkr = _two_agents()
    provider_target = {"provider": "deepseek", "kind": "openai",
                       "base_url": "https://api.deepseek.com", "api_key": "k"}
    _stub_model(monkeypatch, target=provider_target)

    seen = {}

    async def fake_run(messages, model, registry, *a, **kw):
        seen["provider"] = kw.get("provider")
        seen["model"] = model
        yield {"type": "text", "delta": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(loop, "run", fake_run)

    out = _call(team.make_team_tools(mgr["id"]), "ask_agent",
                agent=wkr["id"], question="on your own backend?")
    assert out == "ok"
    assert seen["provider"] is provider_target   # routed to the teammate's provider
    assert seen["model"] == "stub-model"


def test_ask_agent_resolution_failure_returns_refusal(db, monkeypatch):
    """If resolve_target raises, ask_agent returns the friendly refusal — never raises."""
    mgr, wkr = _two_agents()

    async def boom(_agent, _requested=None):
        raise RuntimeError("provider record missing")
    monkeypatch.setattr("gateway.providers.resolve_target", boom)

    ran = {"n": 0}

    async def fake_run(*a, **kw):
        ran["n"] += 1
        yield {"type": "done"}

    monkeypatch.setattr(loop, "run", fake_run)

    out = _call(team.make_team_tools(mgr["id"]), "ask_agent",
                agent=wkr["id"], question="anything?")
    assert "could not consult" in out
    assert ran["n"] == 0  # never reached the loop


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


# --- ask_agent threads the TEAMMATE's guardrails to loop.run ---------------- #

def test_ask_agent_passes_target_guardrails(db, monkeypatch):
    """A consulted teammate runs under ITS OWN maxIterations/wrapupWarnAt — not
    loop.run's MAX_ITERATIONS fallback (the ~6-step cap bug)."""
    mgr = agent_store.create_agent({"name": "Manager", "role": "lead"})
    wkr = agent_store.create_agent(
        {"name": "Worker", "role": "intern",
         "guardrails": {"maxIterations": 42, "wrapupWarnAt": 5}})
    _stub_model(monkeypatch)

    seen = {}

    async def fake_run(messages, model, registry, *a, **kw):
        seen["max_iterations"] = kw.get("max_iterations")
        seen["wrapup_warn_at"] = kw.get("wrapup_warn_at")
        yield {"type": "text", "delta": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(loop, "run", fake_run)

    _call(team.make_team_tools(mgr["id"]), "ask_agent",
          agent=wkr["id"], question="deep dive?")
    assert seen["max_iterations"] == 42
    assert seen["wrapup_warn_at"] == 5


def test_ask_agent_passes_default_guardrails_not_none(db, monkeypatch):
    """With default guardrails the merged default (0 = unlimited, warn-at 2) is
    threaded — never None (which would trigger the 6-step fallback)."""
    mgr, wkr = _two_agents()
    _stub_model(monkeypatch)

    seen = {}

    async def fake_run(messages, model, registry, *a, **kw):
        seen["max_iterations"] = kw.get("max_iterations")
        seen["wrapup_warn_at"] = kw.get("wrapup_warn_at")
        yield {"type": "text", "delta": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(loop, "run", fake_run)

    _call(team.make_team_tools(mgr["id"]), "ask_agent",
          agent=wkr["id"], question="quick one?")
    assert seen["max_iterations"] == 0   # default: unlimited (backstopped)
    assert seen["wrapup_warn_at"] == 2   # default warn-at
    assert seen["max_iterations"] is not None and seen["wrapup_warn_at"] is not None


# --- per-agent ask budget (askBudget) --------------------------------------- #

def _ask_run(monkeypatch):
    async def fake_run(*a, **kw):
        yield {"type": "text", "delta": "ok"}
        yield {"type": "done"}
    monkeypatch.setattr(loop, "run", fake_run)


def test_ask_budget_defaults_to_three(db, monkeypatch):
    mgr, wkr = _two_agents()
    _stub_model(monkeypatch)
    _ask_run(monkeypatch)
    reg = team.make_team_tools(mgr["id"])
    for _ in range(3):
        assert "budget" not in _call(reg, "ask_agent", agent=wkr["id"], question="q")
    assert "budget (3 per run)" in _call(reg, "ask_agent", agent=wkr["id"], question="4th")


def test_ask_budget_agent_override_allows_more(db, monkeypatch):
    mgr = agent_store.create_agent({"name": "Manager", "role": "lead", "askBudget": 5})
    wkr = agent_store.create_agent({"name": "Worker", "role": "intern"})
    _stub_model(monkeypatch)
    _ask_run(monkeypatch)
    reg = team.make_team_tools(mgr["id"])
    for _ in range(5):  # the 4th and 5th now succeed
        assert "budget" not in _call(reg, "ask_agent", agent=wkr["id"], question="q")
    assert "budget (5 per run)" in _call(reg, "ask_agent", agent=wkr["id"], question="6th")


def test_ask_cap_is_the_owners_and_threads_to_nested(db, monkeypatch):
    # A nested consult built with an explicit ask_cap uses the owner's cap, not
    # the nested caller's own askBudget.
    mgr = agent_store.create_agent({"name": "Manager", "askBudget": 5})
    wkr = agent_store.create_agent({"name": "Worker", "askBudget": 1})
    _stub_model(monkeypatch)
    _ask_run(monkeypatch)
    shared = {"asks": 0, "assigns": 0}
    reg = team.make_team_tools(wkr["id"], depth=1, chain=(mgr["id"],),
                               budget=shared, ask_cap=5)
    third = agent_store.create_agent({"name": "Third"})
    for _ in range(5):
        assert "budget" not in _call(reg, "ask_agent", agent=third["id"], question="q")
    assert "budget (5 per run)" in _call(reg, "ask_agent", agent=third["id"], question="6th")
