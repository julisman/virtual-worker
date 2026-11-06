from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import (
    BackendCapabilities,
    BackendFeatures,
    CanonicalEvent,
    ExitResult,
    LaunchContext,
    LaunchSpec,
    ResolutionRequest,
    ResumeContext,
)


# Reasoning-effort ordering shared with Codex so a requested effort can be
# clamped to the nearest supported Claude level instead of silently swapped.
REASONING_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"]
CLAUDE_EFFORTS = ("low", "medium", "high")

MCP_SERVER_KEY = "delegate_parent"
MAILBOX_TOOLS = ["check_inbox", "acknowledge", "report_progress", "request_input", "wait_for_response", "complete"]
MAILBOX_ALLOW = [f"mcp__{MCP_SERVER_KEY}__{tool}" for tool in MAILBOX_TOOLS]

# Native Claude tools mapped per portable sandbox level. `Task` is always
# disallowed so a worker cannot spawn its own subagents outside the runtime's
# supervision (plan doc §21.1 — Claude has no `agents.enabled=false` equivalent).
READ_TOOLS = ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
WRITE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"]
NESTED_SPAWN_TOOLS = ["Task"]

# Static aliases. Full model IDs are still accepted verbatim (plan doc §7) — the
# alias catalog exists for policy resolution, not to reject explicit models.
STATIC_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "sonnet", "model": "sonnet", "display_name": "Claude Sonnet", "is_default": True, "hidden": False, "reasoning_efforts": [{"effort": effort} for effort in CLAUDE_EFFORTS]},
    {"id": "opus", "model": "opus", "display_name": "Claude Opus", "is_default": False, "hidden": False, "reasoning_efforts": [{"effort": effort} for effort in CLAUDE_EFFORTS]},
    {"id": "haiku", "model": "haiku", "display_name": "Claude Haiku", "is_default": False, "hidden": False, "reasoning_efforts": [{"effort": effort} for effort in CLAUDE_EFFORTS]},
)


def policies_path() -> Path:
    return Path(__file__).resolve().parents[2] / "policies" / "claude.json"


def load_policies() -> dict[str, dict[str, Any]]:
    return json.loads(policies_path().read_text())


