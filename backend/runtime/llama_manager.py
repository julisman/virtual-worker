"""Manage a single `llama-server` process so the active model is switchable.

llama.cpp serves exactly one model per process, so "switch model" = stop the
running server and start a new one pointed at the chosen GGUF. The child is
launched **detached** (its own session) and tracked via a pid file, so it
survives a uvicorn `--reload` and the next worker re-adopts it instead of
reloading a multi-GB model on every code change.

Only one model is active at a time (which also matches local memory limits).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

import config
from config import APP_HOME, LLAMA_BIN, LLAMA_CTX, LLAMA_NGL, LLAMA_PORT, MLX_BIN, MLX_VLM_BIN
from storage import settings


def _is_multimodal(path: str) -> bool:
    """An MLX model dir is multimodal if its config declares vision/audio."""
    try:
        cfg = json.loads((Path(path) / "config.json").read_text())
        return ("vision_config" in cfg) or ("audio_config" in cfg)
    except Exception:
        return False


def _backend_for(path: str) -> str:
    """Pick the engine from the model artifact: .gguf → llama.cpp; an MLX model
    directory → mlx-vlm if multimodal (vision/audio), else mlx (text)."""
    if str(path).lower().endswith(".gguf"):
        return "llamacpp"
    return "mlx-vlm" if _is_multimodal(path) else "mlx"


def _mlx_lm_available() -> bool:
    return bool(shutil.which(MLX_BIN)) or importlib.util.find_spec("mlx_lm") is not None


def _mlx_vlm_available() -> bool:
    return bool(shutil.which(MLX_VLM_BIN)) or importlib.util.find_spec("mlx_vlm") is not None

logger = logging.getLogger(__name__)

_PID_FILE = APP_HOME / "llama-server.json"
_LOG_FILE = APP_HOME / "llama-server.log"
# The last model the user activated is persisted under "activeModel" in the
# single app settings file (settings.json), so the choice survives restarts.
_ACTIVE_MODEL_KEY = "activeModel"

# In-process view of what we started/adopted. Mirrors the pid file.
_state: dict = {
    "pid": None, "path": None, "ctx": None, "ngl": None, "backend": None,
    "port": LLAMA_PORT, "started_at": None, "managed": False,
}

# Generation-probe result for the active model. /v1/models returning 200 only
# means the server is *up* — some engines (mlx_lm.server) load the weights lazily
# and only fail on the first real generation (e.g. 4-bit MoE models with missing
# params). So after starting we probe a 1-token completion and track the outcome:
#   state: "idle" (adopted/untested) | "verifying" | "ok" | "failed"
_verify: dict = {"state": "idle", "error": None, "path": None}
_verify_task = None


# --------------------------------------------------------------------------- #
# pid-file persistence (survives --reload)
# --------------------------------------------------------------------------- #

def _save_state():
    try:
        APP_HOME.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(json.dumps(_state))
    except OSError as exc:
        logger.warning("could not write llama pid file: %s", exc)


def _load_state() -> dict | None:
    try:
        return json.loads(_PID_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _clear_state():
    _state.update({"pid": None, "path": None, "started_at": None, "managed": False, "backend": None})
    try:
        _PID_FILE.unlink()
    except OSError:
        pass


def _save_pref(path: str, ctx: int, ngl: int, backend: str = "llamacpp", adapter: str | None = None):
    settings.set(_ACTIVE_MODEL_KEY,
                 {"path": path, "ctx": ctx, "ngl": ngl, "backend": backend, "adapter": adapter})


def _load_pref() -> dict | None:
    pref = settings.get(_ACTIVE_MODEL_KEY)
    if pref is None:
        # one-time migration from the old standalone file
        old = config.APP_HOME / "active-model.json"
        try:
            pref = json.loads(old.read_text())
            settings.set(_ACTIVE_MODEL_KEY, pref)
            old.unlink()
        except (OSError, json.JSONDecodeError):
            return None
    return pref


def _clear_pref():
    settings.set(_ACTIVE_MODEL_KEY, None)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _port_pids(port: int) -> list[int]:
    """PIDs listening on a TCP port (macOS/Linux via lsof)."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=4,
        ).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def _proc_name(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                              capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# health / introspection
# --------------------------------------------------------------------------- #

def _base_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or _state['port']}"


