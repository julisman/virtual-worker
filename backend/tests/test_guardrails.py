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
    assert g == {"shell": "auto", "verify": True, "maxIterations": 0, "wrapupWarnAt": 2,
                 "dailyCap": 60, "extraDenied": [], "allowedCommands": [], "tightContext": False,
                 "shellTimeoutS": 60, "contextChars": 0, "autoCompact": False, "compactAt": 80,
                 "mcpToolResultChars": 40000, "wikiReadChars": 8000, "skillFactory": False,
                 "maxOutputTokens": 0,
                 "contextMode": "chars", "reserveTokens": 0, "contextTokens": 0,
                 "compactKeep": "messages", "compactKeepAmount": 0, "channelSend": False}


def test_partial_update_merges_and_coerces(db):
    a = agent_store.create_agent({"name": "X", "role": "Intern"})
    upd = agent_store.update_agent(a["id"], {"guardrails": {"shell": "readonly", "maxIterations": 9999, "verify": False}})
    g = upd["guardrails"]
    assert g["shell"] == "readonly" and g["verify"] is False
    assert g["maxIterations"] == 1000      # clamped to max
    assert g["dailyCap"] == 60             # untouched key preserved


def test_max_iterations_zero_means_unlimited(db):
    # 0 (the default) = unlimited; explicit numbers stick; garbage/negative → 0.
    a = agent_store.create_agent({"name": "X", "guardrails": {"maxIterations": 0}})
    assert a["guardrails"]["maxIterations"] == 0
    a = agent_store.create_agent({"name": "Y", "guardrails": {"maxIterations": 25}})
    assert a["guardrails"]["maxIterations"] == 25
    a = agent_store.create_agent({"name": "Z", "guardrails": {"maxIterations": -3}})
    assert a["guardrails"]["maxIterations"] == 0
    a = agent_store.create_agent({"name": "W", "guardrails": {"maxIterations": "abc"}})
    assert a["guardrails"]["maxIterations"] == 0
    # legacy agents that stored an explicit cap keep it
    a = agent_store.create_agent({"name": "V", "guardrails": {"maxIterations": 6}})
    assert a["guardrails"]["maxIterations"] == 6


def test_bad_shell_value_falls_back(db):
    a = agent_store.create_agent({"name": "X", "guardrails": {"shell": "bogus"}})
    assert a["guardrails"]["shell"] == "auto"


def test_mcp_tool_result_chars_clamped(db):
    a = agent_store.create_agent({"name": "X", "guardrails": {"mcpToolResultChars": 9_999_999}})
    assert a["guardrails"]["mcpToolResultChars"] == 500_000   # clamped to max
    b = agent_store.create_agent({"name": "Y", "guardrails": {"mcpToolResultChars": 10}})
    assert b["guardrails"]["mcpToolResultChars"] == 2_000     # clamped to min


def test_wiki_read_chars_default_and_clamped(db):
    a = agent_store.create_agent({"name": "X"})
    assert a["guardrails"]["wikiReadChars"] == 8_000          # default
    b = agent_store.create_agent({"name": "Y", "guardrails": {"wikiReadChars": 9_999_999}})
    assert b["guardrails"]["wikiReadChars"] == 200_000        # clamped to max
    c = agent_store.create_agent({"name": "Z", "guardrails": {"wikiReadChars": 10}})
    assert c["guardrails"]["wikiReadChars"] == 1_000          # clamped to min


def test_context_chars_default_and_clamped(db):
    # 0 = auto (default); positive clamps to [8000, 2M]; garbage/negative → 0.
    a = agent_store.create_agent({"name": "A"})
    assert a["guardrails"]["contextChars"] == 0
    b = agent_store.create_agent({"name": "B", "guardrails": {"contextChars": 500}})
    assert b["guardrails"]["contextChars"] == 8_000          # clamped to min
    c = agent_store.create_agent({"name": "C", "guardrails": {"contextChars": 9_999_999}})
    assert c["guardrails"]["contextChars"] == 2_000_000      # clamped to max
    d = agent_store.create_agent({"name": "D", "guardrails": {"contextChars": -5}})
    assert d["guardrails"]["contextChars"] == 0              # negative → auto
    e = agent_store.create_agent({"name": "E", "guardrails": {"contextChars": "nope"}})
    assert e["guardrails"]["contextChars"] == 0              # garbage → auto


