#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from backends import get_backend, probe_backends
from backends.base import ResolutionRequest
from core_lib import (
    ACTIVE_STATUSES,
    Run,
    acquire_workspace_lock,
    cleanup_runs,
    create_agent_id,
    current_status,
    enqueue_message,
    ensure_private_dir,
    find_run,
    get_state,
    initialize_database,
    iter_runs,
    list_events,
    load_roles,
    process_alive,
    read_json,
    release_workspace_lock,
    respond_request,
    set_state,
    state_root,
    task_directory,
    terminate_process_group,
    update_metadata,
    utc_now,
    worker_main,
    workspace_identity,
    atomic_write_json,
)
from group_lib import (
    TERMINAL_STATUSES,
    add_member,
    cleanup_groups,
    create_group,
    iter_groups,
    list_group_events,
    list_members,
    read_group,
    remove_group_if_empty,
    remove_member,
    wait_group_events,
)


CONTROL_PROMPT = """
You are a child coding agent controlled by a parent through the delegate_parent MCP server.

Coordination rules:
- Call delegate_parent.check_inbox after understanding the task.
- Call it before your first file modification, after meaningful implementation stages, before expensive commands, and before completion.
- Acknowledge instructions after incorporating them.
- Report concise milestones with delegate_parent.report_progress.
- Use delegate_parent.request_input only when a decision cannot be safely inferred. If blocking, wait with delegate_parent.wait_for_response.
- Before your final response, call delegate_parent.check_inbox once more and then delegate_parent.complete with a structured result.
- Do not continuously poll. Check only at meaningful checkpoints unless waiting for a response.
""".strip()


def emit(payload: Any) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="delegate-agent", description="Spawn and steer durable CLI coding agents")
    root.add_argument("--home", type=Path, help="Override state root (or use DELEGATE_AGENT_HOME)")
    subcommands = root.add_subparsers(dest="command", required=True)

    spawn = subcommands.add_parser("spawn")
    spawn.add_argument("--backend", default=None)
    spawn.add_argument("--group")
    spawn.add_argument("--prompt", required=True)
    spawn.add_argument("--cwd", type=Path, default=Path.cwd())
    spawn.add_argument("--role", default="general")
    spawn.add_argument("--complexity", choices=["simple", "standard", "complex"])
    spawn.add_argument("--model")
    spawn.add_argument("--model-policy", choices=["economy", "default", "capable"])
    spawn.add_argument("--reasoning")
    spawn.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    spawn.add_argument("--approval-policy", choices=["untrusted", "on-request", "never"], default="never")
    spawn.add_argument("--controller", default=os.environ.get("DELEGATE_AGENT_CONTROLLER", "unknown"))
    spawn.add_argument("--allow-shared-checkout", action="store_true")

    for name in ("status", "result", "cancel"):
        command = subcommands.add_parser(name)
        command.add_argument("agent_id")

    events = subcommands.add_parser("events")
    events.add_argument("agent_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--wait", type=float, default=0)

    steer = subcommands.add_parser("steer")
    steer.add_argument("agent_id")
    steer.add_argument("instruction")
    steer.add_argument("--priority", choices=["normal", "urgent"], default="normal")
    steer.add_argument("--interrupt", action="store_true")

    resume = subcommands.add_parser("resume")
    resume.add_argument("agent_id")
    resume.add_argument("--prompt", required=True)
    resume.add_argument("--model")
    resume.add_argument("--reasoning")

    respond = subcommands.add_parser("respond")
    respond.add_argument("agent_id")
    respond.add_argument("request_id")
    respond.add_argument("answer")

    list_command = subcommands.add_parser("list")
    list_command.add_argument("--active", action="store_true")
    list_command.add_argument("--workspace", type=Path)

    wait = subcommands.add_parser("wait")
    wait.add_argument("agent_id")
    wait.add_argument("--timeout", type=float, default=30)

    cleanup = subcommands.add_parser("cleanup")
    cleanup.add_argument("--completed-days", type=int, default=3)
    cleanup.add_argument("--failed-days", type=int, default=7)

    group = subcommands.add_parser("group")
    group_commands = group.add_subparsers(dest="group_command", required=True)
    group_create = group_commands.add_parser("create")
    group_create.add_argument("--name")
    group_create.add_argument("--coordinator", default="parent")
    group_commands.add_parser("list")
    group_status = group_commands.add_parser("status")
    group_status.add_argument("group_id")
    group_respond = group_commands.add_parser("respond")
    group_respond.add_argument("group_id")
    group_respond.add_argument("request_id")
    group_respond.add_argument("answer")

    supervise = subcommands.add_parser("supervise")
    supervise.add_argument("group_id")
    supervise.add_argument("--after", type=int, default=0)
    supervise.add_argument("--limit", type=int, default=200)
    supervise.add_argument("--timeout", type=float, default=30)
    supervise.add_argument("--until", default="action,failed,warning,complete", help="Comma-separated: action,failed,warning,complete")
    supervise.add_argument("--follow", action="store_true", help="Wait without a timeout until a selected condition occurs")

    subcommands.add_parser("doctor")
    subcommands.add_parser("backends")
    backend = subcommands.add_parser("backend")
    backend.add_argument("backend_id")
    models = subcommands.add_parser("models")
    models.add_argument("--backend", default="codex")
    models.add_argument("--include-hidden", action="store_true")
    capabilities = subcommands.add_parser("capabilities")
    capabilities.add_argument("--backend", default="codex")
    capabilities.add_argument("--include-hidden", action="store_true")
    subcommands.add_parser("roles")
    role = subcommands.add_parser("role")
    role.add_argument("role_id")
    resolve = subcommands.add_parser("resolve")
    resolve.add_argument("--backend", default=None)
    resolve.add_argument("--role", default="general")
    resolve.add_argument("--complexity", choices=["simple", "standard", "complex"])
    resolve.add_argument("--model")
    resolve.add_argument("--model-policy", choices=["economy", "default", "capable"])
    resolve.add_argument("--reasoning")
    resolve.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])

    worker = subcommands.add_parser("_worker")
    worker.add_argument("run_directory", type=Path)
    worker.add_argument("--prompt-file", required=True, type=Path)
    worker.add_argument("--resume-session-id")
    return root


