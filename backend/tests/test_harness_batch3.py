"""Harness review batch 3 — regression tests.

Covers: a failed file-trigger run re-arming the change (with a retry cap); the
worker retrying a transient model-resolution failure instead of parking the
task as blocked with a misleading message; the git alias hole in the shell
deny-list; crusher field min/max being exact over ALL rows; ambiguous teammate
prefix matches refusing instead of guessing; and edit_file refusing to corrupt
non-UTF-8 files."""

import asyncio

import pytest

import config
from agent import crusher, shell_tool, team
from storage import agent_store, task_store, trigger_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


# ─── file trigger: failed run re-arms the change, bounded retries ────────────

def _file_trigger_setup(tmp_path, monkeypatch, results):
    """A file trigger whose runs fail/succeed per the ``results`` script."""
    from agent import triggers as trig_mod

    watched = tmp_path / "watched"
    watched.mkdir()
    trig = trigger_store.create("a1", "Watch", "file", "Handle {{event}}",
                                path=str(watched))
    monkeypatch.setattr(trig_mod.agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": "p"})

    async def scripted_run(agent, prompt, session_id, history=None):
        outcome = results.pop(0)
        return ("", outcome, "run") if outcome else ("ok", None, "run")

    monkeypatch.setattr(trig_mod, "execute_run", scripted_run)
    return trig_mod.TriggerManager(), trig, watched


def test_failed_file_fire_rearms_the_change(db, tmp_path, monkeypatch):
    mgr, trig, watched = _file_trigger_setup(tmp_path, monkeypatch,
                                             results=["boom", None])
    (watched / "new.txt").write_text("x")

    async def _drive():
        await mgr._poll_file_triggers()   # fire 1: fails -> state reverted
        await mgr.join()
        await mgr._poll_file_triggers()   # same change re-detected -> retries, succeeds
        await mgr.join()
    asyncio.run(_drive())

    after = trigger_store.get(trig["id"])
    assert after["fireCount"] == 2
    assert after["lastStatus"] == "ok"    # the change was retried, not lost


def test_file_fire_gives_up_after_retry_cap(db, tmp_path, monkeypatch):
    from agent import triggers as trig_mod
    mgr, trig, watched = _file_trigger_setup(
        tmp_path, monkeypatch, results=["boom"] * (trig_mod._MAX_FILE_RETRIES + 2))
    (watched / "new.txt").write_text("x")

    async def _drive():
        for _ in range(trig_mod._MAX_FILE_RETRIES + 2):
            await mgr._poll_file_triggers()
            await mgr.join()
    asyncio.run(_drive())

    # Fired exactly _MAX_FILE_RETRIES times, then the change was consumed —
    # a permanently failing run must not retry every tick forever.
    assert trigger_store.get(trig["id"])["fireCount"] == trig_mod._MAX_FILE_RETRIES


# ─── worker: transient resolve failure retries, not "No active model" ───────

def test_worker_retries_on_resolve_exception(db, monkeypatch):
    from agent import worker as worker_mod

    a = agent_store.create_agent({"name": "W", "role": "Intern"})
    t = task_store.create(a["id"], {"title": "job", "status": "todo"})

    async def boom(agent, requested=None):
        raise RuntimeError("provider briefly down")
    monkeypatch.setattr(worker_mod, "resolve_target", boom)
    monkeypatch.setattr(worker_mod, "ERROR_BACKOFF_S", 0)

    mgr = worker_mod.WorkerManager()
    asyncio.run(mgr._work_one(agent_store.get_agent(a["id"]),
                              task_store.get(a["id"], t["id"])))

    after = task_store.get(a["id"], t["id"])
    assert after["status"] == "todo"      # back in the queue, NOT blocked
    comments = task_store.list_comments(t["id"])
    assert any("will retry" in c["text"] for c in comments)


# ─── shell: git alias definitions are refused ────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "git -c alias.p=push p",
    "git -calias.p=push p",
    "git --config-env=alias.p=PUSHVAR p",
    "git config alias.p push",
    "git config --global alias.p push",
])
def test_git_alias_definition_refused(tmp_path, cmd):
    tool = shell_tool.make_shell_tool(str(tmp_path))["fn"]
    out = asyncio.run(tool(command=cmd))
    assert "refused" in out and "alias" in out


def test_git_normal_commands_still_allowed(tmp_path):
    tool = shell_tool.make_shell_tool(str(tmp_path))["fn"]
    out = asyncio.run(tool(command="git status"))
    assert "refused" not in out


# ─── crusher: fields.*.min/max exact over ALL rows ───────────────────────────

def test_array_field_minmax_exact_beyond_sample():
    # 500 rows; the extreme value sits in the un-sampled middle (rows 100-399).
    rows = [{"i": i, "v": 10} for i in range(500)]
    rows[250]["v"] = 99999
    import json
    r = crusher.crush(json.dumps(rows), "mcp__x__y", cap=3000)
    body = json.loads(r.text.rsplit("\n<<vw:", 1)[0])
    fields = body["_vw_compressed"]["fields"]
    assert fields["v"]["max"] == 99999    # exact, as the TRUST note promises


# ─── team: ambiguous prefix refuses instead of guessing ──────────────────────

def test_ambiguous_name_prefix_is_not_guessed(db):
    caller = agent_store.create_agent({"name": "Caller"})
    agent_store.create_agent({"name": "Anabel"})
    agent_store.create_agent({"name": "Anastasia"})
    out = asyncio.run(team.make_team_tools(caller["id"])["assign_to"]["fn"](
        agent="Ana", task_title="x"))
    assert "no such agent" in out         # refused, not silently picked

    out2 = asyncio.run(team.make_team_tools(caller["id"])["assign_to"]["fn"](
        agent="Anab", task_title="x"))
    assert "assigned" in out2             # unambiguous prefix still works


# ─── fs_tools: edit_file refuses non-UTF-8 files ─────────────────────────────

def test_edit_file_refuses_non_utf8(tmp_path):
    from agent.fs_tools import make_fs_tools
    raw = b"\xff\xfe binary-ish \x00\x81 content"
    (tmp_path / "blob.bin").write_bytes(raw)
    tools = make_fs_tools(tmp_path)
    out = asyncio.run(tools["edit_file"]["fn"](path="blob.bin", old="content", new="x"))
    assert "not valid UTF-8" in out
    assert (tmp_path / "blob.bin").read_bytes() == raw   # untouched
