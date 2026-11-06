from __future__ import annotations

import json
from pathlib import Path
import sys


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
FIXTURE = SKILL_ROOT / "tests" / "fixtures" / "claude-events.jsonl"
sys.path.insert(0, str(SCRIPTS))

from backends.base import LaunchContext, ResolutionRequest, ResumeContext  # noqa: E402
from backends.claude import MAILBOX_ALLOW, ClaudeBackend  # noqa: E402
from core_lib import load_roles  # noqa: E402


def load_fixture() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip() and not line.startswith("#")]


def build_metadata(tmp_path: Path, sandbox: str = "workspace-write") -> dict:
    return {"model": "sonnet", "reasoning_effort": "high", "sandbox": sandbox, "cwd": str(tmp_path), "approval_policy": "never"}


def make_launch(tmp_path: Path, sandbox: str = "workspace-write") -> list[str]:
    backend = ClaudeBackend()
    context = LaunchContext(build_metadata(tmp_path, sandbox), tmp_path / "mailbox.db", "do work", SCRIPTS / "parent_mcp.py")
    return list(backend.build_launch(context).command)


# --- probe / discovery -------------------------------------------------------

def test_probe_reports_usable_when_installed(monkeypatch) -> None:
    backend = ClaudeBackend()
    monkeypatch.setattr(backend, "_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(backend, "_version", lambda executable: "2.1.227 (Claude Code)")
    capabilities = backend.probe()
    assert capabilities.id == "claude"
    assert capabilities.usable is True
    assert capabilities.models_dynamic is False
    assert capabilities.features.resume is True
    assert capabilities.features.session_storage_owner == "backend"


def test_probe_reports_unusable_when_missing(monkeypatch) -> None:
    backend = ClaudeBackend()
    monkeypatch.setattr(backend, "_binary", lambda: None)
    capabilities = backend.probe()
    assert capabilities.installed is False
    assert capabilities.usable is False
    assert "not found" in (capabilities.error or "")


# --- resolution --------------------------------------------------------------

def test_resolution_uses_backend_policy(tmp_path: Path, monkeypatch) -> None:
    backend = ClaudeBackend()
    monkeypatch.setattr(backend, "probe", lambda include_hidden=False: _usable_caps(backend))
    roles = load_roles(tmp_path)
    simple = backend.resolve(ResolutionRequest(roles, "implementer", complexity="simple"))
    assert simple["model"] == "haiku"  # economy policy
    capable = backend.resolve(ResolutionRequest(roles, "reviewer", complexity="complex"))
    assert capable["model"] == "opus"  # capable policy prefers opus
    # xhigh is not a Claude effort -> clamp to high, with a warning (never silent)
    assert capable["reasoning_effort"] == "high"
    assert any("xhigh" in warning for warning in capable["warnings"])


def test_explicit_full_model_id_passes_through(tmp_path: Path, monkeypatch) -> None:
    backend = ClaudeBackend()
    monkeypatch.setattr(backend, "probe", lambda include_hidden=False: _usable_caps(backend))
    roles = load_roles(tmp_path)
    resolved = backend.resolve(ResolutionRequest(roles, "implementer", model="claude-sonnet-5-20991231"))
    assert resolved["model"] == "claude-sonnet-5-20991231"
    assert resolved["model_source"] == "explicit"


def _usable_caps(backend: ClaudeBackend):
    from backends.base import BackendCapabilities
    from backends.claude import CLAUDE_EFFORTS, STATIC_MODELS
    return BackendCapabilities("claude", "Claude Code", True, True, "/usr/bin/claude", "2.1.227", STATIC_MODELS, CLAUDE_EFFORTS, backend._features(), models_dynamic=False)


# --- launch / permissions ----------------------------------------------------

def test_launch_uses_private_mailbox_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeBackend, "_binary", lambda self: "/usr/bin/claude")
    command = make_launch(tmp_path, "read-only")
    joined = " ".join(command)
    assert "--strict-mcp-config" in command
    for tool in MAILBOX_ALLOW:
        assert tool in command
    assert "mcp_servers" not in joined  # Claude does not use Codex -c wiring
    # mailbox MCP server is configured via --mcp-config JSON
    config_index = command.index("--mcp-config") + 1
    config = json.loads(command[config_index])
    assert "delegate_parent" in config["mcpServers"]


