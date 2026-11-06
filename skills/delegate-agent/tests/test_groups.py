from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent  # noqa: E402
import group_lib  # noqa: E402
from agent import command_spawn, group_snapshot, parse_until, respond_group_request, supervise_group  # noqa: E402
from core_lib import Run, add_event, cleanup_runs, create_request, get_request, get_state, initialize_database, load_roles, set_state  # noqa: E402
from group_lib import add_member, cleanup_groups, create_group, iter_groups, list_group_events, list_members, wait_group_events  # noqa: E402


def make_run(root: Path, group_id: str, agent_id: str, backend: str = "codex") -> Run:
    directory = root / "tasks" / "workspace" / agent_id
    directory.mkdir(parents=True)
    metadata = {
        "agent_id": agent_id,
        "group_id": group_id,
        "backend": backend,
        "backend_source": "explicit",
        "cwd": str(root),
        "prompt": "test",
        "role": "general",
        "complexity": "standard",
        "model": "test",
        "sandbox": "read-only",
        "warnings": [],
        "worker_pid": os.getpid(),
        "status": "running",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    (directory / "metadata.json").write_text(json.dumps(metadata))
    initialize_database(directory / "mailbox.db")
    set_state(directory / "mailbox.db", "status", "running")
    add_member(root, group_id, agent_id, backend, directory)
    return Run(directory, metadata)


def test_group_membership_and_event_mirroring(tmp_path: Path) -> None:
    group = create_group(tmp_path, "mixed review")
    run = make_run(tmp_path, group["group_id"], "agent-one")
    add_event(run.database, "worker", "progress.reported", {"phase": "testing", "summary": "working", "details": {"large": "x" * 5000}})
    add_event(run.database, "codex", "item.completed", {"raw": "x" * 5000})
    members = list_members(tmp_path, group["group_id"])
    assert [member["agent_id"] for member in members] == ["agent-one"]
    events = list_group_events(tmp_path, group["group_id"])
    assert events[-1]["agent_id"] == "agent-one"
    assert events[-1]["type"] == "progress.reported"
    assert events[-1]["payload"] == {"phase": "testing", "summary": "working"}
    assert all(event["type"] != "item.completed" for event in events)
    assert wait_group_events(tmp_path, group["group_id"], events[-1]["cursor"], 0) == []


def test_coordinator_request_routes_to_originating_worker(tmp_path: Path) -> None:
    group = create_group(tmp_path)
    run = make_run(tmp_path, group["group_id"], "agent-question")
    request_id = create_request(run.database, {"question": "Proceed?"})
    snapshot = group_snapshot(tmp_path, group["group_id"])
    assert snapshot["action_required"] is True
    assert snapshot["complete"] is False
    assert snapshot["coordinator_inbox"]["messages"][0]["request_id"] == request_id
    routed_agent = respond_group_request(tmp_path, group["group_id"], request_id, "yes")
    assert routed_agent == "agent-question"
    assert get_request(run.database, request_id)["response"] == {"answer": "yes"}


def test_supervise_ignores_progress_until_action_required(tmp_path: Path) -> None:
    group = create_group(tmp_path)
    run = make_run(tmp_path, group["group_id"], "agent-later-question")

    def publish() -> None:
        time.sleep(0.1)
        add_event(run.database, "worker", "progress.reported", {"phase": "analysis", "summary": "working"})
        time.sleep(0.2)
        create_request(run.database, {"question": "Choose?"})

    thread = threading.Thread(target=publish)
    thread.start()
    result = supervise_group(tmp_path, group["group_id"], 0, 2, 100)
    thread.join()
    assert result["reason"] == "action_required"
    assert result["action_required"] is True
    assert {event["type"] for event in result["events"]} >= {"progress.reported", "input.requested"}


def test_group_completes_only_after_workers_terminal_and_inbox_drained(tmp_path: Path) -> None:
    group = create_group(tmp_path)
    first = make_run(tmp_path, group["group_id"], "codex-worker", "codex")
    second = make_run(tmp_path, group["group_id"], "claude-worker", "claude")
    set_state(first.database, "status", "completed")
    set_state(second.database, "status", "completed")
    snapshot = group_snapshot(tmp_path, group["group_id"])
    assert snapshot["complete"] is True
    assert {agent["backend"] for agent in snapshot["agents"]} == {"codex", "claude"}


def test_spawn_rolls_back_membership_when_launcher_fails(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    role = load_roles(tmp_path)["general"]
    resolution = {
        "backend": "codex", "backend_source": "explicit", "role": role,
        "complexity": "standard", "complexity_source": "role-default",
        "model_policy": "default", "model_policy_source": "role-complexity",
        "model": "test", "model_source": "explicit", "reasoning_effort": "medium",
        "reasoning_source": "role-complexity", "sandbox": "read-only",
        "sandbox_source": "explicit", "warnings": [],
    }
    monkeypatch.setattr(agent, "resolve_configuration", lambda arguments, root: resolution)
    monkeypatch.setattr(agent, "launch_worker", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launch failed")))
    arguments = SimpleNamespace(
        cwd=workspace, group=None, prompt="rollback", controller="test", backend="codex",
        role="general", complexity=None, model=None, model_policy=None, reasoning=None,
        sandbox="read-only", approval_policy="never", allow_shared_checkout=False,
    )
    try:
        command_spawn(arguments, tmp_path)
    except OSError as exc:
        assert "launch failed" in str(exc)
    else:
        raise AssertionError("spawn unexpectedly succeeded")
    assert list(iter_groups(tmp_path)) == []
    assert not any((tmp_path / "tasks").glob("*/*/metadata.json"))


def test_group_mirror_failure_is_actionable(tmp_path: Path, monkeypatch) -> None:
    group = create_group(tmp_path)
    run = make_run(tmp_path, group["group_id"], "mirror-failure")
    monkeypatch.setattr(group_lib, "mirror_group_event", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("group disk unavailable")))
    add_event(run.database, "worker", "progress.reported", {"phase": "test", "summary": "working"})
    assert "group disk unavailable" in get_state(run.database, "group_mirror_error")
    snapshot = group_snapshot(tmp_path, group["group_id"])
    assert snapshot["action_required"] is True
    assert snapshot["warnings"][0]["agent_id"] == "mirror-failure"


def test_supervise_limit_does_not_advance_past_omitted_events(tmp_path: Path) -> None:
    group = create_group(tmp_path)
    run = make_run(tmp_path, group["group_id"], "cursor-worker")
    for index in range(5):
        add_event(run.database, "worker", "progress.reported", {"phase": "test", "summary": str(index)})
    first = supervise_group(tmp_path, group["group_id"], 0, 0, 2)
    assert len(first["events"]) == 2
    assert first["truncated"] is True
    second = supervise_group(tmp_path, group["group_id"], first["next_cursor"], 0, 2)
    assert second["events"][0]["cursor"] == first["next_cursor"] + 1


def test_cleanup_removes_members_and_empty_groups(tmp_path: Path) -> None:
    group = create_group(tmp_path)
    run = make_run(tmp_path, group["group_id"], "expired-worker")
    metadata = json.loads((run.directory / "metadata.json").read_text())
    metadata["updated_at"] = "2020-01-01T00:00:00+00:00"
    (run.directory / "metadata.json").write_text(json.dumps(metadata))
    set_state(run.database, "status", "completed")
    assert cleanup_runs(tmp_path, completed_days=0) == ["expired-worker"]
    cleaned = cleanup_groups(tmp_path)
    assert cleaned["members"] == ["expired-worker"]
    assert cleaned["groups"] == [group["group_id"]]


def test_interrupted_worker_is_terminal_for_group_completion(tmp_path: Path) -> None:
    group = create_group(tmp_path)
    run = make_run(tmp_path, group["group_id"], "crashed-worker")
    metadata = json.loads((run.directory / "metadata.json").read_text())
    metadata["worker_pid"] = 99999999
    (run.directory / "metadata.json").write_text(json.dumps(metadata))
    snapshot = group_snapshot(tmp_path, group["group_id"])
    assert snapshot["agents"][0]["status"] == "interrupted"
    assert snapshot["complete"] is True
    supervised = supervise_group(tmp_path, group["group_id"], 0, 10, 100)
    assert supervised["reason"] == "worker_failed"
    assert supervised["failed_agents"][0]["agent_id"] == "crashed-worker"


def test_follow_waits_without_timeout_until_question(tmp_path: Path) -> None:
    group = create_group(tmp_path)
    run = make_run(tmp_path, group["group_id"], "follow-worker")

    def publish() -> None:
        time.sleep(0.1)
        add_event(run.database, "worker", "progress.reported", {"phase": "test", "summary": "still working"})
        time.sleep(0.2)
        create_request(run.database, {"question": "Need input"})

    thread = threading.Thread(target=publish)
    thread.start()
    result = supervise_group(tmp_path, group["group_id"], 0, None, 100, parse_until("action,failed,complete"))
    thread.join()
    assert result["reason"] == "action_required"
    assert result["timed_out"] is False


def test_parse_until_rejects_unknown_condition() -> None:
    try:
        parse_until("action,banana")
    except ValueError as exc:
        assert "banana" in str(exc)
    else:
        raise AssertionError("unknown condition unexpectedly accepted")
