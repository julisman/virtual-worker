from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
CLI = SCRIPTS / "agent.py"
sys.path.insert(0, str(SCRIPTS))

from backends.base import BackendCapabilities, BackendFeatures, LaunchContext, ResolutionRequest  # noqa: E402
from backends.claude import ClaudeBackend  # noqa: E402
from backends.codex import CodexBackend  # noqa: E402
from core_lib import acknowledge_messages, check_messages, enqueue_message, initialize_database, load_roles  # noqa: E402


def fake_capabilities() -> BackendCapabilities:
    models = (
        {"id": "provider--gpt-5.6-luna", "model": "provider--gpt-5.6-luna", "display_name": "gpt-5.6-luna", "hidden": False, "is_default": True, "reasoning_efforts": [{"effort": effort} for effort in ["low", "medium", "high", "xhigh"]]},
        {"id": "provider--gpt-5.6-terra", "model": "provider--gpt-5.6-terra", "display_name": "gpt-5.6-terra", "hidden": False, "is_default": False, "reasoning_efforts": [{"effort": effort} for effort in ["low", "medium", "high", "xhigh"]]},
    )
    return BackendCapabilities("codex", "Codex CLI", True, True, "/bin/codex", "test", models, ("low", "medium", "high", "xhigh"), BackendFeatures(True, True, True, True, True), models_dynamic=True)


def test_mailbox_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "mailbox.db"
    initialize_database(database)
    message_id = enqueue_message(database, "instruction", {"text": "keep the API stable"}, "urgent")
    assert check_messages(database)[0]["id"] == message_id
    assert acknowledge_messages(database, [message_id]) == 1
    assert check_messages(database) == []


def test_codex_resolution_uses_backend_policy(tmp_path: Path, monkeypatch) -> None:
    backend = CodexBackend()
    monkeypatch.setattr(backend, "probe", lambda include_hidden=False: fake_capabilities())
    roles = load_roles(tmp_path)
    simple = backend.resolve(ResolutionRequest(roles, "implementer", complexity="simple"))
    assert simple["model"] == "provider--gpt-5.6-luna"
    complex_review = backend.resolve(ResolutionRequest(roles, "reviewer", complexity="complex"))
    assert complex_review["model"] == "provider--gpt-5.6-terra"
    assert complex_review["reasoning_effort"] == "xhigh"


def test_codex_launch_uses_private_mailbox_tools(tmp_path: Path, monkeypatch) -> None:
    backend = CodexBackend()
    monkeypatch.setattr(backend, "_binary", lambda: "/bin/codex")
    metadata = {"model": "test", "reasoning_effort": "medium", "sandbox": "workspace-write", "approval_policy": "never", "cwd": str(tmp_path)}
    spec = backend.build_launch(LaunchContext(metadata, tmp_path / "mailbox.db", "task", SCRIPTS / "parent_mcp.py"))
    joined = " ".join(spec.command)
    assert "agents.enabled=false" in joined
    assert "mcp_servers.delegate_parent.default_tools_approval_mode" in joined
    assert "check_inbox" in joined


def test_claude_adapter_is_discoverable() -> None:
    capabilities = ClaudeBackend().probe()
    assert capabilities.id == "claude"
    # The Claude adapter is implemented; usability tracks whether the CLI is installed.
    assert capabilities.usable == capabilities.installed


def test_spawn_and_collect_fake_codex(tmp_path: Path) -> None:
    fake = tmp_path / "fake-codex"
    fake.write_text("""#!/usr/bin/env python3
import json, sys
if len(sys.argv) > 1 and sys.argv[1] == '--version': print('codex-cli test')
elif len(sys.argv) > 1 and sys.argv[1] == 'app-server':
    for line in sys.stdin:
        request=json.loads(line)
        if request.get('method') == 'initialize': print(json.dumps({'id':request['id'],'result':{}}), flush=True)
        elif request.get('method') == 'model/list':
            model={'id':'test-model','model':'test-model','displayName':'Test','hidden':False,'isDefault':True,'defaultReasoningEffort':'medium','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}
            print(json.dumps({'id':request['id'],'result':{'data':[model],'nextCursor':None}}), flush=True)
else:
    print(json.dumps({'type':'thread.started','thread_id':'thread-test'}), flush=True)
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}), flush=True)
""")
    fake.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = {**os.environ, "DELEGATE_AGENT_HOME": str(tmp_path / "state"), "DELEGATE_AGENT_CODEX_BIN": str(fake)}
    spawned = subprocess.run([sys.executable, str(CLI), "spawn", "--backend", "codex", "--cwd", str(workspace), "--model", "test-model", "--prompt", "Test"], capture_output=True, text=True, env=environment, check=True)
    spawn_result = json.loads(spawned.stdout)
    agent_id = spawn_result["agent_id"]
    group_id = spawn_result["group_id"]
    deadline = time.monotonic() + 5
    while True:
        result = subprocess.run([sys.executable, str(CLI), "status", agent_id], capture_output=True, text=True, env=environment, check=True)
        status = json.loads(result.stdout)
        if status["status"] == "completed" or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    assert status["backend"] == "codex"
    assert status["session_id"] == "thread-test"
    final = subprocess.run([sys.executable, str(CLI), "result", agent_id], capture_output=True, text=True, env=environment, check=True)
    assert json.loads(final.stdout)["final_message"] == "done"
    supervised = subprocess.run([sys.executable, str(CLI), "supervise", group_id, "--timeout", "0"], capture_output=True, text=True, env=environment, check=True)
    group_result = json.loads(supervised.stdout)
    assert group_result["complete"] is True
    assert group_result["action_required"] is False
    assert any(event["type"] == "worker.finished" for event in group_result["events"])
    steered = subprocess.run([sys.executable, str(CLI), "steer", agent_id, "Use this on follow-up"], capture_output=True, text=True, env=environment, check=True)
    steer_result = json.loads(steered.stdout)
    assert steer_result["queued"] is True
    assert steer_result["delivery"] == "next_resume"
    assert steer_result["worker_active"] is False
    assert steer_result["warning"]
    interrupted = subprocess.run([sys.executable, str(CLI), "steer", agent_id, "stop", "--interrupt"], capture_output=True, text=True, env=environment)
    assert interrupted.returncode == 2
    assert "cannot interrupt terminal worker" in json.loads(interrupted.stdout)["error"]
