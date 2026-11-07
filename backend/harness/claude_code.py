"""Claude Code harness: see your Claude Code sessions and send prompts into them.

Claude Code keeps everything this module needs on disk under ``~/.claude``:

* ``sessions/<pid>.json``: the live-session registry (one file per running
  process: session id, project directory, derived name, ``busy``/``idle``).
* ``projects/<slug>/<session-id>.jsonl``: the transcript of every session,
  with an ``ai-title`` record, the last prompt, and every user/assistant turn.

Listing and monitoring only read those files. Sending a prompt runs
``claude -p <prompt> --resume <id> --output-format stream-json`` through the
shared job supervisor in :mod:`harness.base`; the final ``result`` record
carries the answer, cost and turn count. When the target session is open in a
terminal the run forks it (``--fork-session``) so two processes never write
the same transcript.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from harness import base
from harness.base import (  # noqa: F401 — re-exported for callers and tests
    JOBS, HarnessError, Job, get_job, get_settings, list_jobs, render_detail, render_job,
    set_settings, shutdown, stop,
)

logger = logging.getLogger(__name__)

KEY = "claude"
LABEL = "Claude Code"


def _short(text: str, n: int = 160) -> str:
    return base.short(text, n)


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
        txt = (out.stdout or out.stderr).strip()
        ver = txt.splitlines()[0] if txt else "unknown"
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


def _iso(ms) -> str | None:
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
            "id": rec.get("sessionId") or "", "pid": pid, "cwd": rec.get("cwd") or "",
            "name": rec.get("name") or "", "kind": rec.get("kind") or "interactive",
            "status": rec.get("status") or "unknown", "startedAt": _iso(rec.get("startedAt")),
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
    if cached and cached[0] == mtime:
        d = dict(cached[1]); d["turns"] = d["_all"][-turns:] if turns else []
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
                        continue        # slash-command / system records wrapped in tags
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
                    exchanges.append({"role": t, "text": text, "ts": ts})
                    if len(exchanges) > 80:
                        exchanges = exchanges[-80:]
    except OSError:
        return {}
    users = [e for e in exchanges if e["role"] == "user"]
    if users and not info["lastPrompt"]:
        info["lastPrompt"] = users[-1]["text"]
    info["_all"] = exchanges
    _scan_cache[key] = (mtime, dict(info))
    if len(_scan_cache) > 200:
        _scan_cache.pop(next(iter(_scan_cache)))
    d = dict(info); d["turns"] = exchanges[-turns:] if turns else []
    return d


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
            "id": sid, "harness": KEY,
            "title": info.get("title") or _short(info.get("lastPrompt") or "", 60) or "(untitled)",
            "project": (l or {}).get("cwd") or info.get("cwd") or "",
            "live": bool(l), "status": (l or {}).get("status") or "stopped",
            "name": (l or {}).get("name") or "", "pid": (l or {}).get("pid"),
            "lastActivity": info.get("lastTs"), "lastPrompt": _short(info.get("lastPrompt") or ""),
            "lastAssistant": _short(info.get("lastAssistant") or ""), "messages": info.get("messages", 0),
        })
        seen.add(sid)
    for sid, l in live.items():
        if sid not in seen and (not project or project in l["cwd"]):
            out.append({"id": sid, "harness": KEY, "title": l["name"] or "(new)", "project": l["cwd"], "live": True,
                        "status": l["status"], "name": l["name"], "pid": l["pid"],
                        "lastActivity": l["updatedAt"], "lastPrompt": "", "lastAssistant": "", "messages": 0})
    out.sort(key=lambda s: s.get("lastActivity") or "", reverse=True)
    out.sort(key=lambda s: not s["live"])
    return out[:limit]


def resolve_session(ref: str) -> dict | None:
    """Find a session by full id, id prefix, or live name (or part of it)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    sessions = list_sessions(limit=200)
    for s in sessions:
        if s["id"] == ref or (s["name"] and s["name"] == ref):
            return s
    cands = [s for s in sessions if s["id"].startswith(ref) or (s["name"] and ref.lower() in s["name"].lower())]
    return cands[0] if len(cands) == 1 else None


