"""Shared machinery for coding harnesses (Claude Code, Codex).

A harness adapter knows how to *discover* its sessions on disk and how to
*run* a prompt as a headless subprocess that streams JSON events. Everything
else lives here once: settings, the ``Job`` record, process supervision with a
timeout and stop, the bounded job list, the allowed-roots check for new
sessions, and the text renderings chat surfaces use.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from storage import settings as settings_store

logger = logging.getLogger(__name__)

SETTINGS_KEY = "codingHarness"
DEFAULTS = {
    # Claude Code
    "bin": "",                # path to the claude binary; "" = VW_CLAUDE_BIN or PATH lookup
    "permissionMode": "",     # "" = inherit Claude Code's own setting
    "model": "",              # "" = Claude Code default
    # Codex
    "codexBin": "",           # path to the codex binary; "" = VW_CODEX_BIN or PATH lookup
    "codexSandbox": "workspace-write",   # read-only | workspace-write | danger-full-access
    "codexModel": "",
    # shared
    "maxTurns": 60,           # Claude Code: --max-turns (Codex has no equivalent)
    "timeoutS": 1800,         # kill a run after this long
    "allowedRoots": [],       # directories under which *new* sessions may be started
    "maxResultChars": 3500,   # trim the answer posted back to a chat
}
MAX_JOBS = 50
_EVENTS_KEEP = 40
MAX_PARALLEL = 3
PERMISSION_MODES = ("", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions", "manual")
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


class HarnessError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def get_settings() -> dict:
    stored = settings_store.get(SETTINGS_KEY) or {}
    out = {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}
    out["allowedRoots"] = [str(r) for r in (out.get("allowedRoots") or []) if str(r).strip()]
    return out


def set_settings(values: dict) -> dict:
    cur = get_settings()
    for k in DEFAULTS:
        if k in values and values[k] is not None:
            cur[k] = values[k]
    cur["maxTurns"] = max(0, min(500, int(cur["maxTurns"] or 0)))
    cur["timeoutS"] = max(30, min(6 * 3600, int(cur["timeoutS"] or 1800)))
    cur["maxResultChars"] = max(500, min(20000, int(cur["maxResultChars"] or 3500)))
    if cur["permissionMode"] not in PERMISSION_MODES:
        raise HarnessError("permissionMode must be one of " + ", ".join(m for m in PERMISSION_MODES if m))
    if cur["codexSandbox"] not in CODEX_SANDBOXES:
        raise HarnessError("codexSandbox must be one of " + ", ".join(CODEX_SANDBOXES))
    cur["allowedRoots"] = [str(Path(r).expanduser()) for r in (cur.get("allowedRoots") or []) if str(r).strip()]
    settings_store.set(SETTINGS_KEY, cur)
    return cur


def under_allowed_root(path: Path) -> bool:
    roots = [Path(r).expanduser().resolve() for r in get_settings().get("allowedRoots") or []]
    rp = path.resolve()
    return any(rp == r or r in rp.parents for r in roots)


# --------------------------------------------------------------------------- #
# Adapter contract
# --------------------------------------------------------------------------- #

class Adapter(Protocol):
    key: str
    label: str

    def availability(self) -> dict: ...
    def list_sessions(self, limit: int = 20, project: str | None = None) -> list[dict]: ...
    def resolve_session(self, ref: str) -> dict | None: ...
    def session_detail(self, session_id: str, turns: int = 6) -> dict | None: ...
    def argv(self, job: "Job") -> list[str]: ...
    def consume(self, job: "Job", rec: dict) -> None: ...
    def finish(self, job: "Job") -> None: ...


_ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    _ADAPTERS[adapter.key] = adapter
    return adapter


def get(key: str | None) -> Adapter:
    key = (key or "claude").strip().lower()
    aliases = {"claude-code": "claude", "claudecode": "claude", "cc": "claude", "openai": "codex"}
    key = aliases.get(key, key)
    if key not in _ADAPTERS:
        raise HarnessError(f"unknown harness {key!r}; use one of: " + ", ".join(sorted(_ADAPTERS)))
    return _ADAPTERS[key]


def adapters() -> list[Adapter]:
    return list(_ADAPTERS.values())


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

OnDone = Callable[["Job"], Awaitable[None]]


@dataclass
class Job:
    id: str
    harness: str
    prompt: str
    cwd: str
    session_id: str | None            # target (resumed) session, if any
    forked: bool = False
    status: str = "running"           # running | done | failed | stopped
    started_at: str = ""
    finished_at: str | None = None
    new_session_id: str | None = None  # the session the run wrote to
    result: str = ""
    error: str | None = None
    num_turns: int = 0
    tool_calls: int = 0
    cost_usd: float | None = None
    last_text: str = ""
    events: list[dict] = field(default_factory=list)
    origin: dict = field(default_factory=dict)   # {accountId, conversationId, agentId, sessionKey, kind, source}
    scratch: dict = field(default_factory=dict, repr=False)   # adapter-private (e.g. output file)
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    on_done: OnDone | None = field(default=None, repr=False)

    def view(self) -> dict:
        return {
            "id": self.id, "harness": self.harness, "prompt": self.prompt, "cwd": self.cwd,
            "sessionId": self.session_id, "forked": self.forked, "status": self.status,
            "startedAt": self.started_at, "finishedAt": self.finished_at,
            "newSessionId": self.new_session_id, "result": self.result, "error": self.error,
            "numTurns": self.num_turns, "toolCalls": self.tool_calls, "costUsd": self.cost_usd,
            "lastText": self.last_text, "origin": dict(self.origin), "events": self.events[-_EVENTS_KEEP:],
        }


JOBS: dict[str, Job] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def note(job: Job, kind: str, text: str) -> None:
    job.events.append({"ts": now_iso(), "kind": kind, "text": short(text, 300)})
    if len(job.events) > _EVENTS_KEEP * 2:
        job.events = job.events[-_EVENTS_KEEP:]


def list_jobs(limit: int = 20, harness: str | None = None) -> list[dict]:
    jobs = [j for j in list(JOBS.values())[::-1] if not harness or j.harness == harness]
    return [j.view() for j in jobs[:limit]]


def get_job(job_id: str) -> Job | None:
    if job_id in JOBS:
        return JOBS[job_id]
    cands = [j for j in JOBS.values() if j.id.startswith(job_id)]
    return cands[0] if len(cands) == 1 else None


def running_on(session_id: str) -> Job | None:
    return next((j for j in JOBS.values() if j.status == "running"
                 and session_id in (j.session_id, j.new_session_id)), None)


async def start_job(adapter: Adapter, prompt: str, *, session: str | None = None,
                    project: str | None = None, origin: dict | None = None,
                    on_done: OnDone | None = None) -> Job:
    """Start a prompt against an existing session (id, prefix or name) or a
    new one in ``project`` (must be under an allowed root)."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HarnessError("prompt is empty")
    fork = False
    if session:
        target = adapter.resolve_session(session)
        if not target:
            raise HarnessError(f"no {adapter.label} session matches {session!r}")
        cwd = target.get("project") or os.getcwd()
        fork = bool(target.get("live"))     # never write a transcript a terminal is using
        session_id = target["id"]
    else:
        if not project:
            raise HarnessError("give a session to continue, or a project directory for a new one")
        p = Path(project).expanduser()
        if not p.is_dir():
            raise HarnessError(f"project directory not found: {project}")
        if not under_allowed_root(p):
            raise HarnessError(f"{p} is outside the allowed roots (Settings → Coding harness → Allowed roots)")
        cwd = str(p.resolve())
        session_id = None
    if sum(1 for j in JOBS.values() if j.status == "running") >= MAX_PARALLEL:
        raise HarnessError(f"{MAX_PARALLEL} runs are already in progress; wait for one to finish or stop it")
    job = Job(id=uuid.uuid4().hex[:8], harness=adapter.key, prompt=prompt, cwd=cwd,
              session_id=session_id, forked=fork, started_at=now_iso(), origin=origin or {}, on_done=on_done)
    argv = adapter.argv(job)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        job.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    except OSError as exc:
        raise HarnessError(f"could not start {adapter.label}: {exc}") from exc
    JOBS[job.id] = job
    while len(JOBS) > MAX_JOBS:
        oldest = next(iter(JOBS))
        if JOBS[oldest].status == "running":
            break
        JOBS.pop(oldest)
    job.task = asyncio.get_event_loop().create_task(_supervise(adapter, job))
    logger.info("%s job %s started (%s)", adapter.key, job.id,
                "resume " + session_id if session_id else "new in " + cwd)
    return job


