"""Manage a local SearXNG process — the web-search backend for browser_search.

Mirrors ``llama_manager``: if SearXNG is installed under ``SEARXNG_HOME`` (a venv
+ source tree, set up once via ``~/.virtual-worker/searxng/run.sh``'s install step), the
backend spawns it **detached** on ``SEARXNG_PORT`` at startup and re-adopts it
across a uvicorn ``--reload`` via a pid file — so ``browser_search`` just works
with no URL to configure. If it isn't installed, this is a no-op and search falls
back to whatever the agent/env configures (or a setup hint).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

import config

logger = logging.getLogger(__name__)

# Paths/port are resolved at call time from config, so a monkeypatched
# config.APP_HOME / SEARXNG_HOME (tests) or an env override is honored.
def _pid_file() -> Path: return Path(config.APP_HOME) / "searxng.json"
def _log_file() -> Path: return Path(config.APP_HOME) / "searxng.log"
def _venv_py() -> Path: return Path(config.SEARXNG_HOME) / "venv" / "bin" / "python"
def _src_dir() -> Path: return Path(config.SEARXNG_HOME) / "src"
def _settings() -> Path: return Path(config.SEARXNG_HOME) / "settings.yml"


def installed() -> bool:
    """True when the SearXNG venv + source tree are present."""
    return _venv_py().exists() and (_src_dir() / "searx" / "webapp.py").exists()


def base_url() -> str:
    return f"http://127.0.0.1:{config.SEARXNG_PORT}"


# --- pid-file state (survives uvicorn --reload) ----------------------------- #

def _save_pid(pid: int) -> None:
    import json
    try:
        _pid_file().write_text(json.dumps({"pid": pid, "port": config.SEARXNG_PORT}))
    except Exception:  # noqa: BLE001
        pass


def _load_pid() -> int | None:
    import json
    try:
        return int(json.loads(_pid_file().read_text()).get("pid"))
    except Exception:  # noqa: BLE001
        return None


def _clear_pid() -> None:
    try:
        _pid_file().unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return bool(pid) if isinstance(pid, int) else False
    except OSError:
        return False


async def _healthy() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(base_url() + "/", follow_redirects=True)
            return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


def _spawn() -> int:
    Path(config.APP_HOME).mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SEARXNG_SETTINGS_PATH": str(_settings())}
    logf = open(_log_file(), "ab")
    logf.write(f"\n=== starting searxng @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    logf.flush()
    proc = subprocess.Popen(
        [str(_venv_py()), "-m", "searx.webapp"],
        cwd=str(_src_dir()), env=env, stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True,  # detach: own process group, survives reload
    )
    return proc.pid


def _kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(20):  # up to ~2s for graceful shutdown
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def adopt_or_autostart() -> dict:
    """If SearXNG is already up, adopt it; else autostart it (when managed +
    installed). Best-effort — never raises into the startup path."""
    if not config.MANAGED_SEARXNG:
        return {"managed": False, "reason": "disabled"}
    if not installed():
        return {"managed": False, "reason": "not installed"}
    if await _healthy():                       # something already serving the port
        return {"managed": True, "running": True, "adopted": True, "url": base_url()}
    pid = _spawn()
    _save_pid(pid)
    for _ in range(60):                        # up to ~30s for first boot
        if await _healthy():
            logger.info("searxng: started (pid %s) on %s", pid, base_url())
            return {"managed": True, "running": True, "pid": pid, "url": base_url()}
        if not _pid_alive(pid):
            break
        time.sleep(0.5)
    logger.warning("searxng: failed to become healthy (see %s)", _log_file())
    return {"managed": True, "running": False, "reason": "unhealthy"}


def stop() -> None:
    """Stop the managed SearXNG (if we started it)."""
    _kill(_load_pid())
    _clear_pid()


async def status() -> dict:
    return {
        "managed": config.MANAGED_SEARXNG,
        "installed": installed(),
        "running": await _healthy(),
        "url": base_url(),
        "port": config.SEARXNG_PORT,
    }