def session_detail(session_id: str, turns: int = 6) -> dict | None:
    p = _transcript_path(session_id)
    live = next((s for s in live_sessions() if s["id"] == session_id), None)
    if p is None and live is None:
        return None
    info = scan_transcript(p, turns=turns) if p else {}
    return {
        "id": session_id, "harness": KEY, "assistantName": "Claude",
        "title": info.get("title") or (live or {}).get("name") or "(untitled)",
        "project": (live or {}).get("cwd") or info.get("cwd") or "",
        "gitBranch": info.get("gitBranch") or "",
        "live": bool(live), "status": (live or {}).get("status") or "stopped",
        "name": (live or {}).get("name") or "", "pid": (live or {}).get("pid"),
        "firstActivity": info.get("firstTs"),
        "lastActivity": info.get("lastTs") or (live or {}).get("updatedAt"),
        "messages": info.get("messages", 0), "toolCalls": info.get("toolCalls", 0),
        "turns": info.get("turns") or [], "transcript": str(p) if p else None,
    }


# --------------------------------------------------------------------------- #
# Adapter: argv + event parsing for `claude -p --output-format stream-json`
# --------------------------------------------------------------------------- #

class _Adapter:
    key = KEY
    label = LABEL

    def availability(self) -> dict:
        return availability()

    def list_sessions(self, limit: int = 20, project: str | None = None) -> list[dict]:
        return list_sessions(limit=limit, project=project)

    def resolve_session(self, ref: str) -> dict | None:
        return resolve_session(ref)

    def session_detail(self, session_id: str, turns: int = 6) -> dict | None:
        return session_detail(session_id, turns=turns)

    def argv(self, job: Job) -> list[str]:
        cfg = get_settings()
        b = claude_bin()
        if not b:
            raise HarnessError("claude binary not found; install Claude Code or set the path in Settings → Coding harness")
        argv = [b, "-p", job.prompt, "--output-format", "stream-json", "--verbose"]
        if job.session_id:
            argv += ["--resume", job.session_id]
            if job.forked:
                argv.append("--fork-session")
        if cfg.get("permissionMode"):
            argv += ["--permission-mode", cfg["permissionMode"]]
        if int(cfg.get("maxTurns") or 0) > 0:
            argv += ["--max-turns", str(int(cfg["maxTurns"]))]
        if cfg.get("model"):
            argv += ["--model", cfg["model"]]
        return argv

    def consume(self, job: Job, rec: dict) -> None:
        t = rec.get("type")
        if t == "system" and rec.get("subtype") == "init":
            job.new_session_id = rec.get("session_id") or job.new_session_id
            base.note(job, "init", f"model {rec.get('model')} · {rec.get('cwd')}")
        elif t == "assistant":
            content = (rec.get("message") or {}).get("content") or []
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    job.last_text = b["text"]
                    base.note(job, "text", b["text"])
                elif b.get("type") == "tool_use":
                    job.tool_calls += 1
                    args = b.get("input") or {}
                    hint = args.get("command") or args.get("file_path") or args.get("pattern") or args.get("description") or ""
                    base.note(job, "tool", f"{b.get('name')} {hint}".strip())
        elif t == "result":
            job.num_turns = int(rec.get("num_turns") or 0)
            job.cost_usd = rec.get("total_cost_usd")
            job.new_session_id = rec.get("session_id") or job.new_session_id
            if rec.get("is_error"):
                job.error = str(rec.get("result") or rec.get("error") or "run failed")
            else:
                job.result = str(rec.get("result") or job.last_text or "")
        elif t == "system" and rec.get("subtype") in ("informational", "model_refusal_fallback") and rec.get("content"):
            base.note(job, "system", str(rec["content"]))

    def finish(self, job: Job) -> None:
        return None


ADAPTER = base.register(_Adapter())


async def start(prompt: str, *, session: str | None = None, project: str | None = None,
                origin: dict | None = None, on_done: base.OnDone | None = None) -> Job:
    return await base.start_job(ADAPTER, prompt, session=session, project=project, origin=origin, on_done=on_done)


def render_sessions(sessions: list[dict]) -> str:
    return base.render_sessions(sessions, LABEL, "/code")
