"""Claude Code harness: see your Claude Code sessions and send prompts into them.

Claude Code keeps everything this module needs on disk under ``~/.claude``:

* ``sessions/<pid>.json``: the live-session registry (one file per running
  process: session id, project directory, derived name, ``busy``/``idle``).
* ``projects/<slug>/<session-id>.jsonl``: the transcript of every session,
  with an ``ai-title`` record, the last prompt, and every user/assistant turn.

Listing and monitoring only read those files. Sending a prompt runs
``claude -p <prompt> --resume <id> --output-format stream-json`` as a
subprocess this backend supervises (``Job``): events are parsed as they
stream, the final ``result`` record carries the answer, cost and turn count,
and an optional ``on_done`` callback lets a chat channel receive the result
where the request came from. When the target session is open in a terminal
the run forks it (``--fork-session``) so two processes never write the same
transcript; the new session id is reported.

New sessions may only start inside ``allowedRoots`` from the settings, so a
prompt arriving over a chat channel can never point Claude Code at an
arbitrary directory. Everything is opt-in per worker (``guardrails.codingHarness``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import config
from storage import settings as settings_store

logger = logging.getLogger(__name__)

SETTINGS_KEY = "codingHarness"
DEFAULTS = {
    "bin": "",                # path to the claude binary; "" = VW_CLAUDE_BIN or PATH lookup
    "permissionMode": "",     # "" = inherit Claude Code's own setting; else acceptEdits | auto | plan | dontAsk | bypassPermissions
    "maxTurns": 60,           # cap on agentic turns per prompt (0 = Claude Code default)
    "timeoutS": 1800,         # kill a run after this long
    "model": "",              # "" = Claude Code default
    "allowedRoots": [],       # directories under which *new* sessions may be started
    "maxResultChars": 3500,   # trim the answer posted back to a chat
}
MAX_JOBS = 50
_EVENTS_KEEP = 40


class HarnessError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Settings + availability
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
    if cur["permissionMode"] not in ("", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions", "manual"):
        raise HarnessError("permissionMode must be one of acceptEdits, auto, plan, dontAsk, bypassPermissions, manual")
    cur["allowedRoots"] = [str(Path(r).expanduser()) for r in (cur.get("allowedRoots") or []) if str(r).strip()]
    settings_store.set(SETTINGS_KEY, cur)
    return cur


def claude_bin() -> str | None:
    cand = (get_settings().get("bin") or config.CLAUDE_BIN or "").strip()
    if cand:
        return cand if os.path.isabs(cand) and os.access(cand, os.X_OK) else shutil.which(cand)
    return shutil.which("claude")


_version_cache: dict = {"bin": None, "version": None, "at": 0.0}


def availability() -> dict:
    """Whether Claude Code is runnable, with its version (cached a minute)."""
    b = claude_bin()
    if not b:
        return {"available": False, "bin": None, "version": None,
                "error": "claude binary not found; install Claude Code or set the path in Settings"}
    now = time.time()
    if _version_cache["bin"] == b and now - _version_cache["at"] < 60:
        return {"available": True, "bin": b, "version": _version_cache["version"], "error": None}
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20)
        ver = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else "unknown"
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "bin": b, "version": None, "error": f"claude --version failed: {exc}"}
    _version_cache.update(bin=b, version=ver, at=now)
    return {"available": True, "bin": b, "version": ver, "error": None}


# --------------------------------------------------------------------------- #
# Discovery: live sessions + transcripts
# --------------------------------------------------------------------------- #

def _home() -> Path:
    return Path(config.CLAUDE_HOME)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _iso(ms: float | int | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        return None


def live_sessions() -> list[dict]:
    """Running Claude Code processes from the registry (dead pids skipped)."""
    out: list[dict] = []
    d = _home() / "sessions"
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = rec.get("pid")
        if not pid or not _pid_alive(pid):
            continue
        out.append({
            "id": rec.get("sessionId") or "",
            "pid": pid,
            "cwd": rec.get("cwd") or "",
            "name": rec.get("name") or "",
            "kind": rec.get("kind") or "interactive",
            "status": rec.get("status") or "unknown",
            "startedAt": _iso(rec.get("startedAt")),
            "updatedAt": _iso(rec.get("updatedAt") or rec.get("statusUpdatedAt")),
        })
    return out


def _transcript_path(session_id: str) -> Path | None:
    root = _home() / "projects"
    if not root.is_dir() or not session_id:
        return None
    for p in root.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def _text_of(content) -> str:
    """Visible text of a message's content (str or block list)."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            parts.append(b["text"])
        elif t == "tool_use":
            parts.append(f"[tool: {b.get('name')}]")
    return "\n".join(parts).strip()


