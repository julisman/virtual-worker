"""``/code`` (Claude Code) and ``/codex`` (Codex) commands on chat channels,
plus the result flow-back.

Deterministic counterpart to the ``code_*`` agent tools: a person on Telegram
(or any channel) types ``/code list``, ``/code show <ref>``, ``/code run <ref>
<prompt>`` and gets a precise answer without a model in the loop. Long runs
answer at once with the job id and post the result into the same chat when
the harness finishes. Gated by the bound worker's ``codingHarness`` guardrail.
"""

from __future__ import annotations

import logging

from harness import base
from harness import claude_code, codex  # noqa: F401 — importing registers both adapters

logger = logging.getLogger(__name__)

PREFIXES = {"/codex": "codex", "/code": "claude"}   # longest first: "/codex" before "/code"


def help_text(cmd: str, label: str) -> str:
    return (
        f"{cmd} list — your {label} sessions\n"
        f"{cmd} show <ref> — last exchanges of a session (id prefix{' or name' if cmd == '/code' else ''})\n"
        f"{cmd} run <ref> <prompt> — send a prompt into a session; the result comes back here\n"
        f"{cmd} new <project-dir> <prompt> — start a new session in an allowed directory\n"
        f"{cmd} jobs — runs started from here\n"
        f"{cmd} job <id> — one run's status or result\n"
        f"{cmd} stop <id> — stop a run"
        + ("\n/codex … — the same for Codex" if cmd == "/code" else "\n/code … — the same for Claude Code")
    )


def enabled_for(agent: dict | None) -> bool:
    return bool(((agent or {}).get("guardrails") or {}).get("codingHarness"))


_LAST_STARTED: dict[tuple[str, str], base.Job] = {}


def _chat(origin: dict) -> bool:
    return bool(origin.get("accountId") and origin.get("conversationId"))


def deliver_to(origin: dict):
    """Build the ``on_done`` callback that posts a finished job back into the
    chat it came from, remembers the message so a reply can continue the
    session, and keeps the chat session's history complete."""
    async def on_done(job: base.Job) -> None:
        if not _chat(origin):
            return
        from channels.manager import manager
        from storage import chat_store
        text = base.render_job(job)
        if job.status == "done":
            text += "\n\nReply to this message to continue in the same session."
        rec = await manager.send(origin["accountId"], origin["conversationId"], text,
                                 agent_id=origin.get("agentId"), source="reply")
        base.remember_reply(origin["accountId"], (rec or {}).get("messageId"), base.reply_context(job))
        if origin.get("sessionKey"):
            try:
                chat_store.append_message(origin["sessionKey"], "assistant", text,
                                          agent_id=origin.get("agentId"),
                                          meta={"channel": origin.get("kind") or "chat", "harnessJob": job.id})
            except Exception:  # noqa: BLE001
                logger.debug("harness flow-back persist failed", exc_info=True)
    return on_done


def progress_to(origin: dict):
    """Build the ``on_progress`` callback: a short status line into the chat
    while the run works (throttled by ``progressEveryS``)."""
    if not _chat(origin):
        return None

    async def on_progress(job: base.Job) -> None:
        from channels.manager import manager
        rec = await manager.send(origin["accountId"], origin["conversationId"], base.render_progress(job),
                                 agent_id=origin.get("agentId"), source="reply")
        base.remember_reply(origin["accountId"], (rec or {}).get("messageId"), base.reply_context(job))
    return on_progress


def _started(origin: dict, job: base.Job) -> None:
    if _chat(origin):
        _LAST_STARTED[(origin["accountId"], origin["conversationId"])] = job
        while len(_LAST_STARTED) > 200:
            _LAST_STARTED.pop(next(iter(_LAST_STARTED)))


def after_send(origin: dict, rec: dict | None) -> None:
    """Called by the channel manager after it sent our reply: if a run was
    just started from this chat, the "Started job …" message can be replied to."""
    if not _chat(origin):
        return
    job = _LAST_STARTED.pop((origin["accountId"], origin["conversationId"]), None)
    if job is not None:
        base.remember_reply(origin["accountId"], (rec or {}).get("messageId"), base.reply_context(job))


def continuation(origin: dict, reply_to: str | None) -> dict | None:
    """The harness context a quoted message belongs to, if any."""
    return base.lookup_reply(origin.get("accountId"), reply_to)


