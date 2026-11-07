"""``code_*`` tools: let a worker look at and drive Claude Code or Codex sessions.

Opt-in per worker (``guardrails.codingHarness``). Every tool takes a
``harness`` argument (``claude``, the default, or ``codex``). ``code_run``
starts a supervised run and returns at once; inside a chat-channel turn the
finished result is posted back into that chat automatically, elsewhere the
worker (or the person) polls ``code_jobs``.
"""

from __future__ import annotations

from channels.manager import CURRENT_INBOUND
from harness import base
from harness import claude_code, codex  # noqa: F401 — registers both adapters
from harness.commands import deliver_to

_HARNESS_PARAM = {"type": "string", "enum": ["claude", "codex"],
                  "description": "which coding agent: 'claude' (Claude Code, default) or 'codex' (OpenAI Codex)"}


def _origin(agent_id: str) -> dict:
    cur = CURRENT_INBOUND.get()
    if not cur:
        return {"agentId": agent_id, "source": "tool"}
    return {"accountId": cur.account_id, "conversationId": cur.conversation_id,
            "agentId": agent_id, "sessionKey": cur.session_key, "kind": cur.channel, "source": "tool"}


def make_harness_tools(agent_id: str) -> dict[str, dict]:
    async def code_sessions(limit: int = 10, project: str = "", harness: str = "claude") -> str:
        try:
            ad = base.get(harness)
        except base.HarnessError as exc:
            return f"error: {exc}"
        return base.render_sessions(ad.list_sessions(limit=max(1, min(int(limit or 10), 30)), project=project or None),
                                    ad.label, "/code" if ad.key == "claude" else "/codex")

    async def code_session(session: str = "", turns: int = 6, harness: str = "claude") -> str:
        try:
            ad = base.get(harness)
        except base.HarnessError as exc:
            return f"error: {exc}"
        s = ad.resolve_session(session)
        if not s:
            return f"no {ad.label} session matches {session!r}; call code_sessions first"
        d = ad.session_detail(s["id"], turns=max(0, min(int(turns or 6), 20)))
        return base.render_detail(d) if d else "session not found"

    async def code_run(prompt: str = "", session: str = "", project: str = "", harness: str = "claude") -> str:
        origin = _origin(agent_id)
        try:
            ad = base.get(harness)
            job = await base.start_job(ad, prompt, session=session or None, project=project or None,
                                       origin=origin, on_done=deliver_to(origin) if origin.get("conversationId") else None)
        except base.HarnessError as exc:
            return f"error: {exc}"
        tail = (" The result will be posted into this chat when it finishes."
                if origin.get("conversationId") else " Check it with code_jobs(job=...).")
        where = (f"session {job.session_id[:8]}" + (" (forked, it is open in a terminal)" if job.forked else "")
                 if job.session_id else f"a new session in {job.cwd}")
        return f"started {ad.label} job {job.id} on {where}.{tail}"

    async def code_jobs(job: str = "") -> str:
        if job:
            j = base.get_job(job)
            return base.render_job(j) if j else f"no job {job!r}"
        jobs = base.list_jobs(limit=10)
        if not jobs:
            return "no runs yet"
        return "\n".join(f"{j['id']} · {j['harness']} · {j['status']} · {j['toolCalls']} tool calls · {base.short(j['prompt'], 80)}" for j in jobs)

    async def code_stop(job: str = "") -> str:
        j = await base.stop(job)
        return f"job {j.id} {j.status}" if j else f"no job {job!r}"

    return {
        "code_sessions": {"fn": code_sessions, "schema": {"type": "function", "function": {
            "name": "code_sessions",
            "description": ("List the coding-agent sessions on this machine (Claude Code by default, or Codex), "
                            "newest first: which are live (busy or idle), their project folder, title and last "
                            "activity. Use the name or id prefix it prints with the other code_* tools."),
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "max sessions (default 10)"},
                "project": {"type": "string", "description": "only sessions whose project path contains this"},
                "harness": _HARNESS_PARAM}}}}},
        "code_session": {"fn": code_session, "schema": {"type": "function", "function": {
            "name": "code_session",
            "description": "Show one coding-agent session: status, project, and its last exchanges.",
            "parameters": {"type": "object", "properties": {
                "session": {"type": "string", "description": "session name, id, or id prefix"},
                "turns": {"type": "integer", "description": "how many recent messages to show (default 6)"},
                "harness": _HARNESS_PARAM},
                "required": ["session"]}}}},
        "code_run": {"fn": code_run, "schema": {"type": "function", "function": {
            "name": "code_run",
            "description": ("Send a prompt to Claude Code or Codex: continue an existing session (give 'session') "
                            "or start a new one in a project directory (give 'project'). Returns immediately with "
                            "a job id; the run can take minutes. In a chat channel the result is posted back "
                            "automatically, otherwise check code_jobs."),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "what the coding agent should do"},
                "session": {"type": "string", "description": "session name, id or prefix to continue"},
                "project": {"type": "string", "description": "directory for a new session (must be under an allowed root)"},
                "harness": _HARNESS_PARAM},
                "required": ["prompt"]}}}},
        "code_jobs": {"fn": code_jobs, "schema": {"type": "function", "function": {
            "name": "code_jobs",
            "description": "Status of runs started with code_run (both harnesses); give a job id for its result.",
            "parameters": {"type": "object", "properties": {
                "job": {"type": "string", "description": "job id (optional)"}}}}}},
        "code_stop": {"fn": code_stop, "schema": {"type": "function", "function": {
            "name": "code_stop",
            "description": "Stop a running coding-agent job started with code_run.",
            "parameters": {"type": "object", "properties": {
                "job": {"type": "string", "description": "job id"}}, "required": ["job"]}}}},
    }