def _user_is_tool_result(content) -> bool:
    return isinstance(content, list) and bool(content) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


_scan_cache: dict[str, tuple[float, dict]] = {}


def scan_transcript(path: Path, turns: int = 0) -> dict:
    """Summary of one transcript: title, project, last prompt, last answer,
    time span, and (optionally) the last ``turns`` visible exchanges."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _scan_cache.get(key)
    if cached and cached[0] == mtime and turns <= len(cached[1].get("turns", [])) and cached[1].get("_turns", 0) >= turns:
        d = dict(cached[1]); d["turns"] = d["turns"][-turns:] if turns else []
        return d
    info: dict = {"id": path.stem, "title": "", "cwd": "", "lastPrompt": "", "lastAssistant": "",
                  "firstTs": None, "lastTs": None, "gitBranch": "", "messages": 0, "toolCalls": 0}
    exchanges: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                t = rec.get("type")
                if t == "ai-title" and rec.get("aiTitle"):
                    info["title"] = rec["aiTitle"]
                elif t == "last-prompt" and rec.get("lastPrompt"):
                    info["lastPrompt"] = rec["lastPrompt"]
                elif t in ("user", "assistant"):
                    if rec.get("isSidechain") or rec.get("isMeta"):
                        continue
                    msg = rec.get("message") or {}
                    content = msg.get("content")
                    if t == "user" and _user_is_tool_result(content):
                        continue
                    text = _text_of(content)
                    if t == "user" and text.lstrip().startswith("<"):
                        # Slash-command and system-reminder records are stored as user
                        # messages wrapped in tags; they are not prompts.
                        continue
                    if t == "assistant" and isinstance(content, list):
                        info["toolCalls"] += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
                    if not text:
                        continue
                    info["messages"] += 1
                    ts = rec.get("timestamp")
                    info["firstTs"] = info["firstTs"] or ts
                    info["lastTs"] = ts or info["lastTs"]
                    if rec.get("cwd"):
                        info["cwd"] = rec["cwd"]
                    if rec.get("gitBranch"):
                        info["gitBranch"] = rec["gitBranch"]
                    if t == "assistant":
                        info["lastAssistant"] = text
                    elif not info["lastPrompt"]:
                        pass
                    exchanges.append({"role": t, "text": text, "ts": ts})
                    if len(exchanges) > max(turns, 12) * 2:
                        exchanges = exchanges[-max(turns, 12) * 2:]
    except OSError:
        return {}
    users = [e for e in exchanges if e["role"] == "user"]
    if users and not info["lastPrompt"]:
        info["lastPrompt"] = users[-1]["text"]
    info["_turns"] = len(exchanges)
    info["turns"] = exchanges
    _scan_cache[key] = (mtime, dict(info))
    if len(_scan_cache) > 200:
        _scan_cache.pop(next(iter(_scan_cache)))
    d = dict(info); d["turns"] = exchanges[-turns:] if turns else []
    return d


def _short(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def list_sessions(limit: int = 20, project: str | None = None) -> list[dict]:
    """Sessions ordered by last activity: live ones first, then recent transcripts."""
    live = {s["id"]: s for s in live_sessions() if s["id"]}
    root = _home() / "projects"
    files: list[Path] = []
    if root.is_dir():
        files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    out: list[dict] = []
    seen: set[str] = set()
    for p in files[: max(limit * 3, 30)]:
        sid = p.stem
        if sid in seen:
            continue
        info = scan_transcript(p)
        if not info or not info.get("messages"):
            continue
        if project and project not in (info.get("cwd") or ""):
            continue
        l = live.get(sid)
        out.append({
            "id": sid,
            "title": info.get("title") or _short(info.get("lastPrompt") or "", 60) or "(untitled)",
            "project": (l or {}).get("cwd") or info.get("cwd") or "",
            "live": bool(l),
            "status": (l or {}).get("status") or "stopped",
            "name": (l or {}).get("name") or "",
            "pid": (l or {}).get("pid"),
            "lastActivity": info.get("lastTs"),
            "lastPrompt": _short(info.get("lastPrompt") or ""),
            "lastAssistant": _short(info.get("lastAssistant") or ""),
            "messages": info.get("messages", 0),
        })
        seen.add(sid)
    for sid, l in live.items():     # live sessions without a transcript yet
        if sid not in seen and (not project or project in l["cwd"]):
            out.append({"id": sid, "title": l["name"] or "(new)", "project": l["cwd"], "live": True,
                        "status": l["status"], "name": l["name"], "pid": l["pid"],
                        "lastActivity": l["updatedAt"], "lastPrompt": "", "lastAssistant": "", "messages": 0})
    out.sort(key=lambda s: (not s["live"], s.get("lastActivity") or ""), reverse=False)
    out.sort(key=lambda s: s.get("lastActivity") or "", reverse=True)
    out.sort(key=lambda s: not s["live"])
    return out[:limit]


def resolve_session(ref: str) -> dict | None:
    """Find a session by full id, id prefix, or live name (or part of it)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    for s in list_sessions(limit=200):
        if s["id"] == ref or (s["name"] and s["name"] == ref):
            return s
    cands = [s for s in list_sessions(limit=200)
             if s["id"].startswith(ref) or (s["name"] and ref.lower() in s["name"].lower())]
    return cands[0] if len(cands) == 1 else (cands[0] if cands and all(c["id"] == cands[0]["id"] for c in cands) else None)


