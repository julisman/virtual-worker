"""Agent-facing tools for inspecting and relaying delegated task results."""

from __future__ import annotations

from agent import delivery
from storage import agent_store, task_store

_NOT_FOUND = "Task not found or not yours"


def _owned_task(caller_id: str, task_id: str) -> tuple[dict, dict] | None:
    """Find a task across boards, returning it only for its assigning agent."""
    for agent in agent_store.list_agents():
        task = task_store.get(agent["id"], task_id)
        if task and (task.get("metadata") or {}).get("assignedBy") == caller_id:
            return task, agent
    return None


def make_inbox_tools(agent_id: str, session_id: str | None) -> dict[str, dict]:
    """Build task_status and relay_task_result tools for an agent run."""

    async def task_status(task_id: str = "") -> str:
        found = _owned_task(agent_id, (task_id or "").strip())
        if not found:
            return _NOT_FOUND
        task, owner = found
        summary = task.get("resultSummary") or ""
        marker = " (truncated)"
        if len(summary) > 1500:
            summary = summary[:1500 - len(marker)] + marker
        return (f"status={task.get('status')}; title={task.get('title')}; "
                f"assignee={owner.get('name') or owner.get('id')}; "
                f"updatedAt={task.get('updatedAt')}; resultSummary={summary or '(none)'}")

    async def relay_task_result(task_id: str = "") -> str:
        found = _owned_task(agent_id, (task_id or "").strip())
        if not found:
            return _NOT_FOUND
        task, _owner = found
        text = task.get("resultSummary") or ""
        if not session_id:
            return "cannot relay — this run has no conversation channel"
        if session_id.startswith("chat:"):
            record = await delivery.deliver(
                "in_app", session_id, text, agent_id=agent_id, source="tool")
        elif session_id.count(":") >= 2:
            # "<channel>:<accountId>:<conversationId>" — a chat-channel session.
            _ch, account_id, conversation_id = session_id.split(":", 2)
            record = await delivery.deliver(
                "chat", f"{account_id}:{conversation_id}", text, agent_id=agent_id, source="tool")
        else:
            return "cannot relay — this run has no conversation channel"
        return f"relayed {len(text)} chars ({record.get('status', 'unknown')})"

    return {
        "task_status": {
            "fn": task_status,
            "schema": {
                "type": "function",
                "function": {
                    "name": "task_status",
                    "description": "Inspect the status and result summary of a task you assigned.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"],
                    },
                },
            },
        },
        "relay_task_result": {
            "fn": relay_task_result,
            "schema": {
                "type": "function",
                "function": {
                    "name": "relay_task_result",
                    "description": "Forward the full result of a task you assigned into this chat.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"],
                    },
                },
            },
        },
    }