def resolved_root(arguments: argparse.Namespace) -> Path:
    root = arguments.home.expanduser().resolve() if arguments.home else state_root()
    ensure_private_dir(root)
    return root


def launch_worker(run: Run, prompt: str, resume_session_id: str | None = None) -> int:
    prompt_path = run.directory / f"prompt-{int(time.time() * 1000)}.txt"
    prompt_path.write_text(prompt + "\n")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--home",
        str(run.directory.parents[2]),
        "_worker",
        str(run.directory),
        "--prompt-file",
        str(prompt_path),
    ]
    if resume_session_id:
        command.extend(["--resume-session-id", resume_session_id])
    stdout = (run.directory / "runner.log").open("a")
    stderr = (run.directory / "runner-error.log").open("a")
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "cwd": read_json(run.directory / "metadata.json")["cwd"],
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    stdout.close()
    stderr.close()
    update_metadata(run, worker_pid=process.pid, status="starting")
    set_state(run.database, "status", "starting")
    return process.pid


def resolve_configuration(arguments: argparse.Namespace, root: Path) -> dict[str, Any]:
    backend_id = arguments.backend or os.environ.get("DELEGATE_AGENT_BACKEND")
    if not backend_id:
        usable = [item["id"] for item in probe_backends() if item["usable"]]
        if len(usable) == 1:
            backend_id = usable[0]
        else:
            raise ValueError("backend is ambiguous; pass --backend or set DELEGATE_AGENT_BACKEND")
    backend = get_backend(backend_id)
    resolution = backend.resolve(ResolutionRequest(
        roles=load_roles(root),
        role_id=arguments.role,
        complexity=arguments.complexity,
        model=arguments.model,
        model_policy=arguments.model_policy,
        reasoning=arguments.reasoning,
        sandbox=arguments.sandbox,
    ))
    resolution["backend_source"] = "explicit" if arguments.backend else ("environment" if os.environ.get("DELEGATE_AGENT_BACKEND") else "single-usable")
    return resolution