async def _supervise(adapter: Adapter, job: Job) -> None:
    import json
    proc = job.proc
    assert proc is not None
    timeout = float(get_settings().get("timeoutS") or 1800)
    stderr_tail = ""

    async def read_stdout():
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                note(job, "raw", line)
                continue
            try:
                adapter.consume(job, rec)
            except Exception:  # noqa: BLE001
                logger.debug("harness event parse failed", exc_info=True)

    async def read_stderr():
        nonlocal stderr_tail
        assert proc.stderr is not None
        async for raw in proc.stderr:
            stderr_tail = (stderr_tail + raw.decode("utf-8", "replace"))[-2000:]

    try:
        await asyncio.wait_for(asyncio.gather(read_stdout(), read_stderr(), proc.wait()), timeout=timeout)
        rc = proc.returncode
        try:
            adapter.finish(job)
        except Exception:  # noqa: BLE001
            logger.debug("harness finish failed", exc_info=True)
        if job.status == "running":
            if job.error:
                job.status = "failed"
            elif rc not in (0, None):
                job.status = "failed"
                job.error = f"{adapter.label} exited with {rc}: {stderr_tail.strip()[-500:] or 'no output'}"
            else:
                job.status = "done"
                if not job.result:
                    job.result = job.last_text or "(no reply)"
    except asyncio.TimeoutError:
        job.status = "failed"
        job.error = f"timed out after {int(timeout)}s"
        await _terminate(proc)
    except asyncio.CancelledError:
        await _terminate(proc)
        job.status = "stopped"
        raise
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = str(exc)[:500]
        await _terminate(proc)
    finally:
        job.finished_at = now_iso()
        logger.info("%s job %s %s (%d turns, %d tool calls)", adapter.key, job.id, job.status,
                    job.num_turns, job.tool_calls)
        if job.on_done:
            try:
                await job.on_done(job)
            except Exception:  # noqa: BLE001
                logger.warning("harness on_done failed for job %s", job.id, exc_info=True)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
    except ProcessLookupError:
        pass


