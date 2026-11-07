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
from harness import base, codex, commands

FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, sys, time, os
    args = sys.argv[1:]
    if args == ["--version"]:
        print("9.9.9 (Claude Code fake)"); sys.exit(0)
    assert "-p" in args and args.index("--") > 0
    prompt = args[args.index("--") + 1]
    resume = args[args.index("--resume") + 1] if "--resume" in args else None
    forked = "--fork-session" in args
    sid = "f0rked-session" if forked else (resume or "new-session-1234")
    def emit(d): print(json.dumps(d), flush=True)
    emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd(), "model": "fake"})
    if prompt == "slow":
        time.sleep(30)
    if prompt == "big":
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 300000}]}})
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
    base.forget_version()
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
    return {"chome": chome, "proj": proj, "fake": fake, "app": tmp_path / "app"}


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
        real = base.get_settings
        monkeypatch.setattr(base, "get_settings", lambda: {**real(), "timeoutS": 1})
        job = await cc.start("slow", session="old-33")
        await asyncio.wait_for(job.task, 10)
        assert job.status == "failed" and "timed out" in job.error
        monkeypatch.setattr(base, "get_settings", real)
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
        assert st["harnesses"]["claude"]["available"] and st["settings"]["maxTurns"] == 60
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


# ─── Codex ───────────────────────────────────────────────────────────────────

FAKE_CODEX = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, sys, os
    args = sys.argv[1:]
    if args == ["--version"]:
        print("codex-cli 9.9.9"); sys.exit(0)
    assert args[0] == "exec"
    resume = args[1] == "resume"
    dd = args.index("--")
    sid = args[dd - 1] if resume else "new-codex-thread"
    prompt = args[dd + 1]
    out = args[args.index("-o") + 1]
    def emit(d): print(json.dumps(d), flush=True)
    emit({"type": "thread.started", "thread_id": sid})
    emit({"type": "turn.started"})
    emit({"type": "item.completed", "item": {"type": "command_execution", "command": "ls -la"}})
    if prompt == "slow":
        import time; time.sleep(30)
    if prompt == "fail":
        emit({"type": "turn.failed", "error": {"message": "sign in again"}}); sys.exit(1)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": f"codex did: {prompt}"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    open(out, "w").write(f"codex did: {prompt}\\n")
    assert ("-C" in args) == (not resume) and "--skip-git-repo-check" in args