async def _healthy(port: int | None = None) -> bool:
    # /v1/models is 200-when-ready on both llama-server and mlx_lm.server.
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{_base_url(port)}/v1/models")
        return r.status_code == 200
    except Exception:
        return False


async def _active_model_id(port: int | None = None) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{_base_url(port)}/v1/models")
        data = r.json().get("data") or []
        return data[0].get("id") if data else None
    except Exception:
        return None


def _start_verify(path: str, port: int) -> None:
    """Kick off a background generation probe for the just-started model."""
    global _verify_task, _verify
    if _verify_task and not _verify_task.done():
        _verify_task.cancel()
    _verify = {"state": "verifying", "error": None, "path": path}
    try:
        _verify_task = asyncio.create_task(_run_verify(path, port))
    except RuntimeError:  # no running loop (shouldn't happen from an async caller)
        _verify_task = None


async def _run_verify(path: str, port: int) -> None:
    """Wait for the server, then probe a 1-token completion. On failure mark the
    model failed, stop the (useless) server, and forget the pref so it doesn't
    auto-reload a known-broken model on every restart."""
    global _verify
    deadline = time.time() + 300  # large models can take a while to load
    while time.time() < deadline:
        if not _pid_alive(_state.get("pid")):
            _verify = {"state": "failed", "path": path,
                       "error": _log_tail() or "model server exited during load"}
            _clear_pref()
            return
        if await _healthy(port):
            break
        await asyncio.sleep(0.5)
    else:
        _verify = {"state": "failed", "path": path,
                   "error": "timed out waiting for the model server to come up"}
        return

    model_id = await _active_model_id(port) or path
    payload = {"model": model_id, "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 1, "temperature": 0, "stream": False}
    try:
        timeout = httpx.Timeout(connect=10, read=300, write=30, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{_base_url(port)}/v1/chat/completions", json=payload)
        if r.status_code // 100 == 2:
            _verify = {"state": "ok", "error": None, "path": path}
            return
        msg = f"generation probe failed (HTTP {r.status_code}): {r.text[:600]}"
    except Exception as exc:  # noqa: BLE001 — connection reset = server crashed on load
        msg = f"generation probe failed: {exc}"

    tail = _log_tail()
    detail = (msg + ("\n\n--- server log ---\n" + tail if tail else "")).strip()
    _verify = {"state": "failed", "error": detail[:1800], "path": path}
    logger.warning("model %s failed the generation probe: %s", Path(path).name, msg[:200])
    # The model can't actually run — free its memory and don't auto-reload it.
    _kill(_state.get("pid"))
    _clear_state()
    _clear_pref()
    _invalidate_model_cache()


def _log_tail(n: int = 25) -> str:
    try:
        lines = _LOG_FILE.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def _display_name(path: str | None) -> str | None:
    """Friendly name for the active model. GGUF → the file name; MLX → the repo
    name (mlx_lm.server reports the full snapshot path as its model id)."""
    if not path:
        return None
    p = Path(path)
    if p.suffix.lower() == ".gguf":
        return p.name
    for parent in [p, *p.parents]:
        if parent.name.startswith("models--"):
            return parent.name.replace("models--", "").replace("--", "/", 1).split("/")[-1]
    return p.name


def _quant_of(path: str | None) -> str | None:
    if not path:
        return None
    try:
        from routes.hub import _quant_label  # reuse the GGUF quant parser
        return _quant_label(Path(path).name)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #

def _mlx_args(path: str, port: int, console: str, module: str, adapter: str | None = None) -> list[str]:
    base = [console] if shutil.which(console) else [sys.executable or "python3", "-m", module]
    args = base + ["--model", path, "--host", "127.0.0.1", "--port", str(port)]
    if adapter:  # serve base + a LoRA adapter (e.g. a fine-tuned VLM, which can't be fused)
        args += ["--adapter-path", adapter]
    return args


def _spawn(path: str, ctx: int, ngl: int, port: int, backend: str, adapter: str | None = None) -> int:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    if backend == "mlx-vlm":  # vision/audio models — MLX manages ctx/GPU itself
        args = _mlx_args(path, port, MLX_VLM_BIN, "mlx_vlm.server", adapter)
    elif backend == "mlx":
        args = _mlx_args(path, port, MLX_BIN, "mlx_lm.server", adapter)
    else:
        binary = shutil.which(LLAMA_BIN) or LLAMA_BIN
        args = [
            binary,
            "--model", path,
            "--alias", Path(path).stem,
            "--host", "127.0.0.1", "--port", str(port),
            "--ctx-size", str(ctx), "--n-gpu-layers", str(ngl),
            "--jinja",
        ]
    logf = open(_LOG_FILE, "ab")
    logf.write(f"\n=== starting {path} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    logf.flush()
    proc = subprocess.Popen(
        args, stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True,  # detach: own process group, survives reload
    )
    return proc.pid


def _kill(pid: int | None):
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


def stop(clear_pref: bool = True) -> dict:
    """Stop the managed llama-server (if any). ``clear_pref`` forgets the
    persisted active-model choice (so it won't auto-reload next start)."""
    global _verify
    if _verify_task and not _verify_task.done():
        _verify_task.cancel()
    pid = _state.get("pid")
    if pid:
        _kill(pid)
    _clear_state()
    _verify = {"state": "idle", "error": None, "path": None}
    if clear_pref:
        _clear_pref()
    _invalidate_model_cache()
    return {"running": False}


async def _free_port(port: int) -> bool:
    """Kill any llama-server holding the port (used for confirmed takeover).
    Refuses to kill non-llama processes. Returns True if the port is now free."""
    killed = False
    for pid in _port_pids(port):
        name = _proc_name(pid).lower()
        if any(k in name for k in ("llama", "mlx", "python")):  # our server engines
            _kill(pid)
            killed = True
    if not killed:
        return False
    for _ in range(30):  # wait up to ~3s for the socket to free
        if not await _healthy(port):
            return True
        await asyncio.sleep(0.1)
    return not await _healthy(port)


async def start(path: str, ctx: int | None = None, ngl: int | None = None,
                force: bool = False, adapter: str | None = None) -> dict:
    """Switch the active model: stop the current server, start one on `path`.
    Returns immediately with state 'loading'; poll status() for health.
    ``force=True`` takes over the port from an existing llama-server.
    ``adapter`` serves a LoRA adapter alongside the base (fine-tuned VLMs/LLMs
    that aren't fused into a standalone model)."""
    p = Path(path)
    backend = _backend_for(path)
    if backend == "llamacpp":
        if not p.is_file() or p.suffix.lower() != ".gguf":
            raise FileNotFoundError(f"not a GGUF file on disk: {path}")
        if not (shutil.which(LLAMA_BIN) or os.path.exists(LLAMA_BIN)):
            raise RuntimeError(f"{LLAMA_BIN} not found — install llama.cpp (brew install llama.cpp)")
    elif backend == "mlx-vlm":  # vision/audio MLX model
        if not p.exists():
            raise FileNotFoundError(f"MLX model not found on disk: {path}")
        if not _mlx_vlm_available():
            raise RuntimeError("mlx-vlm not found — install it with `pip install mlx-vlm` (Apple Silicon)")
    else:  # text MLX model
        if not p.exists():
            raise FileNotFoundError(f"MLX model not found on disk: {path}")
        if not _mlx_lm_available():
            raise RuntimeError("mlx-lm not found — install it with `pip install mlx-lm` (Apple Silicon)")

    stop(clear_pref=False)  # one model at a time; we'll re-save the pref below

    # If something we don't manage still holds the port, we can't bind.
    if await _healthy():
        if not force:
            raise RuntimeError(
                f"port {_state['port']} is already in use by a server Virtual Worker didn't start "
                "(e.g. a manual `llama-server` or dev.sh). Stop it first, then switch here.")
        if not await _free_port(_state["port"]):
            raise RuntimeError(
                f"couldn't free port {_state['port']} — a non-llama process is using it; "
                "stop it manually.")

    ctx = ctx or LLAMA_CTX
    ngl = ngl if ngl is not None else LLAMA_NGL
    pid = _spawn(str(p), ctx, ngl, _state["port"], backend, adapter)
    _state.update({"pid": pid, "path": str(p), "ctx": ctx, "ngl": ngl, "backend": backend,
                   "adapter": adapter, "started_at": time.time(), "managed": True})
    _save_state()
    _save_pref(str(p), ctx, ngl, backend, adapter)  # remember this choice across restarts
    _invalidate_model_cache()
    _start_verify(str(p), _state["port"])  # confirm it can actually generate
    logger.info("started %s pid=%s model=%s", backend, pid, p.name)
    return await status()


async def status() -> dict:
    pid = _state.get("pid")
    alive = _pid_alive(pid)
    reachable = await _healthy() if alive else False
    vstate = _verify.get("state")

    # A model is only "ready" once it has actually generated. While the probe runs
    # we report 'loading'; if it (or the process) failed we report 'failed' + why.
    error = None
    failed = vstate == "failed"
    if failed:
        error = _verify.get("error")
    elif _state.get("managed") and pid and not alive:
        # process died during/after load with no probe verdict — use the log tail
        error = _log_tail()
        _clear_state()

    # Optimistic readiness: a reachable server is "ready". The background probe
    # only ever DOWNGRADES to 'failed' (e.g. a 4-bit MoE that can't load) — it
    # never gates readiness, so a slow first generation can't wedge the UI in
    # "loading forever" (especially heavy vision models on mlx_vlm).
    healthy = reachable and not failed
    loading = alive and not reachable and not failed

    active_id = await _active_model_id() if reachable else None
    # keep the failed model's path for display even after we tore the server down
    path = _state.get("path") or (_verify.get("path") if failed else None)
    backend = _state.get("backend") or (_backend_for(path) if path else None)
    return {
        "running": alive,
        "healthy": healthy,
        "loading": loading,
        "failed": failed,
        "verifying": reachable and vstate == "verifying",
        "managed": _state.get("managed", False),
        "backend": backend,
        "pid": pid,
        "port": _state["port"],
        "modelPath": path,
        "modelName": _display_name(path) or active_id,
        "quant": _quant_of(path),
        "ctx": _state.get("ctx"),
        "ngl": _state.get("ngl"),
        "binAvailable": bool(shutil.which(LLAMA_BIN)),  # back-compat (llama.cpp)
        "binLlamacpp": bool(shutil.which(LLAMA_BIN)),
        "binMlx": _mlx_lm_available(),
        "binMlxVlm": _mlx_vlm_available(),
        "error": error,
    }


async def adopt_or_autostart() -> dict:
    """Called on app startup. Re-adopt a server that survived a reload / is
    already running; otherwise autostart the configured default model."""
    if not config.MANAGED_LLAMA:
        return {"running": False, "managed": False}

    saved = _load_state()
    if saved and _pid_alive(saved.get("pid")):
        _state.update(saved)
        _invalidate_model_cache()
        logger.info("adopted existing llama-server pid=%s", saved["pid"])
        return await status()

    if await _healthy():  # something else is already serving on the port
        logger.info("a server is already healthy on port %s — not autostarting", _state["port"])
        return await status()

    # Prefer the model the user last activated (persisted). path may be a .gguf
    # file (llama.cpp) or a model directory (MLX) — start() infers the engine.
    # If it fails (e.g. mlx-lm not installed) DON'T silently fall back to a
    # different default — leave nothing active so the failure is visible.
    pref = _load_pref()
    if pref and Path(pref.get("path", "")).exists():
        try:
            return await start(pref["path"], ctx=pref.get("ctx"), ngl=pref.get("ngl"),
                               adapter=pref.get("adapter"))
        except Exception as exc:
            logger.warning("reloading persisted model (%s) failed: %s", pref.get("path"), exc)
            return await status()

    # No prior choice → start the configured default (first run).
    if config.LLAMA_MODEL and Path(config.LLAMA_MODEL).is_file():
        try:
            return await start(config.LLAMA_MODEL)
        except Exception as exc:
            logger.warning("llama autostart failed: %s", exc)
    return await status()


def _invalidate_model_cache():
    try:
        from gateway import models
        models.invalidate_cache()
    except Exception:
        pass