def session_detail(session_id: str, turns: int = 6) -> dict | None:
    p = _transcript_path(session_id)
    live = next((s for s in live_sessions() if s["id"] == session_id), None)
    if p is None and live is None:
        return None
    info = scan_transcript(p, turns=turns) if p else {}
    return {
        "id": session_id,
        "title": info.get("title") or (live or {}).get("name") or "(untitled)",
        "project": (live or {}).get("cwd") or info.get("cwd") or "",
        "gitBranch": info.get("gitBranch") or "",
        "live": bool(live),
        "status": (live or {}).get("status") or "stopped",
        "name": (live or {}).get("name") or "",
        "pid": (live or {}).get("pid"),
        "firstActivity": info.get("firstTs"),
        "lastActivity": info.get("lastTs") or (live or {}).get("updatedAt"),
        "messages": info.get("messages", 0),
        "toolCalls": info.get("toolCalls", 0),
        "turns": info.get("turns") or [],
        "transcript": str(p) if p else None,
    }


# --------------------------------------------------------------------------- #
# Jobs: prompts sent through `claude -p`
# --------------------------------------------------------------------------- #

OnDone = Callable[["Job"], Awaitable[None]]


@dataclass
class Job:
    id: str
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
    origin: dict = field(default_factory=dict)   # {accountId, conversationId, agentId, source}
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    on_done: OnDone | None = field(default=None, repr=False)

    def view(self) -> dict:
        return {
            "id": self.id, "prompt": self.prompt, "cwd": self.cwd, "sessionId": self.session_id,
            "forked": self.forked, "status": self.status, "startedAt": self.started_at,
            "finishedAt": self.finished_at, "newSessionId": self.new_session_id,
            "result": self.result, "error": self.error, "numTurns": self.num_turns,
            "toolCalls": self.tool_calls, "costUsd": self.cost_usd, "lastText": self.last_text,
            "origin": {k: v for k, v in self.origin.items() if k != "agentId"} | ({"agentId": self.origin.get("agentId")} if self.origin.get("agentId") else {}),
            "events": self.events[-_EVENTS_KEEP:],
        }