''')


@pytest.fixture
def codex_home(home, tmp_path, monkeypatch):
    chome = tmp_path / "codex"
    monkeypatch.setattr(config, "CODEX_HOME", chome)
    fake = tmp_path / "codex-bin"
    fake.write_text(FAKE_CODEX)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(config, "CODEX_BIN", str(fake))
    base.forget_version()
    day = chome / "sessions" / "2026" / "11" / "07"
    _jsonl(day / "rollout-2026-11-07T01-00-00-0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl", [
        {"timestamp": "2026-11-07T01:00:00Z", "type": "session_meta", "payload": {"id": "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cwd": str(home["proj"]), "timestamp": "2026-11-07T01:00:00Z"}},
        {"timestamp": "2026-11-07T01:00:01Z", "type": "turn_context", "payload": {"cwd": str(home["proj"]), "model": "gpt-5.5"}},
        {"timestamp": "2026-11-07T01:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "refactor the parser"}},
        {"timestamp": "2026-11-07T01:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        {"timestamp": "2026-11-07T01:00:09Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "Parser refactored."}},
    ])
    return chome


def test_codex_discovery_and_run(codex_home, home):
    a = codex.availability()
    assert a["available"] and a["version"].startswith("codex-cli")
    ss = codex.list_sessions()
    assert len(ss) == 1 and ss[0]["id"] == "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert ss[0]["title"] == "refactor the parser" and ss[0]["lastAssistant"] == "Parser refactored."
    assert not ss[0]["live"] and ss[0]["harness"] == "codex"
    d = codex.session_detail(ss[0]["id"], turns=4)
    assert d["model"] == "gpt-5.5" and d["toolCalls"] == 1 and [t["role"] for t in d["turns"]] == ["user", "assistant"]
    assert codex.resolve_session("0199aa")["id"] == ss[0]["id"]

    async def main():
        job = await codex.start("tidy", session="0199aa")
        assert job.harness == "codex" and job.session_id == ss[0]["id"] and not job.forked
        assert codex.list_sessions()[0]["live"] is True        # busy while our job runs on it
        await asyncio.wait_for(job.task, 10)
        assert job.status == "done" and job.result == "codex did: tidy" and job.tool_calls == 1 and job.num_turns == 1
        assert "Codex finished job" in base.render_job(job)
        base.set_settings({"allowedRoots": [str(home["proj"].parent)]})
        job = await codex.start("hello", project=str(home["proj"]))
        await asyncio.wait_for(job.task, 10)
        assert job.status == "done" and job.new_session_id == "new-codex-thread"
        job = await codex.start("fail", session="0199aa")
        await asyncio.wait_for(job.task, 10)
        assert job.status == "failed" and "sign in again" in job.error
    asyncio.run(main())


def test_codex_commands_and_routes(codex_home, home, monkeypatch):
    sent = []

    class FakeManager:
        async def send(self, account_id, target, text, **kw):
            sent.append(text); return {"status": "sent"}
    import channels.manager as mgr
    monkeypatch.setattr(mgr, "manager", FakeManager())
    on = {"guardrails": {"codingHarness": True}}
    origin = {"accountId": "a", "conversationId": "c"}

    async def main():
        assert "Codex sessions" in await commands.handle("/codex list", origin=origin, agent=on)
        assert "Claude Code sessions" in await commands.handle("/code list", origin=origin, agent=on)
        assert "/codex" in await commands.handle("/code", origin=origin, agent=on)
        reply = await commands.handle("/codex run 0199aa add docs", origin=origin, agent=on)
        assert reply.startswith("Started job") and "Codex session" in reply
        job = list(base.JOBS.values())[-1]
        await asyncio.wait_for(job.task, 10)
        await asyncio.sleep(0.05)
        assert "codex did: add docs" in sent[-1]
    asyncio.run(main())

    monkeypatch.undo()                       # the real channel manager must start with the app
    monkeypatch.setattr(config, "APP_HOME", home["app"]); monkeypatch.setattr(config, "CLAUDE_HOME", home["chome"])
    monkeypatch.setattr(config, "CLAUDE_BIN", str(home["fake"])); monkeypatch.setattr(config, "CODEX_HOME", codex_home)
    monkeypatch.setattr(config, "CODEX_BIN", str(codex_home.parent / "codex-bin"))
    from fastapi.testclient import TestClient
    from app import app
    with TestClient(app) as c:
        st = c.get("/api/harness/status").json()
        assert st["harnesses"]["codex"]["available"] and st["harnesses"]["claude"]["available"]
        assert c.get("/api/harness/sessions?harness=codex").json()["sessions"][0]["harness"] == "codex"
        assert c.get("/api/harness/sessions?harness=nope").status_code == 400
        assert c.put("/api/harness/settings", json={"codexSandbox": "yolo"}).status_code == 400
        assert c.put("/api/harness/settings", json={"codexSandbox": "read-only"}).json()["codexSandbox"] == "read-only"
        r = c.post("/api/harness/run", json={"prompt": "x", "session": "0199aa", "harness": "codex"})
        assert r.status_code == 200 and r.json()["harness"] == "codex"


# ─── Review-pass regressions ───────────────────────────────────────────────────

def test_prompt_starting_with_dash_is_text_not_a_flag(home):
    async def main():
        job = await cc.start("--dangerously-skip-permissions please", session="old-33")
        await asyncio.wait_for(job.task, 10)
        assert job.status == "done" and job.result == "done: --dangerously-skip-permissions please in proj"
        argv = cc.ADAPTER.argv(job)
        assert argv[-2] == "--" and argv[-1].startswith("--dangerously")
    asyncio.run(main())


def test_large_event_lines_do_not_kill_the_run(home):
    async def main():
        job = await cc.start("big", session="old-33")
        await asyncio.wait_for(job.task, 10)
        assert job.status == "done" and job.result == "done: big in proj"
    asyncio.run(main())


def test_second_run_on_busy_session_is_refused(home, codex_home):
    async def main():
        job = await cc.start("slow", session="old-33")
        with pytest.raises(cc.HarnessError, match="already in progress"):
            await cc.start("again", session="old-33")
        await cc.stop(job.id)
        # Codex cannot fork: a session marked live (a run from here) is refused, never double-written
        job = await codex.start("slow", session="0199aa")
        with pytest.raises(base.HarnessError, match="already in progress"):
            await codex.start("again", session="0199aa")
        await base.stop(job.id)
        assert not job.forked
    asyncio.run(main())


def test_empty_job_id_never_matches(home):
    async def main():
        job = await cc.start("slow", session="old-33")
        assert base.get_job("") is None and base.get_job("   ") is None
        assert await base.stop("") is None and job.status == "running"
        await base.stop(job.id)
    asyncio.run(main())


def test_jobs_are_scoped_to_their_origin(home):
    alice = {"accountId": "acc", "conversationId": "alice", "agentId": "ag"}
    bob = {"accountId": "acc", "conversationId": "bob", "agentId": "ag"}

    async def main():
        job = await cc.start("hello", session="old-33", origin=alice)
        await asyncio.wait_for(job.task, 10)
        assert [j["id"] for j in base.list_jobs(scope=alice)] == [job.id]
        assert base.list_jobs(scope=bob) == [] and base.get_job(job.id, scope=bob) is None
        assert await base.stop(job.id, scope=bob) is None
        assert base.get_job(job.id, scope=None).id == job.id          # admin API sees all
        on = {"guardrails": {"codingHarness": True}}
        assert "from this chat" in await commands.handle(f"/code job {job.id}", origin=bob, agent=on)
        assert job.id in await commands.handle("/code jobs", origin=alice, agent=on)
        assert "Usage" in await commands.handle("/code stop", origin=alice, agent=on)
    asyncio.run(main())


def test_shutdown_kills_all_running_jobs_quickly(home):
    async def main():
        j1 = await cc.start("slow", session="old-33")
        j2 = await cc.start("slow", session="proj-ab")
        t0 = asyncio.get_event_loop().time()
        await base.shutdown()
        assert asyncio.get_event_loop().time() - t0 < 5
        for j in (j1, j2):
            assert j.status == "stopped" and j.proc.returncode is not None
    asyncio.run(main())


def test_resolve_session_uses_prefix_glob_not_full_scan(home, monkeypatch):
    calls = []
    real = cc.scan_transcript
    monkeypatch.setattr(cc, "scan_transcript", lambda p, turns=0: (calls.append(p.stem), real(p, turns))[1])
    assert cc.resolve_session("old-33")["id"] == "old-3333-bbbb"
    assert calls == ["old-3333-bbbb"]            # only the matched transcript was parsed
    assert cc.resolve_session("../etc") is None
