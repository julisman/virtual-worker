from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE_STATUSES = {"starting", "running", "waiting_for_parent", "cancelling"}
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root() -> Path:
    override = os.environ.get("DELEGATE_AGENT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "delegate-agent"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "delegate-agent"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "delegate-agent"


def bundled_roles_root() -> Path:
    return Path(__file__).resolve().parents[1] / "roles"


def validate_role(role: dict[str, Any], source: Path) -> None:
    required = {"id", "description", "default_complexity", "sandbox", "instructions", "complexity_profiles"}
    missing = sorted(required - role.keys())
    if missing:
        raise ValueError(f"invalid role {source}: missing {', '.join(missing)}")
    if role["default_complexity"] not in {"simple", "standard", "complex"}:
        raise ValueError(f"invalid role {source}: unsupported default complexity")
    for complexity in ("simple", "standard", "complex"):
        profile = role["complexity_profiles"].get(complexity)
        if not profile or "model_policy" not in profile or "reasoning_effort" not in profile:
            raise ValueError(f"invalid role {source}: incomplete {complexity} profile")


def load_roles(root: Path | None = None) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    for source_root in [bundled_roles_root(), root / "roles" if root else None]:
        if source_root is None or not source_root.exists():
            continue
        for path in sorted(source_root.glob("*.json")):
            role = read_json(path)
            validate_role(role, path)
            role["source"] = str(path)
            roles[role["id"]] = role
    return roles


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def slugify(value: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "task")[:limit].rstrip("-")


def workspace_identity(cwd: Path) -> tuple[str, str]:
    canonical = cwd.resolve()
    name = slugify(canonical.name, 28)
    digest = hashlib.sha256(str(canonical).encode()).hexdigest()[:6]
    return f"{name}-{digest}", str(canonical)


def create_agent_id() -> str:
    return f"ca_{secrets.token_hex(6)}"


def task_directory(root: Path, cwd: Path, prompt: str, agent_id: str) -> Path:
    workspace_id, _ = workspace_identity(cwd)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    directory = root / "tasks" / workspace_id / f"{stamp}-{slugify(prompt)}-{agent_id}"
    directory.mkdir(parents=True, exist_ok=False)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def acquire_workspace_lock(root: Path, workspace_id: str, agent_id: str, run_directory: Path) -> Path:
    lock_directory = root / "workspace-locks"
    ensure_private_dir(lock_directory)
    lock_path = lock_directory / f"{workspace_id}.json"
    payload = json.dumps({"agent_id": agent_id, "run_directory": str(run_directory), "created_at": utc_now()}) + "\n"
    for _ in range(2):
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = read_json(lock_path)
                existing_run = Path(existing["run_directory"])
                metadata_path = existing_run / "metadata.json"
                database = existing_run / "mailbox.db"
                if metadata_path.exists() and database.exists():
                    metadata = read_json(metadata_path)
                    if get_state(database, "status", metadata.get("status")) in ACTIVE_STATUSES:
                        raise ValueError(
                            f"workspace already has an active write agent: {existing.get('agent_id')}; "
                            "use a separate Git worktree, read-only sandbox, or --allow-shared-checkout"
                        )
                lock_path.unlink(missing_ok=True)
                continue
            except (OSError, KeyError, json.JSONDecodeError):
                lock_path.unlink(missing_ok=True)
                continue
        else:
            with os.fdopen(descriptor, "w") as output:
                output.write(payload)
            return lock_path
    raise ValueError(f"unable to acquire workspace lock: {workspace_id}")


def release_workspace_lock(lock_path: Path | None, agent_id: str) -> None:
    if not lock_path or not lock_path.exists():
        return
    try:
        if read_json(lock_path).get("agent_id") == agent_id:
            lock_path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def initialize_database(database: Path) -> None:
    with connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                kind TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'normal',
                body_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                acknowledged_at TEXT
            );
            CREATE TABLE IF NOT EXISTS progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phase TEXT NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                question_json TEXT NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT,
                created_at TEXT NOT NULL,
                responded_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def set_state(database: Path, key: str, value: Any) -> None:
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO state(key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value), utc_now()),
        )