class ClaudeBackend:
    """Claude Code CLI adapter. Pure transforms only: no process start, no state
    writes, no lock or mailbox mutation (plan doc §6)."""

    id = "claude"

    def _binary(self) -> str | None:
        binary = os.environ.get("DELEGATE_AGENT_CLAUDE_BIN", "claude")
        return shutil.which(binary) if os.sep not in binary else binary

    def _version(self, binary: str) -> str | None:
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (result.stdout or result.stderr).strip() or None if result.returncode == 0 else None

    def _features(self) -> BackendFeatures:
        return BackendFeatures(
            resume=True,
            checkpoint_steering=True,
            interrupt_steering=True,
            progress_reporting=True,
            reasoning_selection=True,
            session_storage_owner="backend",
        )

    def probe(self, include_hidden: bool = False) -> BackendCapabilities:
        executable = self._binary()
        if not executable:
            return BackendCapabilities(
                id=self.id,
                name="Claude Code",
                installed=False,
                usable=False,
                executable=None,
                version=None,
                features=self._features(),
                error="Claude executable not found",
                models_dynamic=False,
            )
        return BackendCapabilities(
            id=self.id,
            name="Claude Code",
            installed=True,
            usable=True,
            executable=str(executable),
            version=self._version(str(executable)),
            models=STATIC_MODELS,
            reasoning_efforts=CLAUDE_EFFORTS,
            features=self._features(),
            models_dynamic=False,
        )

    def resolve(self, request: ResolutionRequest) -> dict[str, Any]:
        capabilities = self.probe()
        if not capabilities.usable:
            raise ValueError(capabilities.error or "Claude backend is unavailable")
        role = request.roles.get(request.role_id)
        if role is None:
            raise ValueError(f"unknown role: {request.role_id}")
        complexity = request.complexity or role["default_complexity"]
        profile = role["complexity_profiles"][complexity]
        policy_id = request.model_policy or profile["model_policy"]
        policies = load_policies()
        if policy_id not in policies:
            raise ValueError(f"unknown model policy: {policy_id}")
        warnings: list[str] = []
        aliases = {model["id"] for model in STATIC_MODELS}
        if request.model:
            # Permissive: honor any explicit model, alias or full ID, never reject.
            model = request.model
            model_source = "explicit"
            if model not in aliases:
                warnings.append(f"model {model} is not a known Claude alias; passing through verbatim")
        else:
            preferred = policies[policy_id].get("preferred_models", [])
            model = next((alias for alias in preferred if alias in aliases), None)
            if model is not None:
                model_source = f"policy:{policy_id}:{model}"
            else:
                model = next((item["id"] for item in STATIC_MODELS if item["is_default"]), "sonnet")
                model_source = "default-alias"
                warnings.append(f"no preferred model for policy {policy_id}; using default alias {model}")
        requested_effort = request.reasoning or profile["reasoning_effort"]
        if requested_effort in CLAUDE_EFFORTS:
            reasoning = requested_effort
        else:
            index = REASONING_ORDER.index(requested_effort) if requested_effort in REASONING_ORDER else 1
            reasoning = min(CLAUDE_EFFORTS, key=lambda item: abs(REASONING_ORDER.index(item) - index))
            warnings.append(f"reasoning {requested_effort} unsupported by Claude; using {reasoning}")
        return {
            "backend": self.id,
            "backend_source": "explicit",
            "role": role,
            "complexity": complexity,
            "complexity_source": "explicit" if request.complexity else "role-default",
            "model_policy": policy_id,
            "model_policy_source": "explicit" if request.model_policy else "role-complexity",
            "model": model,
            "model_source": model_source,
            "reasoning_effort": reasoning,
            "reasoning_source": "explicit" if request.reasoning else "role-complexity",
            "sandbox": request.sandbox or role["sandbox"],
            "sandbox_source": "explicit" if request.sandbox else "role-default",
            "warnings": warnings,
        }

    def _permission_flags(self, sandbox: str) -> tuple[str, list[str], list[str]]:
        """Return (permission_mode, allowed_tools, disallowed_tools) for a portable
        sandbox level. Mappings are conservative and explicit (plan doc §10).

        Note: `--permission-mode plan` blocks the private mailbox MCP tools
        ("Cannot call ... while in plan mode"), so read-only intentionally uses
        `default` with a restrictive allowlist instead — verified live against
        Claude CLI 2.1.227."""
        if sandbox == "read-only":
            return "default", [*MAILBOX_ALLOW, *READ_TOOLS], [*NESTED_SPAWN_TOOLS, *WRITE_TOOLS]
        if sandbox == "workspace-write":
            return "acceptEdits", [*MAILBOX_ALLOW, *READ_TOOLS, *WRITE_TOOLS], [*NESTED_SPAWN_TOOLS]
        if sandbox == "danger-full-access":
            # The runtime enforces DELEGATE_AGENT_ALLOW_DANGER=1 before this point.
            return "bypassPermissions", [], [*NESTED_SPAWN_TOOLS]
        raise ValueError(f"unknown sandbox: {sandbox}")

    def _mcp_config(self, mcp_script: Path, mailbox: Path) -> str:
        config = {
            "mcpServers": {
                MCP_SERVER_KEY: {
                    "command": sys.executable,
                    "args": [str(mcp_script), "--mailbox", str(mailbox)],
                }
            }
        }
        return json.dumps(config, separators=(",", ":"))

    def _build(self, context: LaunchContext, session_id: str | None) -> LaunchSpec:
        executable = self._binary()
        if not executable:
            raise ValueError("Claude executable not found")
        metadata = context.metadata
        sandbox = metadata["sandbox"]
        permission_mode, allowed, disallowed = self._permission_flags(sandbox)
        # The prompt is passed as the first positional, immediately after `-p`.
        # Claude's `--allowedTools`/`--disallowedTools`/`--mcp-config` are variadic
        # (`<tools...>`) and would otherwise swallow a trailing positional prompt.
        command: list[str] = [
            str(executable),
            "-p", context.prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", metadata["model"],
            "--effort", metadata["reasoning_effort"],
        ]
        if session_id:
            command.extend(["--resume", session_id])
        command.extend([
            "--mcp-config", self._mcp_config(context.mcp_script, context.mailbox),
            "--strict-mcp-config",
            "--permission-mode", permission_mode,
        ])
        if sandbox == "danger-full-access":
            command.append("--dangerously-skip-permissions")
        if allowed:
            command.extend(["--allowedTools", *allowed])
        if disallowed:
            command.extend(["--disallowedTools", *disallowed])
        environment = os.environ.copy()
        environment["DELEGATE_AGENT_MAILBOX"] = str(context.mailbox)
        # The MCP config carries no secrets, so display_command mirrors the real
        # command; the mailbox path is local state, not a credential.
        return LaunchSpec(tuple(command), environment, tuple(command), True)

    def build_launch(self, context: LaunchContext) -> LaunchSpec:
        return self._build(context, None)

    def build_resume(self, context: ResumeContext) -> LaunchSpec:
        return self._build(context, context.session_id)

    def extract_session_id(self, payload: dict[str, Any]) -> str | None:
        for key in ("session_id", "sessionId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            if isinstance(value, dict):
                found = self.extract_session_id(value)
                if found:
                    return found
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        found = self.extract_session_id(item)
                        if found:
                            return found
        return None

    def normalize_event(self, payload: dict[str, Any]) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        event_type = payload.get("type", "backend.output")
        subtype = payload.get("subtype")

        if event_type == "system" and subtype == "init":
            session_id = self.extract_session_id(payload)
            if session_id:
                events.append(CanonicalEvent("session.started", {"session_id": session_id}))
            return events

        if event_type == "system" and subtype == "permission_denied":
            tool_name = payload.get("tool_name", "")
            message = payload.get("message", "tool call denied")
            if isinstance(tool_name, str) and tool_name.startswith(f"mcp__{MCP_SERVER_KEY}__"):
                events.append(CanonicalEvent("mailbox.failed", {"message": message}))
            else:
                events.append(CanonicalEvent("worker.warning", {"tool": tool_name, "message": message}))
            return events

        if event_type == "assistant":
            content = payload.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        events.append(CanonicalEvent("agent.message", {"text": str(block["text"]), "final": False}))
                    elif block.get("type") == "tool_use":
                        events.append(CanonicalEvent("tool.started", {
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                        }))
            return events

        if event_type == "user":
            content = payload.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        canonical = "tool.failed" if block.get("is_error") else "tool.completed"
                        events.append(CanonicalEvent(canonical, {
                            "id": block.get("tool_use_id"),
                            "content": block.get("content"),
                        }))
            return events

        if event_type == "result":
            text = str(payload.get("result") or "")
            events.append(CanonicalEvent("agent.message", {"text": text, "final": True}))
            return events

        return events

    def classify_exit(self, return_code: int, cancelled: bool) -> ExitResult:
        if cancelled:
            return ExitResult("cancelled", "Claude was cancelled")
        if return_code == 0:
            return ExitResult("completed", "Claude completed successfully")
        return ExitResult("failed", f"Claude exited with code {return_code}")
