"""Coding harness (Claude Code): discovery over a fake ~/.claude, supervised
`claude -p` runs over a fake binary, /code commands, tools gating, routes."""

import asyncio
import json
import os
import stat
import textwrap
import time

import pytest

import config
from harness import claude_code as cc
from harness import commands

FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, sys, time, os
    args = sys.argv[1:]
    if args == ["--version"]:
        print("9.9.9 (Claude Code fake)"); sys.exit(0)
    prompt = args[args.index("-p") + 1]
    resume = args[args.index("--resume") + 1] if "--resume" in args else None
    forked = "--fork-session" in args
    sid = "f0rked-session" if forked else (resume or "new-session-1234")
    def emit(d): print(json.dumps(d), flush=True)
    emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd(), "model": "fake"})
    if prompt == "slow":
        time.sleep(30)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking..."},
          {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}})
    if prompt == "fail":
        emit({"type": "result", "subtype": "error", "is_error": True, "result": "boom", "session_id": sid, "num_turns": 1})
        sys.exit(1)
    emit({"type": "result", "subtype": "success", "is_error": False, "result": f"done: {prompt} in {os.path.basename(os.getcwd())}",
          "session_id": sid, "num_turns": 2, "total_cost_usd": 0.01})
''')


def _jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "app")
    chome = tmp_path / "claude"
    monkeypatch.setattr(config, "CLAUDE_HOME", chome)
    fake = tmp_path / "claude-bin"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(config, "CLAUDE_BIN", str(fake))
    cc._version_cache.update(bin=None, version=None, at=0.0)
    cc.JOBS.clear()
    proj = tmp_path / "proj"; proj.mkdir()
    (chome / "sessions").mkdir(parents=True)
    (chome / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "live-1111-aaaa", "cwd": str(proj), "name": "proj-ab",
        "kind": "interactive", "status": "idle", "startedAt": 1794000000000, "updatedAt": 1794000500000}))
    (chome / "sessions" / "999999.json").write_text(json.dumps({"pid": 999999, "sessionId": "dead-2222", "cwd": str(proj)}))
    slug = chome / "projects" / "-tmp-proj"
    _jsonl(slug / "live-1111-aaaa.jsonl", [
        {"type": "ai-title", "aiTitle": "Fix the login bug", "sessionId": "live-1111-aaaa"},
        {"type": "user", "message": {"role": "user", "content": "fix the login bug"}, "timestamp": "2026-11-07T01:00:00Z", "cwd": str(proj), "gitBranch": "main"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "tool_use", "name": "Read", "input": {}}]}, "timestamp": "2026-11-07T01:00:05Z"},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]}, "timestamp": "2026-11-07T01:00:06Z"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed it in auth.py"}]}, "timestamp": "2026-11-07T01:00:09Z"},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<system hint>"}, "timestamp": "2026-11-07T01:00:10Z"},
        {"type": "last-prompt", "lastPrompt": "fix the login bug", "sessionId": "live-1111-aaaa"},
    ])
    _jsonl(slug / "old-3333-bbbb.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "write tests"}, "timestamp": "2026-11-06T01:00:00Z", "cwd": str(proj)},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Added 3 tests"}]}, "timestamp": "2026-11-06T01:00:09Z"},
    ])
    os.utime(slug / "old-3333-bbbb.jsonl", (1, 1))
    return {"chome": chome, "proj": proj, "fake": fake}


def test_availability_and_settings(home):
    a = cc.availability()
    assert a["available"] and a["version"].startswith("9.9.9")
    s = cc.set_settings({"maxTurns": 9999, "timeoutS": 1, "allowedRoots": [str(home["proj"])]})
    assert s["maxTurns"] == 500 and s["timeoutS"] == 30 and s["allowedRoots"] == [str(home["proj"])]
    with pytest.raises(cc.HarnessError):
        cc.set_settings({"permissionMode": "yolo"})


def test_discovery_merges_live_and_transcripts(home):
    live = cc.live_sessions()
    assert [s["id"] for s in live] == ["live-1111-aaaa"]          # dead pid skipped
    sessions = cc.list_sessions()
    assert [s["id"] for s in sessions] == ["live-1111-aaaa", "old-3333-bbbb"]
    top = sessions[0]
    assert top["live"] and top["status"] == "idle" and top["name"] == "proj-ab"
    assert top["title"] == "Fix the login bug" and top["lastAssistant"] == "Fixed it in auth.py"
    assert top["messages"] == 3          # meta + tool-result-only user turns excluded
    old = sessions[1]
    assert not old["live"] and old["status"] == "stopped" and old["title"] == "write tests"
    d = cc.session_detail("live-1111-aaaa", turns=4)
    assert d["toolCalls"] == 1 and d["gitBranch"] == "main"
    assert [t["role"] for t in d["turns"]] == ["user", "assistant", "assistant"]
    assert cc.resolve_session("proj-ab")["id"] == "live-1111-aaaa"
    assert cc.resolve_session("old-33")["id"] == "old-3333-bbbb"
    assert cc.resolve_session("nope") is None
    text = cc.render_sessions(sessions)
    assert "proj-ab" in text and "● idle" in text and "○ stopped" in text


def test_run_resumes_dead_session_and_forks_live_one(home):
    async def main():
        done = []

        async def on_done(job):
            done.append(job.id)
        job = await cc.start("hello", session="old-33", on_done=on_done)
        assert job.session_id == "old-3333-bbbb" and not job.forked and job.cwd == str(home["proj"])
        await asyncio.wait_for(job.task, 10)
        assert job.status == "done" and job.result == "done: hello in proj"
        assert job.tool_calls == 1 and job.num_turns == 2 and job.cost_usd == 0.01
        assert job.new_session_id == "old-3333-bbbb" and done == [job.id]
        assert any(e["kind"] == "tool" and "Read README.md" in e["text"] for e in job.events)
        job2 = await cc.start("hello", session="proj-ab")
        assert job2.forked and job2.session_id == "live-1111-aaaa"
        await asyncio.wait_for(job2.task, 10)
        assert job2.status == "done" and job2.new_session_id == "f0rked-session"
        assert "forked from live-111" in cc.render_job(job2)
    asyncio.run(main())


def test_new_session_requires_allowed_root(home):
    async def main():
        with pytest.raises(cc.HarnessError, match="allowed roots"):
            await cc.start("hi", project=str(home["proj"]))
        cc.set_settings({"allowedRoots": [str(home["proj"].parent)]})
        job = await cc.start("hi", project=str(home["proj"]))
        await asyncio.wait_for(job.task, 10)
        assert job.status == "done" and job.new_session_id == "new-session-1234"
        with pytest.raises(cc.HarnessError, match="not found"):
            await cc.start("hi", project="/nonexistent/dir")
        with pytest.raises(cc.HarnessError, match="give a session"):
            await cc.start("hi")
    asyncio.run(main())


def test_failure_timeout_and_stop(home, monkeypatch):
    async def main():
        job = await cc.start("fail", session="old-33")
        await asyncio.wait_for(job.task, 10)
        assert job.status == "failed" and "boom" in job.error
        real = cc.get_settings
        monkeypatch.setattr(cc, "get_settings", lambda: {**real(), "timeoutS": 1})
        job = await cc.start("slow", session="old-33")
        await asyncio.wait_for(job.task, 10)
        assert job.status == "failed" and "timed out" in job.error
        monkeypatch.setattr(cc, "get_settings", real)
        job = await cc.start("slow", session="old-33")
        await asyncio.sleep(0.3)
        stopped = await cc.stop(job.id)
        assert stopped.status == "stopped" and job.proc.returncode is not None
        assert cc.get_job(job.id[:4]).id == job.id
    asyncio.run(main())


def test_code_commands_gate_and_flow_back(home, monkeypatch):
    sent = []

    class FakeManager:
        async def send(self, account_id, target, text, **kw):
            sent.append((account_id, target, text))
            return {"status": "sent"}
    import channels.manager as mgr
    monkeypatch.setattr(mgr, "manager", FakeManager())
    origin = {"accountId": "acc", "conversationId": "628", "agentId": "ag", "kind": "telegram"}

    async def main():
        assert await commands.handle("hello", origin=origin, agent={}) is None
        off = await commands.handle("/code list", origin=origin, agent={"guardrails": {}})
        assert "off for this worker" in off
        on = {"guardrails": {"codingHarness": True}}
        assert "proj-ab" in await commands.handle("/code list", origin=origin, agent=on)
        assert "Fix the login bug" in await commands.handle("/code show proj-ab", origin=origin, agent=on)
        assert "Usage" in await commands.handle("/code run", origin=origin, agent=on)
        reply = await commands.handle("/code run old-33 make it faster", origin=origin, agent=on)
        assert reply.startswith("Started job") and "post the result here" in reply
        job = next(iter(cc.JOBS.values()))
        await asyncio.wait_for(job.task, 10)
        await asyncio.sleep(0.05)
        assert sent and sent[-1][0] == "acc" and sent[-1][1] == "628"
        assert "done: make it faster" in sent[-1][2] and "finished job" in sent[-1][2]
        assert job.id in await commands.handle("/code jobs", origin=origin, agent=on)
        assert "Can't do that" in await commands.handle("/code run nope x", origin=origin, agent=on)
        assert "/code list" in await commands.handle("/code", origin=origin, agent=on)
    asyncio.run(main())


def test_tools_gated_by_guardrail(home):
    from agent.harness_tools import make_harness_tools
    tools = make_harness_tools("ag")
    assert set(tools) == {"code_sessions", "code_session", "code_run", "code_jobs", "code_stop"}
    out = asyncio.run(tools["code_sessions"]["fn"](limit=5))
    assert "proj-ab" in out
    assert "error:" in asyncio.run(tools["code_run"]["fn"](prompt="x", session="nope"))
    from storage.agent_store import _guardrails
    assert _guardrails(json.dumps({})).get("codingHarness") is False
    assert _guardrails(json.dumps({"codingHarness": 1}))["codingHarness"] is True


def test_routes(home):
    from fastapi.testclient import TestClient
    from app import app
    with TestClient(app) as c:
        st = c.get("/api/harness/status").json()
        assert st["available"] and st["settings"]["maxTurns"] == 60
        assert c.put("/api/harness/settings", json={"permissionMode": "bad"}).status_code == 400
        assert c.put("/api/harness/settings", json={"allowedRoots": [str(home["proj"])]}).json()["allowedRoots"] == [str(home["proj"])]
        ss = c.get("/api/harness/sessions").json()["sessions"]
        assert ss[0]["id"] == "live-1111-aaaa"
        assert c.get("/api/harness/sessions/proj-ab?turns=2").json()["title"] == "Fix the login bug"
        assert c.get("/api/harness/sessions/zzz").status_code == 404
        r = c.post("/api/harness/run", json={"prompt": "ping", "session": "old-33"})
        assert r.status_code == 200 and r.json()["status"] == "running"
        jid = r.json()["id"]
        j = r.json()
        for _ in range(100):
            j = c.get(f"/api/harness/jobs/{jid}").json()
            if j["status"] != "running":
                break
            time.sleep(0.05)
        assert j["status"] == "done" and j["result"].startswith("done: ping")
        assert c.get("/api/harness/jobs").json()["jobs"][0]["id"] == jid
        assert c.post("/api/harness/run", json={"prompt": "x", "project": "/nope"}).status_code == 400


def test_channel_manager_routes_code_commands(home, monkeypatch):
    """A /code message on a channel is answered by the harness, not the model."""
    from channels import base
    from channels.base import ChannelAdapter, InboundMessage
    from channels.manager import ChannelManager
    import channels.manager as mgr
    from storage import agent_store, chat_channel_store as store

    class Fake(ChannelAdapter):
        kind = "fakeh"; label = "FakeH"; max_text_len = 4000
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw); self.sent = []
        async def start(self): self._set_status("open", me="me")
        async def stop(self): self._set_status("off")
        async def send(self, target, text, *, reply_to=None, attachments=None):
            self.sent.append(text); return "m1"

    base.registry.register(Fake)
    agent = agent_store.create_agent({"name": "Dev", "persona": "x", "guardrails": {"codingHarness": True}})
    acct = store.create_account("fakeh", "a", enabled=True, default_agent_id=agent["id"],
                                config={"dmPolicy": "open", "debounceMs": 0, "ackAfterMs": 0})
    m = ChannelManager(); m._started = True
    monkeypatch.setattr(mgr, "manager", m)

    async def boom(a, messages, session_id):
        raise AssertionError("the model must not run for /code")
    monkeypatch.setattr(mgr, "execute_turn", boom)

    async def main():
        await m.start_account(acct["id"])
        ad = m.adapter(acct["id"])
        await m.handle_inbound(InboundMessage(channel="fakeh", account_id=acct["id"], conversation_id="c", kind="dm",
                                             sender_id="u", text="/code list", event_id="e1"))
        assert ad.sent and "proj-ab" in ad.sent[-1]
        await m.handle_inbound(InboundMessage(channel="fakeh", account_id=acct["id"], conversation_id="c", kind="dm",
                                             sender_id="u", text="/code run old-33 tidy up", event_id="e2"))
        assert ad.sent[-1].startswith("Started job")
        job = [j for j in cc.JOBS.values()][-1]
        await asyncio.wait_for(job.task, 10)
        await asyncio.sleep(0.05)
        assert "done: tidy up" in ad.sent[-1]            # result flowed back into the same chat
        await m.handle_inbound(InboundMessage(channel="fakeh", account_id=acct["id"], conversation_id="c", kind="dm",
                                             sender_id="u", text="/help", event_id="e3"))
        assert "/code" in ad.sent[-1]
        await m.stop()
    asyncio.run(main())