def test_launch_disables_native_subagents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeBackend, "_binary", lambda self: "/usr/bin/claude")
    for sandbox in ("read-only", "workspace-write", "danger-full-access"):
        command = make_launch(tmp_path, sandbox)
        disallowed = command[command.index("--disallowedTools") + 1:]
        assert "Task" in disallowed


def test_read_only_does_not_use_plan_mode(tmp_path: Path, monkeypatch) -> None:
    # plan mode blocks the mailbox MCP tools; read-only must use default + allowlist.
    monkeypatch.setattr(ClaudeBackend, "_binary", lambda self: "/usr/bin/claude")
    command = make_launch(tmp_path, "read-only")
    mode = command[command.index("--permission-mode") + 1]
    assert mode == "default"
    disallowed = command[command.index("--disallowedTools") + 1:]
    assert "Write" in disallowed and "Bash" in disallowed


def test_workspace_write_allows_edits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeBackend, "_binary", lambda self: "/usr/bin/claude")
    command = make_launch(tmp_path, "workspace-write")
    mode = command[command.index("--permission-mode") + 1]
    assert mode == "acceptEdits"
    allowed = command[command.index("--allowedTools") + 1:command.index("--disallowedTools")]
    assert "Write" in allowed and "Edit" in allowed


def test_danger_mode_sets_skip_permissions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeBackend, "_binary", lambda self: "/usr/bin/claude")
    command = make_launch(tmp_path, "danger-full-access")
    assert "--dangerously-skip-permissions" in command


def test_resume_uses_original_backend_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeBackend, "_binary", lambda self: "/usr/bin/claude")
    backend = ClaudeBackend()
    context = ResumeContext(build_metadata(tmp_path), tmp_path / "mailbox.db", "follow up", SCRIPTS / "parent_mcp.py", session_id="sess-123")
    command = list(backend.build_resume(context).command)
    assert "--resume" in command
    assert command[command.index("--resume") + 1] == "sess-123"


# --- event normalization (against real captured fixture) --------------------

def test_session_id_can_be_discovered() -> None:
    backend = ClaudeBackend()
    session_ids = [backend.extract_session_id(event) for event in load_fixture()]
    assert all(isinstance(sid, str) and sid for sid in session_ids)  # every event carries a session id


def test_final_message_is_canonical() -> None:
    backend = ClaudeBackend()
    result_event = next(event for event in load_fixture() if event.get("type") == "result")
    canonical = backend.normalize_event(result_event)
    finals = [event for event in canonical if event.type == "agent.message" and event.data.get("final")]
    assert len(finals) == 1
    assert finals[0].data["text"] == result_event["result"]


def test_failed_mailbox_tool_is_canonical() -> None:
    backend = ClaudeBackend()
    denied = next(event for event in load_fixture() if event.get("subtype") == "permission_denied")
    canonical = backend.normalize_event(denied)
    assert any(event.type == "mailbox.failed" for event in canonical)


def test_tool_use_and_result_normalize() -> None:
    backend = ClaudeBackend()
    types = [event.type for fixture_event in load_fixture() for event in backend.normalize_event(fixture_event)]
    assert "session.started" in types
    assert "tool.started" in types
    assert "tool.completed" in types


def test_classify_exit() -> None:
    backend = ClaudeBackend()
    assert backend.classify_exit(0, False).status == "completed"
    assert backend.classify_exit(1, False).status == "failed"
    assert backend.classify_exit(0, True).status == "cancelled"