def _split(text: str) -> tuple[str, str, str] | None:
    """``(harness key, command word, rest)`` or None when not a harness command."""
    raw = (text or "").strip()
    low = raw.lower()
    for prefix, key in PREFIXES.items():
        if low == prefix or low.startswith(prefix + " "):
            return key, prefix, raw[len(prefix):].strip()
    return None


async def handle(text: str, *, origin: dict, agent: dict | None, reply_to: str | None = None) -> str | None:
    """Return the reply for a ``/code …`` or ``/codex …`` message, or for a
    plain message that quotes one of our job messages (continuation); ``None``
    when ``text`` is neither."""
    parsed = _split(text)
    ctx = None if parsed else continuation(origin, reply_to)
    if not parsed and not ctx:
        return None
    if parsed:
        key, cmd, rest = parsed
    else:
        key, cmd, rest = ctx["harness"], "/code" if ctx["harness"] == "claude" else "/codex", ""
    if not enabled_for(agent):
        return ("The coding harness is off for this worker. Turn on Guardrails → Integrations → "
                f"Coding harness in the worker's settings to use {cmd}.")
    ad = base.get(key)
    if ctx:
        # A reply to a job message: send the text into that job's session.
        prompt = (text or "").strip()
        if not prompt:
            return "Write what the session should do next."
        try:
            job = await base.start_job(ad, prompt, session=ctx["sessionId"], origin=origin,
                                       on_done=deliver_to(origin), on_progress=progress_to(origin))
        except base.HarnessError as exc:
            return f"Can't continue that session: {exc}"
        _started(origin, job)
        return f"Continuing {ad.label} session {job.session_id[:8]} as job {job.id}. I'll post the result here."
    parts = rest.split(None, 1)
    sub = (parts[0].lower() if parts else "help")
    arg = parts[1].strip() if len(parts) > 1 else ""
    try:
        if sub in ("help", "?", ""):
            return help_text(cmd, ad.label)
        if sub in ("list", "ls", "sessions"):
            return base.render_sessions(await base.run_sync(ad.list_sessions, limit=8), ad.label, cmd)
        if sub in ("show", "status", "see"):
            s = await base.run_sync(ad.resolve_session, arg)
            if not s:
                return f"No {ad.label} session matches {arg!r}. Try {cmd} list."
            d = await base.run_sync(ad.session_detail, s["id"], turns=4)
            return base.render_detail(d) if d else "Session not found."
        if sub in ("run", "ask", "send", "resume"):
            ref, _, prompt = arg.partition(" ")
            if not ref or not prompt.strip():
                return f"Usage: {cmd} run <session ref> <prompt>"
            job = await base.start_job(ad, prompt.strip(), session=ref, origin=origin,
                                       on_done=deliver_to(origin), on_progress=progress_to(origin))
            _started(origin, job)
            where = f"{job.session_id[:8]}" + (" (forked: that session is open in a terminal)" if job.forked else "")
            return (f"Started job {job.id} on {ad.label} session {where}. I'll post the result here when it "
                    f"finishes; reply to this message to send a follow-up into the same session.")
        if sub == "new":
            proj, _, prompt = arg.partition(" ")
            if not proj or not prompt.strip():
                return f"Usage: {cmd} new <project-dir> <prompt>"
            job = await base.start_job(ad, prompt.strip(), project=proj, origin=origin,
                                       on_done=deliver_to(origin), on_progress=progress_to(origin))
            _started(origin, job)
            return f"Started {ad.label} job {job.id} in {job.cwd}. I'll post the result here when it finishes."
        if sub == "jobs":
            jobs = base.list_jobs(limit=10, scope=origin)       # only this chat's runs
            if not jobs:
                return f"No runs from this chat yet. Start one with {cmd} run <ref> <prompt>."
            return base.render_jobs(jobs)
        if sub == "job":
            job = base.get_job(arg, scope=origin)
            return base.render_job(job) if job else f"No job {arg!r} from this chat."
        if sub in ("stop", "kill", "cancel"):
            if not arg:
                return f"Usage: {cmd} stop <job id>"
            job = await base.stop(arg, scope=origin)
            return f"Job {job.id} {job.status}." if job else f"No job {arg!r} from this chat."
        return f"Unknown {cmd} command {sub!r}.\n{help_text(cmd, ad.label)}"
    except base.HarnessError as exc:
        return f"Can't do that: {exc}"
