"""``/code`` commands on chat channels and the result flow-back.

Deterministic counterpart to the ``code_*`` agent tools: a person on Telegram
(or any channel) types ``/code list``, ``/code show <ref>``, ``/code run <ref>
<prompt>`` and gets a precise answer without a model in the loop. Long runs
answer at once with the job id and post the result into the same chat when
Claude Code finishes. Gated by the bound worker's ``codingHarness`` guardrail.
"""

from __future__ import annotations

import logging

from harness import claude_code as cc

logger = logging.getLogger(__name__)

HELP = (
    "/code list — your Claude Code sessions\n"
    "/code show <ref> — last exchanges of a session (name or id prefix)\n"
    "/code run <ref> <prompt> — send a prompt into a session; the result comes back here\n"
    "/code new <project-dir> <prompt> — start a new session in an allowed directory\n"
    "/code jobs — runs started from here\n"
    "/code job <id> — one run's status or result\n"
    "/code stop <id> — stop a run"
)


def enabled_for(agent: dict | None) -> bool:
    return bool(((agent or {}).get("guardrails") or {}).get("codingHarness"))


def deliver_to(origin: dict):
    """Build the ``on_done`` callback that posts a finished job back into the
    chat it came from and keeps the chat session's history complete."""
    async def on_done(job: cc.Job) -> None:
        if not origin.get("accountId") or not origin.get("conversationId"):
            return
        from channels.manager import manager
        from storage import chat_store
        text = cc.render_job(job)
        await manager.send(origin["accountId"], origin["conversationId"], text,
                           agent_id=origin.get("agentId"), source="reply")
        if origin.get("sessionKey"):
            try:
                chat_store.append_message(origin["sessionKey"], "assistant", text,
                                          agent_id=origin.get("agentId"),
                                          meta={"channel": origin.get("kind") or "chat", "harnessJob": job.id})
            except Exception:  # noqa: BLE001
                logger.debug("harness flow-back persist failed", exc_info=True)
    return on_done


async def handle(text: str, *, origin: dict, agent: dict | None) -> str | None:
    """Return the reply for a ``/code …`` message, or ``None`` when ``text``
    is not a /code command."""
    raw = (text or "").strip()
    if not raw.lower().startswith("/code"):
        return None
    if not enabled_for(agent):
        return ("The coding harness is off for this worker. Turn on Guardrails → "
                "Coding harness (Claude Code) in the worker's settings to use /code.")
    rest = raw[5:].strip()
    parts = rest.split(None, 1)
    sub = (parts[0].lower() if parts else "help")
    arg = parts[1].strip() if len(parts) > 1 else ""
    try:
        if sub in ("help", "?", ""):
            return HELP
        if sub in ("list", "ls", "sessions"):
            return cc.render_sessions(cc.list_sessions(limit=8))
        if sub in ("show", "status", "see"):
            s = cc.resolve_session(arg)
            if not s:
                return f"No session matches {arg!r}. Try /code list."
            d = cc.session_detail(s["id"], turns=4)
            return cc.render_detail(d) if d else "Session not found."
        if sub in ("run", "ask", "send", "resume"):
            ref, _, prompt = arg.partition(" ")
            if not ref or not prompt.strip():
                return "Usage: /code run <session ref> <prompt>"
            job = await cc.start(prompt.strip(), session=ref, origin=origin, on_done=deliver_to(origin))
            where = f"{job.session_id[:8]}" + (" (forked: that session is open in a terminal)" if job.forked else "")
            return f"Started job {job.id} on session {where}. I'll post the result here when Claude Code finishes."
        if sub == "new":
            proj, _, prompt = arg.partition(" ")
            if not proj or not prompt.strip():
                return "Usage: /code new <project-dir> <prompt>"
            job = await cc.start(prompt.strip(), project=proj, origin=origin, on_done=deliver_to(origin))
            return f"Started job {job.id} in {job.cwd}. I'll post the result here when it finishes."
        if sub == "jobs":
            jobs = [j for j in cc.list_jobs(limit=10)]
            if not jobs:
                return "No runs yet. Start one with /code run <ref> <prompt>."
            return "\n".join(f"{j['id']} · {j['status']} · {j['toolCalls']} tool calls · {cc._short(j['prompt'], 80)}" for j in jobs)
        if sub == "job":
            job = cc.get_job(arg)
            return cc.render_job(job) if job else f"No job {arg!r}."
        if sub in ("stop", "kill", "cancel"):
            job = await cc.stop(arg)
            return f"Job {job.id} {job.status}." if job else f"No job {arg!r}."
        return f"Unknown /code command {sub!r}.\n{HELP}"
    except cc.HarnessError as exc:
        return f"Can't do that: {exc}"
