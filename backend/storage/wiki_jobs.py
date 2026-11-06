"""Linked Wiki Repository job runner (PRD-21 FR-15).

A small, in-process async runner for repository operations that the UI wants to
launch without blocking a request: it validates + trusts exactly like
``wiki_store.run_linked_operation`` (via the shared ``resolve_operation``), then
runs the argv with ``asyncio.create_subprocess_exec`` — no shell — streaming
stdout line-by-line to any SSE subscribers, applying the operation's timeout and
output caps, and supporting cancellation.  On completion it persists a row to
``wiki_repository_job`` and updates the wiki's last-operation state.

The synchronous ``wiki_store.run_linked_operation`` (used by the assistant tool)
is untouched; this is a parallel path for the interactive UI.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from storage import linked_wiki, wiki_store

_TERMINAL = {"succeeded", "failed", "cancelled", "timeout"}
_KILL_GRACE_SECONDS = 3.0
# An approved one-off command runs an arbitrary argv (from an approved proposal),
# so it is bounded independently of any registered operation's own limits.
_ADHOC_TIMEOUT = 60
_ADHOC_OUTPUT_LIMIT = 65536

_jobs: dict[str, "Job"] = {}
_registry_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Job:
    """A single async operation run + its live output buffer and subscribers."""

    def __init__(self, wiki_id: str, name: str, argv: list[str], cwd: str,
                 timeout: int, output_limit: int) -> None:
        self.id = secrets.token_hex(6)          # 12 hex chars
        self.wiki_id = wiki_id
        self.operation = name
        self.argv = argv
        self.cwd = cwd
        self.timeout = int(timeout)
        self.output_limit = int(output_limit)
        self.status = "running"
        self.exit_code: int | None = None
        self.started_at = _now()
        self.finished_at: str | None = None
        self.error: str | None = None
        self._lines: list[str] = []
        self._bytes = 0
        self._truncated = False
        self._subscribers: list[asyncio.Queue] = []
        self._proc: asyncio.subprocess.Process | None = None
        self._done = asyncio.Event()
        self._task: asyncio.Task | None = None       # held so the loop can't GC the run mid-flight

    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "wikiId": self.wiki_id, "operation": self.operation,
                "status": self.status, "argv": self.argv, "cwd": self.cwd,
                "exitCode": self.exit_code, "startedAt": self.started_at,
                "finishedAt": self.finished_at, "error": self.error,
                "output": "".join(self._lines)}


def _get(job_id: str) -> Job | None:
    with _registry_lock:
        return _jobs.get(job_id)


def _emit(job: Job, event: dict[str, Any]) -> None:
    for queue in list(job._subscribers):
        queue.put_nowait(event)


async def start(wiki_id: str, name: str, inputs: dict | None = None) -> Job:
    """Validate + launch an operation as a background job.  Raises
    ``wiki_store.WikiError`` for an unknown/untrusted operation or bad inputs —
    exactly the checks the synchronous path applies — before any subprocess."""
    repo, operation = wiki_store.resolve_operation(wiki_id, name, require_trusted=True)
    try:
        argv = linked_wiki.build_argv(operation, inputs or {})
        cwd = linked_wiki.resolve_cwd(repo, operation)
    except linked_wiki.LinkedWikiError as exc:
        raise wiki_store.WikiError(str(exc)) from exc
    job = Job(wiki_id, name, argv, str(cwd), operation["timeoutSeconds"], operation["outputLimitBytes"])
    with _registry_lock:
        _jobs[job.id] = job
    # Hold the task reference (on the Job, which _jobs keeps) so CPython can't
    # garbage-collect a still-running task — a documented create_task pitfall.
    job._task = asyncio.create_task(_run(job))
    return job


async def _run(job: Job) -> None:
    try:
        job._proc = await asyncio.create_subprocess_exec(
            *job.argv, cwd=job.cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except (OSError, ValueError) as exc:
        job.status = "failed"
        job.error = f"operation could not start: {exc}"
        job.finished_at = _now()
        _finalize(job)
        return
    try:
        await asyncio.wait_for(_pump(job), timeout=job.timeout)
        rc = await job._proc.wait()
        job.exit_code = rc
        if job.status == "running":                 # cancel() may already have won
            job.status = "succeeded" if rc == 0 else "failed"
    except asyncio.TimeoutError:
        await _kill(job)
        if job.status == "running":
            job.status = "timeout"
            job.error = "operation exceeded its timeout"
    except Exception as exc:                         # best-effort — never crash the loop
        job.status = "failed"
        job.error = str(exc)
    job.finished_at = _now()
    _finalize(job)


async def _pump(job: Job) -> None:
    stream = job._proc.stdout if job._proc else None
    if stream is None:
        return
    while True:
        raw = await stream.readline()
        if not raw:
            break
        job._bytes += len(raw)
        if job._bytes <= job.output_limit:
            line = raw.decode("utf-8", errors="replace")
            job._lines.append(line)
            _emit(job, {"type": "line", "line": line})
        elif not job._truncated:
            job._truncated = True
            marker = "…[output truncated]\n"
            job._lines.append(marker)
            _emit(job, {"type": "line", "line": marker})
        # keep draining past the cap so the process never blocks on a full pipe


async def _kill(job: Job) -> None:
    proc = job._proc
    if not proc or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass


def _finalize(job: Job) -> None:
    try:
        wiki_store.record_job(job.snapshot())
    except Exception:                                # best-effort persistence
        pass
    try:
        error = None if job.status == "succeeded" else (job.error or "".join(job._lines)[-1000:] or job.status)
        wiki_store.mark_operation(job.wiki_id, job.operation, error)
    except Exception:
        pass
    if job.status == "succeeded" and job.exit_code == 0:
        try:
            # Kind-based (setup/sync) stamping + re-probe; a non-op adhoc run
            # (kind "custom") is a no-op here.
            wiki_store._note_kind_success(job.wiki_id, job.operation)
        except Exception:
            pass
    job._done.set()
    _emit(job, {"type": "end", **job.snapshot()})


async def subscribe(job_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """Yield buffered-then-live output events, then a terminal ``end`` event.

    Subscribing before snapshotting the buffer (both synchronous, no await
    between them) guarantees no line is missed or replayed twice."""
    job = _get(job_id)
    if not job:
        return
    queue: asyncio.Queue = asyncio.Queue()
    job._subscribers.append(queue)
    replay = list(job._lines)
    already_done = job._done.is_set()
    try:
        for line in replay:
            yield {"type": "line", "line": line}
        if already_done:
            yield {"type": "end", **job.snapshot()}
            return
        while True:
            event = await queue.get()
            yield event
            if event.get("type") == "end":
                break
    finally:
        if queue in job._subscribers:
            job._subscribers.remove(queue)


async def cancel(job_id: str) -> dict | None:
    job = _get(job_id)
    if not job:
        return None
    if job.status in _TERMINAL:
        return job.snapshot()
    job.status = "cancelled"                          # claim it before _run can label it done
    await _kill(job)
    return job.snapshot()


def get(job_id: str) -> dict | None:
    job = _get(job_id)
    return job.snapshot() if job else None


# --------------------------------------------------------------------------- #
# Approved one-off commands — launched only AFTER a human approves the proposal
# that carries them (linked-wiki revamp §5). No awaiting_approval state here: the
# approval already happened in the proposal queue, so this launches immediately.
# --------------------------------------------------------------------------- #

async def start_adhoc(wiki_id: str, argv: list[str], reason: str = "") -> Job:
    """Validate + launch an approved one-off argv as a background job. The cwd is
    pinned to the linked repository root (``linked_wiki._inside`` rejects any
    escape). Only proposal execution calls this, after user approval."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise wiki_store.WikiError("adhoc argv must be a non-empty list of non-empty strings")
    repo, _root = wiki_store.linked_paths(wiki_id)
    try:
        cwd = linked_wiki._inside(repo, repo)
    except linked_wiki.LinkedWikiError as exc:
        raise wiki_store.WikiError(str(exc)) from exc
    job = Job(wiki_id, "adhoc", list(argv), str(cwd), _ADHOC_TIMEOUT, _ADHOC_OUTPUT_LIMIT)
    with _registry_lock:
        _jobs[job.id] = job
    job._task = asyncio.create_task(_run(job))
    return job
