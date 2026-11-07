"""Codex harness: OpenAI's Codex CLI, driven the same way as Claude Code.

Codex stores every session as a rollout under
``~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`` with a
``session_meta`` record (id, cwd, model provider) followed by ``event_msg``
records (``user_message``, ``agent_message``, ``task_started``,
``task_complete``, ``token_count``) and raw ``response_item`` records. There
is no live-process registry, so a Codex session counts as live only while a
job started from here is running on it.

Prompts run as ``codex exec --json`` (new session, ``-C <dir>``) or
``codex exec resume <id> --json`` and stream ``thread.started``,
``item.completed`` (agent_message, command_execution, file_change, …),
``turn.completed`` (usage) and ``turn.failed`` events. The final answer is also
written with ``-o`` to a temp file, which is the source of truth for the
result regardless of event parsing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import config
from harness import base
from harness.base import HarnessError, Job, get_settings

logger = logging.getLogger(__name__)

KEY = "codex"
LABEL = "Codex"


def codex_home() -> Path:
    return Path(config.CODEX_HOME)


def codex_bin() -> str | None:
    cand = (get_settings().get("codexBin") or config.CODEX_BIN or "").strip()
    if cand:
        return cand if os.path.isabs(cand) and os.access(cand, os.X_OK) else shutil.which(cand)
    return shutil.which("codex")


_version_cache: dict = {"bin": None, "version": None, "at": 0.0}


def availability() -> dict:
    b = codex_bin()
    if not b:
        return {"available": False, "bin": None, "version": None,
                "error": "codex binary not found; install the Codex CLI or set the path in Settings"}
    now = time.time()
    if _version_cache["bin"] == b and now - _version_cache["at"] < 60:
        return {"available": True, "bin": b, "version": _version_cache["version"], "error": None}
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20)
        txt = (out.stdout or out.stderr).strip()
        ver = txt.splitlines()[0] if txt else "unknown"
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "bin": b, "version": None, "error": f"codex --version failed: {exc}"}
    _version_cache.update(bin=b, version=ver, at=now)
    return {"available": True, "bin": b, "version": ver, "error": None}


# --------------------------------------------------------------------------- #
# Discovery over rollout files
# --------------------------------------------------------------------------- #

_scan_cache: dict[str, tuple[float, dict]] = {}


def _scan(path: Path) -> dict:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _scan_cache.get(key)
    if cached and cached[0] == mtime:
        return dict(cached[1])
    info: dict = {"id": "", "cwd": "", "model": "", "firstTs": None, "lastTs": None, "messages": 0,
                  "toolCalls": 0, "title": "", "lastPrompt": "", "lastAssistant": "", "all": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                t = rec.get("type")
                p = rec.get("payload") or {}
                ts = rec.get("timestamp")
                if t == "session_meta":
                    info["id"] = p.get("id") or info["id"]
                    info["cwd"] = p.get("cwd") or info["cwd"]
                    info["firstTs"] = p.get("timestamp") or ts
                elif t == "turn_context":
                    info["cwd"] = p.get("cwd") or info["cwd"]
                    info["model"] = p.get("model") or info["model"]
                elif t == "event_msg":
                    et = p.get("type")
                    if et == "user_message" and p.get("message"):
                        text = str(p["message"])
                        info["messages"] += 1
                        info["lastPrompt"] = text
                        info["title"] = info["title"] or base.short(text, 60)
                        info["lastTs"] = ts or info["lastTs"]
                        info["all"].append({"role": "user", "text": text, "ts": ts})
                    elif et == "agent_message" and p.get("message"):
                        text = str(p["message"])
                        info["messages"] += 1
                        info["lastAssistant"] = text
                        info["lastTs"] = ts or info["lastTs"]
                        info["all"].append({"role": "assistant", "text": text, "ts": ts})
                elif t == "response_item" and p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                    info["toolCalls"] += 1
                if len(info["all"]) > 80:
                    info["all"] = info["all"][-80:]
    except OSError:
        return {}
    if not info["id"]:
        stem = path.stem
        info["id"] = stem.split("-", 4)[-1] if stem.startswith("rollout-") and stem.count("-") >= 4 else stem
    _scan_cache[key] = (mtime, dict(info))
    if len(_scan_cache) > 200:
        _scan_cache.pop(next(iter(_scan_cache)))
    return dict(info)


def _rollouts() -> list[Path]:
    root = codex_home() / "sessions"
    if not root.is_dir():
        return []
    return sorted(root.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def _row(info: dict) -> dict:
    running = base.running_on(info["id"])
    return {
        "id": info["id"], "harness": KEY,
        "title": info.get("title") or "(untitled)",
        "project": info.get("cwd") or "",
        "live": bool(running), "status": "busy" if running else "stopped",
        "name": "", "pid": None,
        "lastActivity": info.get("lastTs") or info.get("firstTs"),
        "lastPrompt": base.short(info.get("lastPrompt") or ""),
        "lastAssistant": base.short(info.get("lastAssistant") or ""),
        "messages": info.get("messages", 0),
    }


def list_sessions(limit: int = 20, project: str | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for p in _rollouts()[: max(limit * 3, 30)]:
        info = _scan(p)
        if not info or not info.get("id") or info["id"] in seen or not info.get("messages"):
            continue
        if project and project not in (info.get("cwd") or ""):
            continue
        out.append(_row(info))
        seen.add(info["id"])
    out.sort(key=lambda s: s.get("lastActivity") or "", reverse=True)
    out.sort(key=lambda s: not s["live"])
    return out[:limit]


def _find(session_id: str) -> Path | None:
    for p in _rollouts():
        if p.stem.endswith(session_id):
            return p
    return None


def resolve_session(ref: str) -> dict | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    sessions = list_sessions(limit=200)
    for s in sessions:
        if s["id"] == ref:
            return s
    cands = [s for s in sessions if s["id"].startswith(ref)]
    return cands[0] if len(cands) == 1 else None


def session_detail(session_id: str, turns: int = 6) -> dict | None:
    p = _find(session_id)
    if not p:
        return None
    info = _scan(p)
    row = _row(info)
    return {**row, "assistantName": "Codex", "gitBranch": "", "model": info.get("model") or "",
            "firstActivity": info.get("firstTs"), "toolCalls": info.get("toolCalls", 0),
            "turns": (info.get("all") or [])[-turns:] if turns else [], "transcript": str(p)}


# --------------------------------------------------------------------------- #
# Adapter: argv + event parsing for `codex exec --json`
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
        b = codex_bin()
        if not b:
            raise HarnessError("codex binary not found; install the Codex CLI or set the path in Settings → Coding harness")
        fd, out = tempfile.mkstemp(prefix="vw-codex-", suffix=".txt")
        os.close(fd)
        job.scratch["outfile"] = out
        sandbox = cfg.get("codexSandbox") or "workspace-write"
        common = ["--json", "--skip-git-repo-check", "-o", out]
        if cfg.get("codexModel"):
            common += ["-m", cfg["codexModel"]]
        if job.session_id:
            # `exec resume` has no -s/-C; the sandbox comes through a config override.
            return [b, "exec", "resume", job.session_id, job.prompt, *common, "-c", f'sandbox_mode="{sandbox}"']
        return [b, "exec", *common, "-s", sandbox, "-C", job.cwd, job.prompt]

    def consume(self, job: Job, rec: dict) -> None:
        t = rec.get("type")
        if t == "thread.started":
            job.new_session_id = rec.get("thread_id") or job.new_session_id
            base.note(job, "init", f"thread {rec.get('thread_id', '')[:8]}")
        elif t == "turn.started":
            job.num_turns += 1
        elif t in ("item.completed", "item.started", "item.updated"):
            item = rec.get("item") or {}
            it = item.get("type")
            if t != "item.completed":
                return
            if it == "agent_message" and item.get("text"):
                job.last_text = str(item["text"])
                base.note(job, "text", job.last_text)
            elif it in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                job.tool_calls += 1
                hint = item.get("command") or item.get("server") or item.get("query") or ""
                if it == "file_change":
                    hint = ", ".join(c.get("path", "") for c in (item.get("changes") or []) if isinstance(c, dict))
                base.note(job, "tool", f"{it} {hint}".strip())
            elif it == "error" and item.get("message"):
                base.note(job, "system", str(item["message"]))
        elif t == "turn.completed":
            usage = rec.get("usage") or {}
            if usage:
                base.note(job, "usage", f"in {usage.get('input_tokens', 0)} · out {usage.get('output_tokens', 0)}")
        elif t == "turn.failed":
            job.error = str((rec.get("error") or {}).get("message") or "turn failed")
        elif t == "error" and rec.get("message"):
            job.error = str(rec["message"])

    def finish(self, job: Job) -> None:
        out = job.scratch.pop("outfile", None)
        if not out:
            return
        try:
            text = Path(out).read_text(encoding="utf-8", errors="replace").strip()
            if text and not job.error:
                job.result = text
        except OSError:
            pass
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass


ADAPTER = base.register(_Adapter())


async def start(prompt: str, *, session: str | None = None, project: str | None = None,
                origin: dict | None = None, on_done: base.OnDone | None = None) -> Job:
    return await base.start_job(ADAPTER, prompt, session=session, project=project, origin=origin, on_done=on_done)


def render_sessions(sessions: list[dict]) -> str:
    return base.render_sessions(sessions, LABEL, "/codex")