def test_compact_at_default_and_clamped(db):
    a = agent_store.create_agent({"name": "A"})
    assert a["guardrails"]["compactAt"] == 80                # default
    b = agent_store.create_agent({"name": "B", "guardrails": {"compactAt": "x"}})
    assert b["guardrails"]["compactAt"] == 80                # garbage → default
    c = agent_store.create_agent({"name": "C", "guardrails": {"compactAt": 5}})
    assert c["guardrails"]["compactAt"] == 30                # clamped to min
    d = agent_store.create_agent({"name": "D", "guardrails": {"compactAt": 150}})
    assert d["guardrails"]["compactAt"] == 95                # clamped to max


def test_shell_timeout_default_and_clamped(db):
    a = agent_store.create_agent({"name": "A"})
    assert a["guardrails"]["shellTimeoutS"] == 60             # default
    b = agent_store.create_agent({"name": "B", "guardrails": {"shellTimeoutS": "x"}})
    assert b["guardrails"]["shellTimeoutS"] == 60             # garbage → default
    c = agent_store.create_agent({"name": "C", "guardrails": {"shellTimeoutS": 3}})
    assert c["guardrails"]["shellTimeoutS"] == 10             # clamped to min
    d = agent_store.create_agent({"name": "D", "guardrails": {"shellTimeoutS": 9999}})
    assert d["guardrails"]["shellTimeoutS"] == 600            # clamped to max


def test_auto_compact_truthiness(db):
    a = agent_store.create_agent({"name": "A"})
    assert a["guardrails"]["autoCompact"] is False           # default
    b = agent_store.create_agent({"name": "B", "guardrails": {"autoCompact": 1}})
    assert b["guardrails"]["autoCompact"] is True
    c = agent_store.create_agent({"name": "C", "guardrails": {"autoCompact": ""}})
    assert c["guardrails"]["autoCompact"] is False


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


def test_allowed_command_bypasses_denylist():
    # `curl` is on the built-in deny-list; the operator allow-list overrides it.
    blocked = make_shell_tool("/tmp", mode="workspace")
    assert "refused" in _call(blocked, command="curl --help")
    allowed = make_shell_tool("/tmp", mode="workspace", allowed_commands=["curl"])
    assert "refused" not in _call(allowed, command="curl --help")


def test_allowed_command_runs_in_readonly_tier():
    # A side-effectful subcommand normally needs workspace mode; the allow-list
    # lets the permitted program run even in the read-only tier.
    blocked = make_shell_tool(None, mode="readonly")
    assert "refused" in _call(blocked, command="git commit -m x")
    t = make_shell_tool(None, mode="readonly", allowed_commands=["git"])
    assert "refused" not in _call(t, command="git commit -m x")


def test_extra_denied_wins_over_allowed():
    # A command in both lists stays blocked — a tightening can't be undone.
    t = make_shell_tool("/tmp", mode="workspace",
                        extra_denied=["curl"], allowed_commands=["curl"])
    assert "refused" in _call(t, command="curl --help")


def test_allowed_commands_normalized_and_stored(db):
    a = agent_store.create_agent(
        {"name": "X", "role": "Intern", "guardrails": {"allowedCommands": [" curl ", "", "docker"]}})
    assert a["guardrails"]["allowedCommands"] == ["curl", "docker"]


# --- worker honors guardrails ----------------------------------------------- #

def _stub_model_and_loop(monkeypatch, marks_done=True):
    async def fake_resolve(agent, requested=None):
        return "m", None
    monkeypatch.setattr(worker_mod, "resolve_target", fake_resolve)

    async def fake_run(messages, model, registry, temperature, **kw):
        if marks_done:
            task_store.update(kw["agent_id"], kw["task_id"], {"status": "done"})
        yield {"type": "done"}
    # Runs go through agent.runner.stream_run -> agent.loop.run now.
    from agent import loop as loop_mod
    monkeypatch.setattr(loop_mod, "run", fake_run)


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