JOBS: dict[str, Job] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _under_allowed_root(path: Path) -> bool:
    roots = [Path(r).expanduser().resolve() for r in get_settings().get("allowedRoots") or []]
    rp = path.resolve()
    return any(rp == r or r in rp.parents for r in roots)


def list_jobs(limit: int = 20) -> list[dict]:
    return [j.view() for j in list(JOBS.values())[::-1][:limit]]


def get_job(job_id: str) -> Job | None:
    if job_id in JOBS:
        return JOBS[job_id]
    cands = [j for j in JOBS.values() if j.id.startswith(job_id)]
    return cands[0] if len(cands) == 1 else None


def _argv(prompt: str, *, session_id: str | None, fork: bool) -> list[str]:
    cfg = get_settings()
    b = claude_bin()
    if not b:
        raise HarnessError("claude binary not found; install Claude Code or set the path in Settings → Coding harness")
    argv = [b, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if session_id:
        argv += ["--resume", session_id]
        if fork:
            argv.append("--fork-session")
    if cfg.get("permissionMode"):
        argv += ["--permission-mode", cfg["permissionMode"]]
    if int(cfg.get("maxTurns") or 0) > 0:
        argv += ["--max-turns", str(int(cfg["maxTurns"]))]
    if cfg.get("model"):
        argv += ["--model", cfg["model"]]
    return argv


async def start(prompt: str, *, session: str | None = None, project: str | None = None,
                origin: dict | None = None, on_done: OnDone | None = None) -> Job:
    """Start a prompt against an existing session (``session``: id, prefix or
    live name) or a new one in ``project`` (must be under an allowed root)."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HarnessError("prompt is empty")
    target = None
    fork = False
    if session:
        target = resolve_session(session)
        if not target:
            raise HarnessError(f"no Claude Code session matches {session!r}")
        cwd = target["project"] or os.getcwd()
        fork = bool(target["live"])     # never write a transcript a terminal is using
        session_id = target["id"]
    else:
        if not project:
            raise HarnessError("give a session to continue, or a project directory for a new one")
        p = Path(project).expanduser()
        if not p.is_dir():
            raise HarnessError(f"project directory not found: {project}")
        if not _under_allowed_root(p):
            raise HarnessError(f"{p} is outside the allowed roots (Settings → Coding harness → Allowed roots)")
        cwd = str(p.resolve())
        session_id = None
    running = [j for j in JOBS.values() if j.status == "running"]
    if len(running) >= 3:
        raise HarnessError("three runs are already in progress; wait for one to finish or stop it")
    argv = _argv(prompt, session_id=session_id, fork=fork)
    job = Job(id=uuid.uuid4().hex[:8], prompt=prompt, cwd=cwd, session_id=session_id, forked=fork,
              started_at=_now(), origin=origin or {}, on_done=on_done)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        job.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    except OSError as exc:
        raise HarnessError(f"could not start claude: {exc}") from exc
    JOBS[job.id] = job
    while len(JOBS) > MAX_JOBS:
        oldest = next(iter(JOBS))
        if JOBS[oldest].status == "running":
            break
        JOBS.pop(oldest)
    job.task = asyncio.get_event_loop().create_task(_supervise(job))
    logger.info("harness job %s started (%s)", job.id, "resume " + session_id if session_id else "new in " + cwd)
    return job


def _note(job: Job, kind: str, text: str) -> None:
    job.events.append({"ts": _now(), "kind": kind, "text": _short(text, 300)})
    if len(job.events) > _EVENTS_KEEP * 2:
        job.events = job.events[-_EVENTS_KEEP:]


def _consume(job: Job, rec: dict) -> None:
    t = rec.get("type")
    if t == "system" and rec.get("subtype") == "init":
        job.new_session_id = rec.get("session_id") or job.new_session_id
        _note(job, "init", f"model {rec.get('model')} · {rec.get('cwd')}")
    elif t == "assistant":
        content = (rec.get("message") or {}).get("content") or []
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                job.last_text = b["text"]
                _note(job, "text", b["text"])
            elif b.get("type") == "tool_use":
                job.tool_calls += 1
                args = b.get("input") or {}
                hint = args.get("command") or args.get("file_path") or args.get("pattern") or args.get("description") or ""
                _note(job, "tool", f"{b.get('name')} {hint}".strip())
    elif t == "result":
        job.num_turns = int(rec.get("num_turns") or 0)
        job.cost_usd = rec.get("total_cost_usd")
        job.new_session_id = rec.get("session_id") or job.new_session_id
        if rec.get("is_error"):
            job.error = str(rec.get("result") or rec.get("error") or "run failed")
        else:
            job.result = str(rec.get("result") or job.last_text or "")
    elif t == "system" and rec.get("subtype") in ("informational", "model_refusal_fallback") and rec.get("content"):
        _note(job, "system", str(rec["content"]))


async def _supervise(job: Job) -> None:
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
                _note(job, "raw", line)
                continue
            try:
                _consume(job, rec)
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
        if job.status == "running":
            if job.error:
                job.status = "failed"
            elif rc not in (0, None):
                job.status = "failed"
                job.error = f"claude exited with {rc}: {stderr_tail.strip()[-500:] or 'no output'}"
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
        job.finished_at = _now()
        logger.info("harness job %s %s (%d turns, %d tool calls)", job.id, job.status, job.num_turns, job.tool_calls)
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

def _ago(iso: str | None) -> str:
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


def render_sessions(sessions: list[dict]) -> str:
    if not sessions:
        return "No Claude Code sessions found on this machine."
    lines = ["Claude Code sessions (newest first):"]
    for i, s in enumerate(sessions, 1):
        state = f"● {s['status']}" if s["live"] else "○ stopped"
        proj = os.path.basename(s["project"].rstrip("/")) or s["project"]
        ref = s["name"] or s["id"][:8]
        lines.append(f"{i}. {ref} · {proj} · {state} · {_ago(s['lastActivity'])}\n   {s['title']}")
    lines.append("Use the name or id prefix with /code show <ref> or /code run <ref> <prompt>.")
    return "\n".join(lines)


def render_detail(d: dict) -> str:
    state = f"● {d['status']} (pid {d['pid']})" if d["live"] else "○ not running"
    head = [f"{d['title']}", f"session {d['id'][:8]}{(' · ' + d['name']) if d['name'] else ''} · {state}",
            f"project {d['project']}{(' @ ' + d['gitBranch']) if d['gitBranch'] else ''}",
            f"{d['messages']} messages · {d['toolCalls']} tool calls · last {_ago(d['lastActivity'])}"]
    turns = d.get("turns") or []
    if turns:
        head.append("")
        for t in turns:
            who = "You" if t["role"] == "user" else "Claude"
            head.append(f"{who}: {_short(t['text'], 400)}")
    return "\n".join(head)


def render_job(job: Job, max_chars: int | None = None) -> str:
    max_chars = max_chars or int(get_settings().get("maxResultChars") or 3500)
    where = f"session {(job.new_session_id or job.session_id or '')[:8]}"
    if job.forked:
        where += f" (forked from {job.session_id[:8]})"
    if job.status == "running":
        last = job.events[-1]["text"] if job.events else "starting"
        return f"Job {job.id} running on {where}: {job.tool_calls} tool calls so far · last: {_short(last, 200)}"
    if job.status == "done":
        body = job.result.strip() or "(no reply)"
        if len(body) > max_chars:
            body = body[: max_chars - 1] + "…"
        cost = f" · ${job.cost_usd:.2f}" if isinstance(job.cost_usd, (int, float)) else ""
        return f"Claude Code finished job {job.id} on {where} ({job.num_turns} turns, {job.tool_calls} tool calls{cost}):\n\n{body}"
    return f"Job {job.id} {job.status} on {where}: {job.error or 'no details'}"
