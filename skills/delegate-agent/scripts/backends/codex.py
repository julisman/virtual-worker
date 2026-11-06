from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
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


REASONING_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"]
MAILBOX_TOOLS = ["check_inbox", "acknowledge", "report_progress", "request_input", "wait_for_response", "complete"]


def policies_path() -> Path:
    return Path(__file__).resolve().parents[2] / "policies" / "codex.json"


def load_policies() -> dict[str, dict[str, Any]]:
    return json.loads(policies_path().read_text())


def canonical_model_match(model: dict[str, Any], canonical_name: str) -> bool:
    needle = canonical_name.lower()
    values = [model.get("id"), model.get("model"), model.get("display_name")]
    return any(
        isinstance(value, str)
        and (value.lower() == needle or value.lower().endswith(f"--{needle}") or value.lower().startswith(f"{needle} "))
        for value in values
    )


class CodexBackend:
    id = "codex"

    def _binary(self) -> str | None:
        binary = os.environ.get("DELEGATE_AGENT_CODEX_BIN", "codex")
        return shutil.which(binary) if os.sep not in binary else binary

    def _version(self, binary: str) -> str | None:
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (result.stdout or result.stderr).strip() or None if result.returncode == 0 else None

    def _models(self, binary: str, include_hidden: bool = False, timeout: float = 10) -> dict[str, Any]:
        process = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        responses: Queue[dict[str, Any] | None] = Queue()
        stderr_lines: list[str] = []

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    responses.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            responses.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            stderr_lines.extend(line.rstrip() for line in process.stderr)

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        def send(payload: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()

        def wait_for(request_id: int, deadline: float) -> dict[str, Any]:
            while time.monotonic() < deadline:
                try:
                    message = responses.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                except Empty:
                    continue
                if message is None:
                    break
                if message.get("id") == request_id:
                    if "error" in message:
                        raise RuntimeError(json.dumps(message["error"]))
                    return message.get("result", {})
            detail = "\n".join(stderr_lines[-5:])
            raise RuntimeError(f"timed out waiting for Codex app-server response{': ' + detail if detail else ''}")

        deadline = time.monotonic() + timeout
        models: list[dict[str, Any]] = []
        try:
            send({
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "delegate-agent-skill", "title": "Delegate Agent Skill", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            })
            wait_for(1, deadline)
            send({"method": "initialized", "params": {}})
            cursor = None
            request_id = 2
            while True:
                send({"id": request_id, "method": "model/list", "params": {"includeHidden": include_hidden, "limit": 100, "cursor": cursor}})
                result = wait_for(request_id, deadline)
                models.extend(result.get("data", []))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
                request_id += 1
        finally:
            if process.stdin:
                process.stdin.close()
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

        normalized = [{
            "id": model.get("id"),
            "model": model.get("model"),
            "display_name": model.get("displayName"),
            "description": model.get("description"),
            "is_default": bool(model.get("isDefault")),
            "hidden": bool(model.get("hidden")),
            "default_reasoning_effort": model.get("defaultReasoningEffort"),
            "reasoning_efforts": [{"effort": option.get("reasoningEffort"), "description": option.get("description")} for option in model.get("supportedReasoningEfforts", [])],
            "input_modalities": model.get("inputModalities", []),
            "supports_personality": bool(model.get("supportsPersonality")),
            "upgrade": model.get("upgrade"),
        } for model in models]
        default = next((model for model in normalized if model["is_default"]), normalized[0] if normalized else None)
        return {"models": normalized, "default_model": default["model"] if default else None}

    def probe(self, include_hidden: bool = False) -> BackendCapabilities:
        executable = self._binary()
        if not executable:
            return BackendCapabilities(
                id=self.id,
                name="Codex CLI",
                installed=False,
                usable=False,
                executable=None,
                version=None,
                features=BackendFeatures(True, True, True, True, True),
                error="Codex executable not found",
                models_dynamic=True,
            )
        try:
            catalog = self._models(str(executable), include_hidden=include_hidden)
            models = tuple(catalog["models"])
            efforts = tuple(dict.fromkeys(
                option["effort"]
                for model in models
                for option in model.get("reasoning_efforts", [])
                if option.get("effort")
            ))
            return BackendCapabilities(
                id=self.id,
                name="Codex CLI",
                installed=True,
                usable=True,
                executable=str(executable),
                version=self._version(str(executable)),
                models=models,
                reasoning_efforts=efforts,
                features=BackendFeatures(True, True, True, True, True),
                models_dynamic=True,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return BackendCapabilities(
                id=self.id,
                name="Codex CLI",
                installed=True,
                usable=False,
                executable=str(executable),
                version=self._version(str(executable)),
                features=BackendFeatures(True, True, True, True, True),
                error=str(exc),
                models_dynamic=True,
            )

    def resolve(self, request: ResolutionRequest) -> dict[str, Any]:
        capabilities = self.probe(include_hidden=True)
        if not capabilities.usable:
            raise ValueError(capabilities.error or "Codex backend is unavailable")
        role = request.roles.get(request.role_id)
        if role is None:
            raise ValueError(f"unknown role: {request.role_id}")
        complexity = request.complexity or role["default_complexity"]
        profile = role["complexity_profiles"][complexity]
        policy_id = request.model_policy or profile["model_policy"]
        policies = load_policies()
        if policy_id not in policies:
            raise ValueError(f"unknown model policy: {policy_id}")
        models = list(capabilities.models)
        visible = [model for model in models if not model.get("hidden")]
        warnings: list[str] = []
        if request.model:
            selected = next((model for model in models if request.model in {model.get("id"), model.get("model")}), None)
            if selected is None:
                raise ValueError(f"model is not available in the current Codex catalog: {request.model}")
            model_source = "explicit"
        else:
            selected = None
            model_source = "catalog-default"
            for canonical_name in policies[policy_id]["preferred_canonical_models"]:
                matches = [model for model in visible if canonical_model_match(model, canonical_name)]
                if matches:
                    selected = next((model for model in matches if model.get("is_default")), matches[0])
                    model_source = f"policy:{policy_id}:{canonical_name}"
                    break
            if selected is None:
                selected = next((model for model in visible if model.get("is_default")), visible[0] if visible else None)
                warnings.append(f"no preferred model for policy {policy_id}; using catalog default")
        if selected is None:
            raise ValueError("Codex model catalog is empty")
        supported = [item["effort"] for item in selected.get("reasoning_efforts", []) if item.get("effort")]
        requested_effort = request.reasoning or profile["reasoning_effort"] or selected.get("default_reasoning_effort")
        if requested_effort in supported or not supported:
            reasoning = requested_effort
        else:
            requested_index = REASONING_ORDER.index(requested_effort) if requested_effort in REASONING_ORDER else 0
            reasoning = min(supported, key=lambda item: abs(REASONING_ORDER.index(item) - requested_index))
            warnings.append(f"reasoning {requested_effort} unsupported by {selected.get('model')}; using {reasoning}")
        return {
            "backend": self.id,
            "backend_source": "explicit",
            "role": role,
            "complexity": complexity,
            "complexity_source": "explicit" if request.complexity else "role-default",
            "model_policy": policy_id,
            "model_policy_source": "explicit" if request.model_policy else "role-complexity",
            "model": selected.get("model") or selected.get("id"),
            "model_source": model_source,
            "reasoning_effort": reasoning,
            "reasoning_source": "explicit" if request.reasoning else "role-complexity",
            "sandbox": request.sandbox or role["sandbox"],
            "sandbox_source": "explicit" if request.sandbox else "role-default",
            "warnings": warnings,
        }

    def _build(self, context: LaunchContext, session_id: str | None) -> LaunchSpec:
        executable = self._binary()
        if not executable:
            raise ValueError("Codex executable not found")
        metadata = context.metadata
        command = [str(executable), "exec"]
        if session_id:
            command.extend(["resume", session_id])
        command.extend(["--json", "--skip-git-repo-check", "--model", metadata["model"], "-c", "agents.enabled=false"])
        command.extend(["-c", f'model_reasoning_effort="{metadata["reasoning_effort"]}"'])
        command.extend(["-c", f'sandbox_mode="{metadata["sandbox"]}"', "-c", f'approval_policy="{metadata["approval_policy"]}"'])
        python_binary = sys.executable
        command.extend([
            "-c", f'mcp_servers.delegate_parent.command={json.dumps(python_binary)}',
            "-c", f'mcp_servers.delegate_parent.args={json.dumps([str(context.mcp_script), "--mailbox", str(context.mailbox)])}',
            "-c", 'mcp_servers.delegate_parent.default_tools_approval_mode="approve"',
            "-c", f"mcp_servers.delegate_parent.enabled_tools={json.dumps(MAILBOX_TOOLS)}",
            "-c", "mcp_servers.delegate_parent.required=true",
        ])
        if not session_id:
            command.extend(["--cd", metadata["cwd"]])
        command.append(context.prompt)
        environment = os.environ.copy()
        environment["DELEGATE_AGENT_MAILBOX"] = str(context.mailbox)
        return LaunchSpec(tuple(command), environment, tuple(command), True)

    def build_launch(self, context: LaunchContext) -> LaunchSpec:
        return self._build(context, None)

    def build_resume(self, context: ResumeContext) -> LaunchSpec:
        return self._build(context, context.session_id)

    def extract_session_id(self, payload: dict[str, Any]) -> str | None:
        for key in ("thread_id", "threadId", "session_id", "sessionId", "conversation_id"):
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
        item = payload.get("item")
        if self.extract_session_id(payload):
            events.append(CanonicalEvent("session.started", {"session_id": self.extract_session_id(payload)}))
        if isinstance(item, dict) and item.get("type") == "agent_message":
            events.append(CanonicalEvent("agent.message", {"text": str(item.get("text", "")), "final": True}))
        elif event_type in {"message.completed", "assistant.message"}:
            events.append(CanonicalEvent("agent.message", {"text": str(payload.get("text") or payload.get("message") or ""), "final": True}))
        if isinstance(item, dict) and item.get("type") == "mcp_tool_call" and item.get("server") == "delegate_parent":
            error = item.get("error")
            if isinstance(error, dict):
                events.append(CanonicalEvent("mailbox.failed", {"message": error.get("message", "MCP mailbox tool failed")}))
        if event_type.startswith("item.") and isinstance(item, dict):
            if event_type.endswith("started"):
                events.append(CanonicalEvent("tool.started", item))
            elif event_type.endswith("completed"):
                events.append(CanonicalEvent("tool.completed", item))
        return events

    def classify_exit(self, return_code: int, cancelled: bool) -> ExitResult:
        if cancelled:
            return ExitResult("cancelled", "Codex was cancelled")
        if return_code == 0:
            return ExitResult("completed", "Codex completed successfully")
        return ExitResult("failed", f"Codex exited with code {return_code}")
