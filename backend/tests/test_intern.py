"""Intern M1 — task store, activity store, intern tools, shell classifier."""

import asyncio

import pytest

import config
from agent import intern
from agent.shell_tool import _classify, make_shell_tool
from storage import activity_store, task_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


# --- task_store ------------------------------------------------------------- #

def test_task_description_and_criteria_roundtrip(db):
    t = task_store.create("a1", {"title": "Build it", "description": "do the thing",
                                 "criteria": "- compiles\n- tests pass"})
    assert t["description"] == "do the thing"
    assert t["criteria"] == "- compiles\n- tests pass"
    # criteria is editable; clearing it works too
    u = task_store.update("a1", t["id"], {"criteria": "- shipped"})
    assert u["criteria"] == "- shipped"
    cleared = task_store.update("a1", t["id"], {"criteria": ""})
    assert cleared["criteria"] == ""


def test_task_crud_and_lifecycle(db):
    t = task_store.create("a1", {"title": "Ship feature", "priority": "high"})
    assert t["status"] == "todo" and t["priority"] == "high" and t["startedAt"] is None
    t2 = task_store.update("a1", t["id"], {"status": "in_progress"})
    assert t2["status"] == "in_progress" and t2["startedAt"] is not None
    t3 = task_store.update("a1", t["id"], {"status": "done", "resultSummary": "did it"})
    assert t3["status"] == "done" and t3["completedAt"] is not None and t3["resultSummary"] == "did it"
    assert task_store.counts("a1") == {"done": 1}


def test_task_scoped_by_agent(db):
    task_store.create("a1", {"title": "x"})
    task_store.create("a2", {"title": "y"})
    assert len(task_store.list_for("a1")) == 1
    assert task_store.get("a1", task_store.list_for("a2")[0]["id"]) is None


def test_next_todo_priority_order(db):
    task_store.create("a1", {"title": "low", "priority": "low"})
    hi = task_store.create("a1", {"title": "urgent", "priority": "urgent"})
    assert task_store.next_todo("a1")["id"] == hi["id"]


def test_task_comments(db):
    t = task_store.create("a1", {"title": "x"})
    task_store.add_comment(t["id"], "mentor", "please do X")
    task_store.add_comment(t["id"], "intern", "on it")
    thread = task_store.list_comments(t["id"])
    assert [c["author"] for c in thread] == ["mentor", "intern"]


# --- activity_store --------------------------------------------------------- #

def test_activity_log_and_filter(db):
    activity_store.log("a1", "tool_call", "web_search: foo")
    activity_store.log("a1", "file_change", "write_file notes.txt")
    activity_store.log("a2", "note", "other agent")
    assert len(activity_store.list_for("a1")) == 2
    assert len(activity_store.list_for("a1", type="file_change")) == 1
    assert activity_store.list_for("a1")[0]["type"] == "file_change"  # recent first


# --- intern tools ----------------------------------------------------------- #

def _call(reg, name, **kw):
    return asyncio.run(reg[name]["fn"](**kw))


def test_intern_tools_drive_board(db):
    reg = intern.make_intern_tools("a1")
    assert set(reg) == {"task_list", "task_create", "task_claim", "task_update", "task_comment"}
    out = _call(reg, "task_create", title="Write README", priority="high")
    assert "created" in out
    tid = task_store.list_for("a1")[0]["id"]
    assert task_store.get("a1", tid)["origin"] == "intern"
    _call(reg, "task_claim", id=tid)
    assert task_store.get("a1", tid)["status"] == "in_progress"
    _call(reg, "task_update", id=tid, status="review", result_summary="done draft")
    assert task_store.get("a1", tid)["status"] == "review"
    # actions were journaled
    types = {a["type"] for a in activity_store.list_for("a1")}
    assert {"task_created", "task_claimed", "task_moved"} <= types


def test_board_context_lists_open_tasks(db):
    task_store.create("a1", {"title": "Open one"})
    ctx = intern.build_board_context("a1")
    assert "task board" in ctx.lower() and "Open one" in ctx


# --- shell classifier (tiered: read-only vs workspace) ---------------------- #

def test_shell_readonly_tier():
    assert _classify(["ls", "-la"], workspace=False)[0] is True
    assert _classify(["git", "status"], workspace=False)[0] is True
    assert _classify(["cat", "README.md"], workspace=False)[0] is True
    # side-effectful refused without a working folder
    assert _classify(["git", "commit", "-m", "x"], workspace=False)[0] is False


def test_shell_workspace_tier_allows_sideeffects():
    assert _classify(["git", "commit", "-m", "x"], workspace=True)[0] is True
    assert _classify(["npm", "install"], workspace=True)[0] is True
    assert _classify(["mkdir", "src"], workspace=True)[0] is True


def test_shell_denylist_blocked_in_every_tier():
    for ws in (True, False):
        assert _classify(["rm", "-rf", "/"], workspace=ws)[0] is False
        assert _classify(["sudo", "ls"], workspace=ws)[0] is False
        assert _classify(["curl", "http://x"], workspace=ws)[0] is False
        assert _classify(["git", "push"], workspace=ws)[0] is False
    assert _classify(["find", ".", "-delete"], workspace=True)[0] is False


def test_shell_interpreters_blocked_in_readonly_tier():
    # The read-only tier is a security boundary; a language interpreter there
    # would let `python3 -c "..."` defeat the whole deny-list. Refused without a
    # working folder, still available in workspace mode (permissive by design).
    for prog in (["python3", "-c", "import os"], ["node", "-e", "1"],
                 ["env"], ["awk", "BEGIN{system(\"id\")}"]):
        assert _classify(prog, workspace=False)[0] is False, prog
        assert _classify(prog, workspace=True)[0] is True, prog


def test_shell_git_dash_C_push_still_blocked():
    # `git -C . push` must not slip past the push block by misreading `.` as the
    # subcommand. Global value-taking options are skipped when finding the verb.
    assert _classify(["git", "-C", ".", "push"], workspace=True)[0] is False
    assert _classify(["git", "-c", "k=v", "push", "origin"], workspace=True)[0] is False
    # a legitimate read-only verb behind a global option still classifies right
    assert _classify(["git", "-C", "/repo", "status"], workspace=False)[0] is True


def test_shell_tool_runs_and_refuses(tmp_path):
    tool = make_shell_tool(str(tmp_path))  # working folder -> workspace tier
    out = asyncio.run(tool["fn"](command="echo hello"))
    assert "hello" in out
    assert "refused" in asyncio.run(tool["fn"](command="rm -rf x"))