async def stop(job_id: str) -> Job | None:
    job = get_job(job_id)
    if job is None:
        return None
    if job.status == "running":
        job.status = "stopped"
        job.error = "stopped by request"
        if job.proc:
            await _terminate(job.proc)
        if job.task:
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    return job


async def shutdown() -> None:
    for j in list(JOBS.values()):
        if j.status == "running":
            await stop(j.id)


# --------------------------------------------------------------------------- #
# Text renderings for chat surfaces
# --------------------------------------------------------------------------- #

def short(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    s = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def render_sessions(sessions: list[dict], label: str = "Claude Code", command: str = "/code") -> str:
    if not sessions:
        return f"No {label} sessions found on this machine."
    lines = [f"{label} sessions (newest first):"]
    for i, s in enumerate(sessions, 1):
        state = f"● {s['status']}" if s.get("live") else "○ stopped"
        proj = os.path.basename((s.get("project") or "").rstrip("/")) or s.get("project") or ""
        ref = s.get("name") or s["id"][:8]
        lines.append(f"{i}. {ref} · {proj} · {state} · {ago(s.get('lastActivity'))}\n   {s.get('title') or ''}")
    lines.append(f"Use the name or id prefix with {command} show <ref> or {command} run <ref> <prompt>.")
    return "\n".join(lines)


def render_detail(d: dict) -> str:
    state = f"● {d['status']}" + (f" (pid {d['pid']})" if d.get("pid") else "") if d.get("live") else "○ not running"
    head = [f"{d.get('title') or '(untitled)'}",
            f"session {d['id'][:8]}{(' · ' + d['name']) if d.get('name') else ''} · {state}",
            f"project {d.get('project') or '?'}{(' @ ' + d['gitBranch']) if d.get('gitBranch') else ''}",
            f"{d.get('messages', 0)} messages · {d.get('toolCalls', 0)} tool calls · last {ago(d.get('lastActivity'))}"]
    turns = d.get("turns") or []
    if turns:
        head.append("")
        for t in turns:
            who = "You" if t["role"] == "user" else d.get("assistantName") or "Agent"
            head.append(f"{who}: {short(t['text'], 400)}")
    return "\n".join(head)


def render_job(job: Job, max_chars: int | None = None) -> str:
    max_chars = max_chars or int(get_settings().get("maxResultChars") or 3500)
    label = {"claude": "Claude Code", "codex": "Codex"}.get(job.harness, job.harness)
    where = f"session {(job.new_session_id or job.session_id or '')[:8]}"
    if job.forked and job.session_id:
        where += f" (forked from {job.session_id[:8]})"
    if job.status == "running":
        last = job.events[-1]["text"] if job.events else "starting"
        return f"Job {job.id} ({label}) running on {where}: {job.tool_calls} tool calls so far · last: {short(last, 200)}"
    if job.status == "done":
        body = job.result.strip() or "(no reply)"
        if len(body) > max_chars:
            body = body[: max_chars - 1] + "…"
        cost = f" · ${job.cost_usd:.2f}" if isinstance(job.cost_usd, (int, float)) else ""
        turns = f"{job.num_turns} turns, " if job.num_turns else ""
        return f"{label} finished job {job.id} on {where} ({turns}{job.tool_calls} tool calls{cost}):\n\n{body}"
    return f"Job {job.id} ({label}) {job.status} on {where}: {job.error or 'no details'}"