def get_state(database: Path, key: str, default: Any = None) -> Any:
    with connect(database) as connection:
        row = connection.execute("SELECT value_json FROM state WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value_json"])


def add_event(database: Path, source: str, event_type: str, payload: Any) -> int:
    with connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO events(source, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (source, event_type, json.dumps(payload), utc_now()),
        ).lastrowid
    metadata_path = database.parent / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = read_json(metadata_path)
            group_id = metadata.get("group_id")
            if group_id:
                from group_lib import MIRRORED_EVENT_TYPES, mirror_group_event

                mirror_group_event(database.parent.parents[2], group_id, metadata.get("agent_id"), source, event_type, payload)
                if event_type in MIRRORED_EVENT_TYPES and get_state(database, "group_mirror_error"):
                    set_state(database, "group_mirror_error", None)
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            set_state(database, "group_mirror_error", str(exc))
    return int(cursor)


def list_events(database: Path, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE cursor > ? ORDER BY cursor LIMIT ?",
            (after, limit),
        ).fetchall()
    return [
        {
            "cursor": row["cursor"],
            "source": row["source"],
            "type": row["event_type"],
            "data": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def enqueue_message(database: Path, kind: str, body: Any, priority: str = "normal") -> int:
    with connect(database) as connection:
        message_id = connection.execute(
            "INSERT INTO messages(sender, kind, priority, body_json, created_at) VALUES ('parent', ?, ?, ?, ?)",
            (kind, priority, json.dumps(body), utc_now()),
        ).lastrowid
    add_event(database, "parent", "message.queued", {"message_id": message_id, "kind": kind, "priority": priority})
    return int(message_id)


def check_messages(database: Path, after_id: int = 0) -> list[dict[str, Any]]:
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT * FROM messages WHERE id > ? AND status = 'pending' ORDER BY id",
            (after_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "priority": row["priority"],
            "body": json.loads(row["body_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def acknowledge_messages(database: Path, message_ids: Iterable[int]) -> int:
    identifiers = list(message_ids)
    if not identifiers:
        return 0
    placeholders = ",".join("?" for _ in identifiers)
    with connect(database) as connection:
        result = connection.execute(
            f"UPDATE messages SET status = 'acknowledged', acknowledged_at = ? WHERE id IN ({placeholders})",
            (utc_now(), *identifiers),
        )
    add_event(database, "worker", "message.acknowledged", {"message_ids": identifiers})
    return result.rowcount


def report_progress(database: Path, phase: str, summary: str, details: dict[str, Any]) -> int:
    with connect(database) as connection:
        progress_id = connection.execute(
            "INSERT INTO progress(phase, summary, details_json, created_at) VALUES (?, ?, ?, ?)",
            (phase, summary, json.dumps(details), utc_now()),
        ).lastrowid
    set_state(database, "phase", phase)
    set_state(database, "summary", summary)
    set_state(database, "last_heartbeat", utc_now())
    add_event(database, "worker", "progress.reported", {"phase": phase, "summary": summary, "details": details})
    return int(progress_id)


def create_request(database: Path, question: dict[str, Any]) -> str:
    request_id = f"req_{secrets.token_hex(5)}"
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO requests(id, question_json, status, created_at) VALUES (?, ?, 'pending', ?)",
            (request_id, json.dumps(question), utc_now()),
        )
    set_state(database, "status", "waiting_for_parent")
    add_event(database, "worker", "input.requested", {"request_id": request_id, **question})
    return request_id


def respond_request(database: Path, request_id: str, response: Any) -> None:
    with connect(database) as connection:
        result = connection.execute(
            "UPDATE requests SET status = 'answered', response_json = ?, responded_at = ? WHERE id = ? AND status = 'pending'",
            (json.dumps(response), utc_now(), request_id),
        )
    if result.rowcount != 1:
        raise ValueError(f"no pending request: {request_id}")
    set_state(database, "status", "running")
    add_event(database, "parent", "input.responded", {"request_id": request_id, "response": response})


def get_request(database: Path, request_id: str) -> dict[str, Any] | None:
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    return {
        "request_id": row["id"],
        "question": json.loads(row["question_json"]),
        "status": row["status"],
        "response": json.loads(row["response_json"]) if row["response_json"] else None,
        "created_at": row["created_at"],
        "responded_at": row["responded_at"],
    }


def pending_request(database: Path) -> dict[str, Any] | None:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return {"request_id": row["id"], **json.loads(row["question_json"]), "created_at": row["created_at"]}


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_process_group(pid: int, timeout: float = 5.0) -> None:
    if os.name == "nt":
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_alive(pid):
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pid, signal.SIGKILL)


@dataclass(frozen=True)
class Run:
    directory: Path
    metadata: dict[str, Any]

    @property
    def database(self) -> Path:
        return self.directory / "mailbox.db"


def iter_runs(root: Path) -> Iterable[Run]:
    tasks = root / "tasks"
    if not tasks.exists():
        return
    for metadata_path in tasks.glob("*/*/metadata.json"):
        try:
            yield Run(metadata_path.parent, read_json(metadata_path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def find_run(root: Path, agent_id: str) -> Run:
    matches = [run for run in iter_runs(root) if run.metadata.get("agent_id") == agent_id]
    if not matches:
        raise ValueError(f"unknown agent id: {agent_id}")
    if len(matches) > 1:
        raise ValueError(f"duplicate agent id: {agent_id}")
    return matches[0]


def update_metadata(run: Run, **changes: Any) -> dict[str, Any]:
    metadata_path = run.directory / "metadata.json"
    metadata = read_json(metadata_path)
    metadata.update(changes)
    metadata["updated_at"] = utc_now()
    atomic_write_json(metadata_path, metadata)
    return metadata


def current_status(run: Run) -> dict[str, Any]:
    metadata = read_json(run.directory / "metadata.json")
    status = get_state(run.database, "status", metadata.get("status", "unknown"))
    worker_pid = metadata.get("worker_pid")
    backend_pid = get_state(run.database, "backend_pid")
    if status in ACTIVE_STATUSES and not process_alive(worker_pid):
        status = "interrupted"
    latest_event = list_events(run.database, after=max(0, get_state(run.database, "last_cursor", 0) - 1), limit=1)
    return {
        "agent_id": metadata["agent_id"],
        "group_id": metadata.get("group_id"),
        "status": status,
        "phase": get_state(run.database, "phase", "starting"),
        "summary": get_state(run.database, "summary", ""),
        "cwd": metadata["cwd"],
        "role": metadata.get("role", "general"),
        "complexity": metadata.get("complexity", "standard"),
        "model": metadata.get("model"),
        "model_source": metadata.get("model_source"),
        "model_policy": metadata.get("model_policy"),
        "reasoning_effort": metadata.get("reasoning_effort"),
        "reasoning_source": metadata.get("reasoning_source"),
        "sandbox": metadata.get("sandbox"),
        "warnings": metadata.get("warnings", []),
        "worker_pid": worker_pid,
        "worker_alive": process_alive(worker_pid),
        "backend": metadata.get("backend"),
        "backend_source": metadata.get("backend_source"),
        "backend_pid": backend_pid,
        "backend_alive": process_alive(backend_pid),
        "session_id": get_state(run.database, "session_id"),
        "pending_request": pending_request(run.database),
        "mailbox_error": get_state(run.database, "mailbox_error"),
        "group_mirror_error": get_state(run.database, "group_mirror_error"),
        "latest_event_cursor": latest_event[-1]["cursor"] if latest_event else get_state(run.database, "last_cursor", 0),
        "task_directory": str(run.directory),
        "created_at": metadata["created_at"],
        "updated_at": metadata.get("updated_at", metadata["created_at"]),
    }



def worker_main(run_directory: Path, prompt: str, resume_session_id: str | None = None) -> int:
    from backends import get_backend
    from backends.base import LaunchContext, ResumeContext

    run = Run(run_directory, read_json(run_directory / "metadata.json"))
    database = run.database
    initialize_database(database)
    set_state(database, "status", "starting")
    add_event(database, "runner", "worker.started", {"backend": run.metadata["backend"], "resume": bool(resume_session_id)})
    backend = get_backend(run.metadata["backend"])
    context_args = {
        "metadata": run.metadata,
        "mailbox": database,
        "prompt": prompt,
        "mcp_script": Path(__file__).resolve().with_name("parent_mcp.py"),
    }
    if resume_session_id:
        spec = backend.build_resume(ResumeContext(**context_args, session_id=resume_session_id))
    else:
        spec = backend.build_launch(LaunchContext(**context_args))
    flags = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "bufsize": 1}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    try:
        process = subprocess.Popen(spec.command, cwd=run.metadata["cwd"], env=spec.environment, **flags)
    except OSError as exc:
        set_state(database, "status", "failed")
        set_state(database, "summary", f"Unable to start {backend.id}: {exc}")
        add_event(database, "runner", "worker.failed", {"backend": backend.id, "error": str(exc)})
        return 127
    set_state(database, "backend_pid", process.pid)
    set_state(database, "status", "running")
    update_metadata(run, status="running", persistence_enabled=spec.persistence_enabled)
    stderr_path = run.directory / "stderr.log"
    raw_events_path = run.directory / "backend-events.jsonl"
    canonical_events_path = run.directory / "events.jsonl"
    final_path = run.directory / "final.txt"

    def capture_stderr() -> None:
        with stderr_path.open("a") as output:
            assert process.stderr is not None
            for line in process.stderr:
                output.write(line)
                output.flush()

    stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
    stderr_thread.start()
    last_message = ""
    with raw_events_path.open("a") as raw_output, canonical_events_path.open("a") as canonical_output:
        assert process.stdout is not None
        for raw_line in process.stdout:
            raw_output.write(raw_line)
            raw_output.flush()
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = {"type": "backend.output", "text": line}
            raw_type = payload.get("type", "backend.output") if isinstance(payload, dict) else "backend.output"
            cursor = add_event(database, backend.id, raw_type, payload)
            set_state(database, "last_cursor", cursor)
            if not isinstance(payload, dict):
                continue
            session_id = backend.extract_session_id(payload)
            if session_id and not get_state(database, "session_id"):
                set_state(database, "session_id", session_id)
            for event in backend.normalize_event(payload):
                record = {"type": event.type, "data": event.data}
                canonical_output.write(json.dumps(record, separators=(",", ":")) + "\n")
                canonical_output.flush()
                canonical_cursor = add_event(database, "delegate-agent", event.type, event.data)
                set_state(database, "last_cursor", canonical_cursor)
                if event.type == "agent.message" and event.data.get("final"):
                    last_message = str(event.data.get("text", ""))
                elif event.type == "mailbox.failed":
                    set_state(database, "mailbox_error", event.data.get("message", "MCP mailbox tool failed"))
    return_code = process.wait()
    stderr_thread.join(timeout=1)
    if last_message:
        final_path.write_text(last_message + "\n")
    exit_result = backend.classify_exit(return_code, bool(get_state(database, "cancel_requested", False)))
    set_state(database, "status", exit_result.status)
    set_state(database, "exit_code", return_code)
    set_state(database, "backend_pid", None)
    set_state(database, "summary", get_state(database, "summary", "") or exit_result.summary)
    add_event(database, "runner", "worker.finished", {"backend": backend.id, "status": exit_result.status, "exit_code": return_code})
    update_metadata(run, status=exit_result.status)
    lock_value = run.metadata.get("workspace_lock")
    release_workspace_lock(Path(lock_value) if lock_value else None, run.metadata["agent_id"])
    return return_code

def cleanup_runs(root: Path, completed_days: int = 3, failed_days: int = 7) -> list[str]:
    now = datetime.now(timezone.utc)
    removed: list[str] = []
    for run in list(iter_runs(root)):
        status = current_status(run)["status"]
        if status not in TERMINAL_STATUSES:
            continue
        updated = datetime.fromisoformat(read_json(run.directory / "metadata.json")["updated_at"])
        retention = failed_days if status in {"failed", "interrupted"} else completed_days
        if now - updated < timedelta(days=retention):
            continue
        import shutil

        shutil.rmtree(run.directory)
        removed.append(run.metadata["agent_id"])
    return removed
