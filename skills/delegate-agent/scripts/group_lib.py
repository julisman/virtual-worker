from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
MIRRORED_EVENT_TYPES = {
    "agent.message",
    "input.requested",
    "input.responded",
    "mailbox.failed",
    "progress.reported",
    "result.recorded",
    "worker.failed",
    "worker.finished",
    "worker.started",
    "worker.warning",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_group_id() -> str:
    return f"grp_{secrets.token_hex(5)}"


def group_directory(root: Path, group_id: str) -> Path:
    return root / "groups" / group_id


def group_database(root: Path, group_id: str) -> Path:
    return group_directory(root, group_id) / "events.db"


def connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def initialize_group_database(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                agent_id TEXT PRIMARY KEY,
                backend TEXT NOT NULL,
                task_directory TEXT NOT NULL,
                joined_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT,
                source TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def create_group(root: Path, name: str | None = None, coordinator: str = "parent", group_id: str | None = None) -> dict[str, Any]:
    identifier = group_id or create_group_id()
    directory = group_directory(root, identifier)
    if directory.exists():
        raise ValueError(f"group already exists: {identifier}")
    directory.mkdir(parents=True, mode=0o700)
    metadata = {
        "group_id": identifier,
        "name": name or identifier,
        "coordinator": coordinator,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    initialize_group_database(directory / "events.db")
    return metadata


def read_group(root: Path, group_id: str) -> dict[str, Any]:
    path = group_directory(root, group_id) / "metadata.json"
    if not path.exists():
        raise ValueError(f"unknown group: {group_id}")
    return json.loads(path.read_text())


def iter_groups(root: Path) -> Iterable[dict[str, Any]]:
    groups_root = root / "groups"
    if not groups_root.exists():
        return
    for path in groups_root.glob("*/metadata.json"):
        yield json.loads(path.read_text())


def add_member(root: Path, group_id: str, agent_id: str, backend: str, task_directory: Path) -> None:
    read_group(root, group_id)
    database = group_database(root, group_id)
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO members(agent_id, backend, task_directory, joined_at) VALUES (?, ?, ?, ?)",
            (agent_id, backend, str(task_directory), utc_now()),
        )
    add_group_event(root, group_id, agent_id, "runtime", "group.member_joined", {"backend": backend})


def remove_member(root: Path, group_id: str, agent_id: str) -> bool:
    database = group_database(root, group_id)
    if not database.exists():
        return False
    with connect(database) as connection:
        removed = connection.execute("DELETE FROM members WHERE agent_id = ?", (agent_id,)).rowcount
    if removed:
        add_group_event(root, group_id, agent_id, "runtime", "group.member_removed", {})
    return bool(removed)


def remove_group_if_empty(root: Path, group_id: str) -> bool:
    directory = group_directory(root, group_id)
    if not directory.exists():
        return False
    if list_members(root, group_id):
        return False
    shutil.rmtree(directory)
    return True


def cleanup_groups(root: Path) -> dict[str, list[str]]:
    removed_members: list[str] = []
    removed_groups: list[str] = []
    for metadata in list(iter_groups(root)):
        group_id = metadata["group_id"]
        for member in list_members(root, group_id):
            if not (Path(member["task_directory"]) / "metadata.json").exists():
                if remove_member(root, group_id, member["agent_id"]):
                    removed_members.append(member["agent_id"])
        if remove_group_if_empty(root, group_id):
            removed_groups.append(group_id)
    return {"groups": removed_groups, "members": removed_members}


def list_members(root: Path, group_id: str) -> list[dict[str, Any]]:
    read_group(root, group_id)
    with connect(group_database(root, group_id)) as connection:
        rows = connection.execute("SELECT * FROM members ORDER BY joined_at").fetchall()
    return [dict(row) for row in rows]


def add_group_event(root: Path, group_id: str, agent_id: str | None, source: str, event_type: str, payload: Any) -> int:
    database = group_database(root, group_id)
    initialize_group_database(database)
    with connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO events(agent_id, source, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (agent_id, source, event_type, json.dumps(payload), utc_now()),
        ).lastrowid
    return int(cursor)


def compact_payload(event_type: str, payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {"value": payload}
    if event_type == "progress.reported":
        return {key: data[key] for key in ("phase", "summary") if key in data}
    if event_type == "agent.message":
        return {"text": str(data.get("text", ""))[:2000], "final": bool(data.get("final"))}
    if event_type == "input.requested":
        return {key: data[key] for key in ("request_id", "question", "options", "blocking") if key in data}
    if event_type == "input.responded":
        return {key: data[key] for key in ("request_id", "response") if key in data}
    if event_type in {"worker.started", "worker.finished", "worker.failed"}:
        return {key: data[key] for key in ("backend", "status", "exit_code", "error", "resume") if key in data}
    if event_type in {"mailbox.failed", "worker.warning"}:
        return {key: data[key] for key in ("message", "tool") if key in data}
    if event_type == "result.recorded":
        return {"summary": str(data.get("summary", ""))[:2000]}
    return data


def mirror_group_event(root: Path, group_id: str, agent_id: str | None, source: str, event_type: str, payload: Any) -> int | None:
    if event_type not in MIRRORED_EVENT_TYPES:
        return None
    return add_group_event(root, group_id, agent_id, source, event_type, compact_payload(event_type, payload))


def list_group_events(root: Path, group_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    read_group(root, group_id)
    with connect(group_database(root, group_id)) as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE cursor > ? ORDER BY cursor LIMIT ?", (after, limit)
        ).fetchall()
    return [{
        "cursor": row["cursor"],
        "agent_id": row["agent_id"],
        "source": row["source"],
        "type": row["type"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"],
    } for row in rows]


def wait_group_events(root: Path, group_id: str, after: int, timeout: float, limit: int = 200) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    events = list_group_events(root, group_id, after, limit)
    while not events and time.monotonic() < deadline:
        time.sleep(0.2)
        events = list_group_events(root, group_id, after, limit)
    return events