def command_spawn(arguments: argparse.Namespace, root: Path) -> None:
    cwd = arguments.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"not a directory: {cwd}")
    resolution = resolve_configuration(arguments, root)
    agent_id = create_agent_id()
    directory = task_directory(root, cwd, arguments.prompt, agent_id)
    workspace_id, canonical_cwd = workspace_identity(cwd)
    if resolution["sandbox"] == "danger-full-access" and os.environ.get("DELEGATE_AGENT_ALLOW_DANGER") != "1":
        shutil.rmtree(directory)
        raise ValueError("danger-full-access requires DELEGATE_AGENT_ALLOW_DANGER=1")
    auto_group = not arguments.group
    group = read_group(root, arguments.group) if arguments.group else create_group(root, name=arguments.prompt[:80], coordinator=arguments.controller)
    metadata = {
        "agent_id": agent_id,
        "group_id": group["group_id"],
        "backend": resolution["backend"],
        "backend_source": resolution["backend_source"],
        "workspace_id": workspace_id,
        "cwd": canonical_cwd,
        "prompt": arguments.prompt,
        "role": resolution["role"]["id"],
        "complexity": resolution["complexity"],
        "complexity_source": resolution["complexity_source"],
        "model_policy": resolution["model_policy"],
        "model_policy_source": resolution["model_policy_source"],
        "model": resolution["model"],
        "model_source": resolution["model_source"],
        "reasoning_effort": resolution["reasoning_effort"],
        "reasoning_source": resolution["reasoning_source"],
        "sandbox": resolution["sandbox"],
        "sandbox_source": resolution["sandbox_source"],
        "warnings": resolution["warnings"],
        "approval_policy": arguments.approval_policy,
        "controller": arguments.controller,
        "workspace_lock": None,
        "status": "created",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    workspace_lock = None
    try:
        atomic_write_json(directory / "metadata.json", metadata)
        initialize_database(directory / "mailbox.db")
        if resolution["sandbox"] != "read-only" and not arguments.allow_shared_checkout:
            workspace_lock = acquire_workspace_lock(root, workspace_id, agent_id, directory)
            metadata["workspace_lock"] = str(workspace_lock)
            atomic_write_json(directory / "metadata.json", metadata)
    except Exception:
        release_workspace_lock(workspace_lock, agent_id)
        shutil.rmtree(directory, ignore_errors=True)
        if auto_group:
            remove_group_if_empty(root, group["group_id"])
        raise
    run = Run(directory, metadata)
    role_instructions = "\n".join(f"- {instruction}" for instruction in resolution["role"]["instructions"])
    prompt = (
        f"{CONTROL_PROMPT}\n\n"
        f"Assigned role: {resolution['role']['id']}\n"
        f"Task complexity: {resolution['complexity']}\n"
        f"Role instructions:\n{role_instructions}\n\n"
        f"Parent task:\n{arguments.prompt}"
    )
    member_added = False
    try:
        add_member(root, group["group_id"], agent_id, resolution["backend"], directory)
        member_added = True
        worker_pid = launch_worker(run, prompt)
    except Exception:
        if member_added:
            remove_member(root, group["group_id"], agent_id)
        release_workspace_lock(Path(metadata["workspace_lock"]) if metadata.get("workspace_lock") else None, agent_id)
        shutil.rmtree(directory, ignore_errors=True)
        if auto_group:
            remove_group_if_empty(root, group["group_id"])
        raise
    emit(
        {
            "agent_id": agent_id,
            "group_id": group["group_id"],
            "backend": resolution["backend"],
            "backend_source": resolution["backend_source"],
            "status": "starting",
            "worker_pid": worker_pid,
            "task_directory": str(directory),
            "role": resolution["role"]["id"],
            "complexity": resolution["complexity"],
            "model": resolution["model"],
            "model_source": resolution["model_source"],
            "model_policy": resolution["model_policy"],
            "reasoning_effort": resolution["reasoning_effort"],
            "reasoning_source": resolution["reasoning_source"],
            "sandbox": resolution["sandbox"],
            "warnings": resolution["warnings"],
        }
    )


def group_snapshot(root: Path, group_id: str) -> dict[str, Any]:
    metadata = read_group(root, group_id)
    agents: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    failed_agents: list[dict[str, Any]] = []
    for member in list_members(root, group_id):
        run_directory = Path(member["task_directory"])
        if not (run_directory / "metadata.json").exists():
            continue
        run = Run(run_directory, read_json(run_directory / "metadata.json"))
        status = current_status(run)
        agents.append(status)
        request = status.get("pending_request")
        if request:
            pending.append({"agent_id": status["agent_id"], "backend": status["backend"], **request})
        if status.get("group_mirror_error"):
            warnings.append({"agent_id": status["agent_id"], "error": status["group_mirror_error"]})
        if status["status"] in {"failed", "interrupted"}:
            failed_agents.append({"agent_id": status["agent_id"], "backend": status["backend"], "status": status["status"], "summary": status["summary"]})
    all_terminal = bool(agents) and all(agent["status"] in TERMINAL_STATUSES for agent in agents)
    action_required = bool(pending or warnings)
    group_status = "completed" if all_terminal and not action_required else ("action_required" if action_required else "running")
    return {
        **metadata,
        "status": group_status,
        "agents": agents,
        "coordinator_inbox": {
            "unread": len(pending),
            "requires_response": len(pending),
            "messages": pending,
        },
        "warnings": warnings,
        "failed_agents": failed_agents,
        "action_required": action_required,
        "complete": all_terminal and not action_required,
    }


def respond_group_request(root: Path, group_id: str, request_id: str, answer: str) -> str:
    for member in list_members(root, group_id):
        run_directory = Path(member["task_directory"])
        if not (run_directory / "metadata.json").exists():
            continue
        run = Run(run_directory, read_json(run_directory / "metadata.json"))
        from core_lib import get_request

        request = get_request(run.database, request_id)
        if request and request["status"] == "pending":
            respond_request(run.database, request_id, {"answer": answer})
            return run.metadata["agent_id"]
    raise ValueError(f"no pending request in group {group_id}: {request_id}")


def parse_until(value: str) -> set[str]:
    allowed = {"action", "failed", "warning", "complete"}
    selected = {item.strip() for item in value.split(",") if item.strip()}
    unknown = selected - allowed
    if unknown:
        raise ValueError(f"unknown supervise condition: {', '.join(sorted(unknown))}; expected action, failed, warning, or complete")
    if not selected:
        raise ValueError("supervise --until requires at least one condition")
    return selected


def supervise_group(root: Path, group_id: str, after: int, timeout: float | None, limit: int, until: set[str] | None = None) -> dict[str, Any]:
    selected = until or {"action", "failed", "warning", "complete"}
    deadline = None if timeout is None else time.monotonic() + timeout
    cursor = after
    events: list[dict[str, Any]] = []
    while True:
        remaining = max(0, limit - len(events))
        batch = list_group_events(root, group_id, cursor, remaining) if remaining else []
        if batch:
            events.extend(batch)
            cursor = batch[-1]["cursor"]
        snapshot = group_snapshot(root, group_id)
        if snapshot["coordinator_inbox"]["requires_response"] and "action" in selected:
            reason = "action_required"
            break
        if snapshot["warnings"] and "warning" in selected:
            reason = "supervision_warning"
            break
        if snapshot["failed_agents"] and "failed" in selected:
            reason = "worker_failed"
            break
        if snapshot["complete"] and "complete" in selected:
            reason = "complete"
            break
        if deadline is not None and time.monotonic() >= deadline:
            reason = "timeout"
            break
        time.sleep(0.2)
    truncated = bool(list_group_events(root, group_id, cursor, 1))
    return {
        "group_id": group_id,
        "events": events,
        "next_cursor": cursor,
        "reason": reason,
        "timed_out": reason == "timeout",
        "truncated": truncated,
        "action_required": snapshot["action_required"],
        "coordinator_inbox": snapshot["coordinator_inbox"],
        "warnings": snapshot["warnings"],
        "failed_agents": snapshot["failed_agents"],
        "status": snapshot["status"],
        "complete": snapshot["complete"],
    }


def command_events(arguments: argparse.Namespace, run: Run) -> None:
    deadline = time.monotonic() + arguments.wait
    events = list_events(run.database, arguments.after, arguments.limit)
    while not events and time.monotonic() < deadline:
        time.sleep(0.2)
        events = list_events(run.database, arguments.after, arguments.limit)
    next_cursor = events[-1]["cursor"] if events else arguments.after
    emit({"agent_id": arguments.agent_id, "events": events, "next_cursor": next_cursor})


def interrupt_and_resume(run: Run, instruction: str) -> int:
    backend_pid = get_state(run.database, "backend_pid")
    if backend_pid and process_alive(backend_pid):
        terminate_process_group(backend_pid)
    worker_pid = read_json(run.directory / "metadata.json").get("worker_pid")
    deadline = time.monotonic() + 5
    while process_alive(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    session_id = get_state(run.database, "session_id")
    if not session_id:
        raise ValueError("backend session id is not available yet; queued instruction remains pending")
    prompt = "Your previous turn was interrupted by the parent. Call delegate_parent.check_inbox immediately and follow the newest instruction before continuing."
    return launch_worker(run, prompt, session_id)


def command_resume(arguments: argparse.Namespace, run: Run) -> None:
    status = current_status(run)["status"]
    if status in ACTIVE_STATUSES:
        raise ValueError(f"agent is already active: {status}")
    session_id = get_state(run.database, "session_id")
    if not session_id:
        raise ValueError("run has no recorded backend session id")
    changes: dict[str, Any] = {}
    if arguments.model:
        changes["model"] = arguments.model
    if arguments.reasoning:
        changes["reasoning_effort"] = arguments.reasoning
    if changes:
        update_metadata(run, **changes)
    enqueue_message(run.database, "follow_up", {"text": arguments.prompt}, "normal")
    prompt = f"Call delegate_parent.check_inbox immediately. Continue the parent task using the new follow-up instruction.\n\nFollow-up:\n{arguments.prompt}"
    worker_pid = launch_worker(run, prompt, session_id)
    emit({"agent_id": arguments.agent_id, "status": "starting", "worker_pid": worker_pid, "resumed": True})


def command_result(run: Run) -> None:
    status = current_status(run)
    result = get_state(run.database, "result", {})
    final_path = run.directory / "final.txt"
    emit({**status, "result": result, "final_message": final_path.read_text().strip() if final_path.exists() else "", "exit_code": get_state(run.database, "exit_code")})


def command_doctor(root: Path) -> None:
    backends = probe_backends()
    emit({"ok": any(item["usable"] for item in backends), "backends": backends, "state_root": str(root), "python": sys.version.split()[0]})


def command_models(root: Path, backend_id: str, include_hidden: bool, include_capabilities: bool) -> None:
    capabilities = get_backend(backend_id).probe(include_hidden=include_hidden)
    if not capabilities.usable:
        raise ValueError(capabilities.error or f"backend is unavailable: {backend_id}")
    catalog = capabilities.as_dict()
    if include_capabilities:
        catalog.update(
            {
                "state_root": str(root),
                "python": sys.version.split()[0],
                "commands": ["models", "capabilities", "roles", "role", "resolve", "spawn", "list", "status", "events", "wait", "steer", "respond", "cancel", "resume", "result"],
                "steering_modes": ["checkpoint", "urgent", "interrupt"],
                "parallel_agents": True,
                "parallel_writers_require_separate_checkouts": True,
                "roles": sorted(load_roles(root)),
                "complexities": ["simple", "standard", "complex"],
                "model_policies": ["economy", "default", "capable"],
            }
        )
    emit(catalog)


def main() -> int:
    arguments = parser().parse_args()
    root = resolved_root(arguments)
    try:
        if arguments.command == "_worker":
            prompt = arguments.prompt_file.read_text().rstrip("\n")
            return worker_main(arguments.run_directory.resolve(), prompt, arguments.resume_session_id)
        if arguments.command == "doctor":
            command_doctor(root)
            return 0
        if arguments.command == "backends":
            backends = probe_backends()
            emit({"backends": backends, "configured_default": os.environ.get("DELEGATE_AGENT_BACKEND")})
            return 0
        if arguments.command == "backend":
            emit(get_backend(arguments.backend_id).probe(include_hidden=True).as_dict())
            return 0
        if arguments.command in {"models", "capabilities"}:
            command_models(root, arguments.backend, arguments.include_hidden, arguments.command == "capabilities")
            return 0
        if arguments.command in {"roles", "role"}:
            roles = load_roles(root)
            if arguments.command == "role":
                selected = roles.get(arguments.role_id)
                if selected is None:
                    raise ValueError(f"unknown role: {arguments.role_id}")
                emit(selected)
            else:
                emit(
                    {
                        "roles": [
                            {
                                "id": role["id"],
                                "description": role["description"],
                                "default_complexity": role["default_complexity"],
                                "sandbox": role["sandbox"],
                            }
                            for role in roles.values()
                        ]
                    }
                )
            return 0
        if arguments.command == "resolve":
            resolution = resolve_configuration(arguments, root)
            emit(
                {
                    "backend": resolution["backend"],
                    "backend_source": resolution["backend_source"],
                    "role": resolution["role"]["id"],
                    "complexity": resolution["complexity"],
                    "complexity_source": resolution["complexity_source"],
                    "model": resolution["model"],
                    "model_source": resolution["model_source"],
                    "model_policy": resolution["model_policy"],
                    "model_policy_source": resolution["model_policy_source"],
                    "reasoning_effort": resolution["reasoning_effort"],
                    "reasoning_source": resolution["reasoning_source"],
                    "sandbox": resolution["sandbox"],
                    "sandbox_source": resolution["sandbox_source"],
                    "warnings": resolution["warnings"],
                }
            )
            return 0
        if arguments.command == "spawn":
            command_spawn(arguments, root)
            return 0
        if arguments.command == "group":
            if arguments.group_command == "create":
                emit(create_group(root, arguments.name, arguments.coordinator))
            elif arguments.group_command == "list":
                emit({"groups": [group_snapshot(root, group["group_id"]) for group in iter_groups(root)]})
            elif arguments.group_command == "status":
                emit(group_snapshot(root, arguments.group_id))
            elif arguments.group_command == "respond":
                agent_id = respond_group_request(root, arguments.group_id, arguments.request_id, arguments.answer)
                emit({"group_id": arguments.group_id, "agent_id": agent_id, "request_id": arguments.request_id, "status": "answered"})
            return 0
        if arguments.command == "supervise":
            until = parse_until(arguments.until)
            timeout = None if arguments.follow else arguments.timeout
            emit(supervise_group(root, arguments.group_id, arguments.after, timeout, arguments.limit, until))
            return 0
        if arguments.command == "list":
            workspace = str(arguments.workspace.expanduser().resolve()) if arguments.workspace else None
            statuses = []
            for run in iter_runs(root):
                status = current_status(run)
                if arguments.active and status["status"] not in ACTIVE_STATUSES:
                    continue
                if workspace and status["cwd"] != workspace:
                    continue
                statuses.append(status)
            statuses.sort(key=lambda item: item["created_at"], reverse=True)
            emit({"agents": statuses})
            return 0
        if arguments.command == "cleanup":
            removed_runs = cleanup_runs(root, arguments.completed_days, arguments.failed_days)
            emit({"removed": removed_runs, "group_cleanup": cleanup_groups(root)})
            return 0

        run = find_run(root, arguments.agent_id)
        if arguments.command == "status":
            emit(current_status(run))
        elif arguments.command == "events":
            command_events(arguments, run)
        elif arguments.command == "steer":
            status = current_status(run)
            worker_active = status["status"] in ACTIVE_STATUSES
            if arguments.interrupt and not worker_active:
                raise ValueError(f"cannot interrupt terminal worker: {status['status']}")
            message_id = enqueue_message(run.database, "instruction", {"text": arguments.instruction}, arguments.priority)
            worker_pid = None
            if arguments.interrupt:
                worker_pid = interrupt_and_resume(run, arguments.instruction)
            delivery = "interrupt" if arguments.interrupt else ("checkpoint" if worker_active else "next_resume")
            warning = None if worker_active else "Worker is terminal; instruction will be read after resume."
            emit({
                "agent_id": arguments.agent_id,
                "message_id": message_id,
                "queued": True,
                "delivery": delivery,
                "worker_active": worker_active,
                "worker_pid": worker_pid,
                "warning": warning,
            })
        elif arguments.command == "respond":
            respond_request(run.database, arguments.request_id, {"answer": arguments.answer})
            emit({"agent_id": arguments.agent_id, "request_id": arguments.request_id, "status": "answered"})
        elif arguments.command == "cancel":
            set_state(run.database, "cancel_requested", True)
            set_state(run.database, "status", "cancelling")
            backend_pid = get_state(run.database, "backend_pid")
            if backend_pid and process_alive(backend_pid):
                terminate_process_group(backend_pid)
            emit({"agent_id": arguments.agent_id, "status": "cancelling"})
        elif arguments.command == "resume":
            command_resume(arguments, run)
        elif arguments.command == "wait":
            deadline = time.monotonic() + arguments.timeout
            status = current_status(run)
            while status["status"] in ACTIVE_STATUSES and time.monotonic() < deadline:
                time.sleep(0.25)
                status = current_status(run)
            emit(status)
        elif arguments.command == "result":
            command_result(run)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        emit({"error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
