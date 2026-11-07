"""``code_*`` tools: let a worker look at and drive Claude Code sessions.

Opt-in per worker (``guardrails.codingHarness``). ``code_run`` starts a
supervised ``claude -p`` run and returns at once; inside a chat-channel turn
the finished result is posted back into that chat automatically, elsewhere
the worker (or the person) polls ``code_jobs``.
"""

from __future__ import annotations

from channels.manager import CURRENT_INBOUND
from harness import claude_code as cc
from harness.commands import deliver_to


def _origin(agent_id: str) -> dict:
    cur = CURRENT_INBOUND.get()
    if not cur:
        return {"agentId": agent_id, "source": "tool"}
    return {"accountId": cur.account_id, "conversationId": cur.conversation_id,
            "agentId": agent_id, "sessionKey": cur.session_key, "kind": cur.channel, "source": "tool"}


def make_harness_tools(agent_id: str) -> dict[str, dict]:
    async def code_sessions(limit: int = 10, project: str = "") -> str:
        return cc.render_sessions(cc.list_sessions(limit=max(1, min(int(limit or 10), 30)),
                                                   project=project or None))

    async def code_session(session: str = "", turns: int = 6) -> str:
        s = cc.resolve_session(session)
        if not s:
            return f"no Claude Code session matches {session!r}; call code_sessions first"
        d = cc.session_detail(s["id"], turns=max(0, min(int(turns or 6), 20)))
        return cc.render_detail(d) if d else "session not found"

    async def code_run(prompt: str = "", session: str = "", project: str = "") -> str:
        origin = _origin(agent_id)
        try:
            job = await cc.start(prompt, session=session or None, project=project or None,
                                 origin=origin, on_done=deliver_to(origin) if origin.get("conversationId") else None)
        except cc.HarnessError as exc:
            return f"error: {exc}"
        tail = (" The result will be posted into this chat when it finishes."
                if origin.get("conversationId") else " Check it with code_jobs(job=...).")
        where = f"session {job.session_id[:8]}" + (" (forked, it is open in a terminal)" if job.forked else "") \
            if job.session_id else f"a new session in {job.cwd}"
        return f"started job {job.id} on {where}.{tail}"

    async def code_jobs(job: str = "") -> str:
        if job:
            j = cc.get_job(job)
            return cc.render_job(j) if j else f"no job {job!r}"
        jobs = cc.list_jobs(limit=10)
        if not jobs:
            return "no runs yet"
        return "\n".join(f"{j['id']} · {j['status']} · {j['toolCalls']} tool calls · {cc._short(j['prompt'], 80)}" for j in jobs)

    async def code_stop(job: str = "") -> str:
        j = await cc.stop(job)
        return f"job {j.id} {j.status}" if j else f"no job {job!r}"

    return {
        "code_sessions": {"fn": code_sessions, "schema": {"type": "function", "function": {
            "name": "code_sessions",
            "description": ("List the Claude Code sessions on this machine, newest first: which are live "
                            "(busy or idle), their project folder, title and last activity. Use the name or "
                            "id prefix it prints with the other code_* tools."),
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "max sessions (default 10)"},
                "project": {"type": "string", "description": "only sessions whose project path contains this"}}}}}},
        "code_session": {"fn": code_session, "schema": {"type": "function", "function": {
            "name": "code_session",
            "description": "Show one Claude Code session: status, project, and its last exchanges.",
            "parameters": {"type": "object", "properties": {
                "session": {"type": "string", "description": "session name, id, or id prefix"},
                "turns": {"type": "integer", "description": "how many recent messages to show (default 6)"}},
                "required": ["session"]}}}},
        "code_run": {"fn": code_run, "schema": {"type": "function", "function": {
            "name": "code_run",
            "description": ("Send a prompt to Claude Code: continue an existing session (give 'session') or "
                            "start a new one in a project directory (give 'project'). Returns immediately with "
                            "a job id; the run can take minutes. In a chat channel the result is posted back "
                            "automatically, otherwise check code_jobs."),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "what Claude Code should do"},
                "session": {"type": "string", "description": "session name, id or prefix to continue"},
                "project": {"type": "string", "description": "directory for a new session (must be under an allowed root)"}},
                "required": ["prompt"]}}}},
        "code_jobs": {"fn": code_jobs, "schema": {"type": "function", "function": {
            "name": "code_jobs",
            "description": "Status of runs started with code_run; give a job id for its result.",
            "parameters": {"type": "object", "properties": {
                "job": {"type": "string", "description": "job id (optional)"}}}}}},
        "code_stop": {"fn": code_stop, "schema": {"type": "function", "function": {
            "name": "code_stop",
            "description": "Stop a running Claude Code job started with code_run.",
            "parameters": {"type": "object", "properties": {
                "job": {"type": "string", "description": "job id"}}, "required": ["job"]}}}},
    }
