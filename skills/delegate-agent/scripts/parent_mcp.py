#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from core_lib import (
    acknowledge_messages,
    add_event,
    check_messages,
    create_request,
    get_request,
    initialize_database,
    report_progress,
    set_state,
)


TOOLS = [
    {
        "name": "check_inbox",
        "description": "Read unread instructions sent by the parent agent. Call at meaningful checkpoints and before completion.",
        "inputSchema": {
            "type": "object",
            "properties": {"after_message_id": {"type": "integer", "minimum": 0}},
            "additionalProperties": False,
        },
    },
    {
        "name": "acknowledge",
        "description": "Acknowledge parent messages after incorporating their instructions.",
        "inputSchema": {
            "type": "object",
            "properties": {"message_ids": {"type": "array", "items": {"type": "integer"}}},
            "required": ["message_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "report_progress",
        "description": "Report a concise semantic milestone to the parent agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {"type": "string"},
                "summary": {"type": "string"},
                "details": {"type": "object"},
            },
            "required": ["phase", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_input",
        "description": "Ask the parent agent a blocking question when a decision cannot be safely inferred.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
                "context": {"type": "string"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_for_response",
        "description": "Wait briefly for the parent to answer a previously created request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 30},
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "complete",
        "description": "Record the structured final result before sending the final response.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "files_changed": {"type": "array", "items": {"type": "string"}},
                "tests": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
]


def text_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        "isError": is_error,
    }


def call_tool(database: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "check_inbox":
        messages = check_messages(database, int(arguments.get("after_message_id", 0)))
        set_state(database, "last_heartbeat", time.time())
        return text_result({"messages": messages})
    if name == "acknowledge":
        count = acknowledge_messages(database, arguments["message_ids"])
        return text_result({"acknowledged": count})
    if name == "report_progress":
        progress_id = report_progress(database, arguments["phase"], arguments["summary"], arguments.get("details", {}))
        return text_result({"progress_id": progress_id})
    if name == "request_input":
        request_id = create_request(database, arguments)
        return text_result({"request_id": request_id, "status": "pending"})
    if name == "wait_for_response":
        request_id = arguments["request_id"]
        deadline = time.monotonic() + min(int(arguments.get("timeout_seconds", 10)), 30)
        request = get_request(database, request_id)
        while request and request["status"] == "pending" and time.monotonic() < deadline:
            time.sleep(0.25)
            request = get_request(database, request_id)
        if request is None:
            return text_result({"error": "unknown request", "request_id": request_id}, True)
        return text_result(request)
    if name == "complete":
        set_state(database, "result", arguments)
        set_state(database, "summary", arguments["summary"])
        add_event(database, "worker", "result.recorded", arguments)
        return text_result({"recorded": True})
    return text_result({"error": f"unknown tool: {name}"}, True)


def handle_request(database: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "delegate-parent-mailbox", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params", {})
        result = call_tool(database, params.get("name", ""), params.get("arguments", {}))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Local MCP mailbox for a spawned Codex agent")
    parser.add_argument("--mailbox", required=True, type=Path)
    arguments = parser.parse_args()
    database = arguments.mailbox.expanduser().resolve()
    initialize_database(database)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(database, request)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
