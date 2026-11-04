"""Inference-engine versions + updates (llama.cpp, mlx-lm, mlx-vlm, mlx).

Powers the Settings → Engines panel: report the installed version of each
inference runtime, check the latest published version (PyPI for the MLX packages,
the Homebrew formulae API for llama.cpp), and update one through the UI.

Reading installed versions is local + fast. Checking *latest* needs the network
(it's an explicit user action). Updates shell out to ``uv`` (pip packages, since
the uv venv has no pip) or ``brew`` (llama.cpp).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import re
import shutil
import subprocess
import sys

import httpx

import config

logger = logging.getLogger(__name__)

# This interpreter == the backend venv; updates target it.
_VENV_PY = sys.executable or "python3"

# Each engine: how to read its version + how to update it.
ENGINES: list[dict] = [
    {"key": "llama.cpp", "label": "llama.cpp", "sub": "GGUF runtime (llama-server)",
     "kind": "brew", "formula": "llama.cpp"},
    {"key": "mlx-lm", "label": "mlx-lm", "sub": "MLX text models", "kind": "pip", "pkg": "mlx-lm"},
    {"key": "mlx-vlm", "label": "mlx-vlm", "sub": "MLX vision/audio models", "kind": "pip", "pkg": "mlx-vlm"},
    {"key": "mlx", "label": "mlx", "sub": "MLX core", "kind": "pip", "pkg": "mlx"},
]


def _by_key(key: str) -> dict | None:
    return next((e for e in ENGINES if e["key"] == key), None)


def _digits(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


# --------------------------------------------------------------------------- #
# installed versions (local)
# --------------------------------------------------------------------------- #

def _llamacpp_installed() -> str | None:
    binary = shutil.which(config.LLAMA_BIN) or config.LLAMA_BIN
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    text = (out.stderr or "") + (out.stdout or "")
    m = re.search(r"version:\s*(\S+)", text)  # "version: 9301 (1c0f6db54)"
    return m.group(1) if m else None


def _pip_installed(pkg: str) -> str | None:
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _installed(engine: dict) -> str | None:
    if engine["kind"] == "brew":
        return _llamacpp_installed()
    return _pip_installed(engine["pkg"])


def _can_update(engine: dict) -> bool:
    return bool(shutil.which("brew")) if engine["kind"] == "brew" else bool(shutil.which("uv"))


def current() -> list[dict]:
    """Installed version of each engine (no network)."""
    out = []
    for e in ENGINES:
        ver = _installed(e)
        out.append({
            "key": e["key"], "label": e["label"], "sub": e["sub"],
            "manager": e["kind"], "installed": ver, "available": ver is not None,
            "canUpdate": _can_update(e),
        })
    return out


# --------------------------------------------------------------------------- #
# latest versions (network) + comparison
# --------------------------------------------------------------------------- #

async def _pypi_latest(pkg: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://pypi.org/pypi/{pkg}/json")
        return r.json().get("info", {}).get("version")
    except Exception as exc:  # noqa: BLE001
        logger.debug("pypi latest %s failed: %s", pkg, exc)
        return None


async def _brew_latest(formula: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://formulae.brew.sh/api/formula/{formula}.json")
        return (r.json().get("versions") or {}).get("stable")
    except Exception as exc:  # noqa: BLE001
        logger.debug("brew latest %s failed: %s", formula, exc)
        return None


def _update_available(current_v: str | None, latest: str | None, kind: str) -> bool:
    if not current_v or not latest:
        return False
    if kind == "pip":
        try:
            from packaging.version import parse
            return parse(str(latest)) > parse(str(current_v))
        except Exception:
            return str(latest) != str(current_v)
    # brew / build-number: compare leading integers
    cn, ln = _digits(current_v), _digits(latest)
    if cn is not None and ln is not None:
        return ln > cn
    return str(latest) != str(current_v)


async def _latest(engine: dict) -> str | None:
    if engine["kind"] == "brew":
        return await _brew_latest(engine["formula"])
    return await _pypi_latest(engine["pkg"])


async def check() -> list[dict]:
    """Installed + latest + updateAvailable for each engine (hits the network)."""
    rows = current()
    latests = await asyncio.gather(*[_latest(e) for e in ENGINES])
    for row, eng, latest in zip(rows, ENGINES, latests):
        row["latest"] = latest
        row["updateAvailable"] = _update_available(row["installed"], latest, eng["kind"])
    return rows


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #

def _update_cmd(engine: dict) -> list[str] | None:
    if engine["kind"] == "brew":
        brew = shutil.which("brew")
        return [brew, "upgrade", engine["formula"]] if brew else None
    uv = shutil.which("uv")
    return [uv, "pip", "install", "--python", _VENV_PY, "-U", engine["pkg"]] if uv else None


async def update(key: str) -> dict:
    """Run the engine's update command. Returns {ok, returncode, output}."""
    engine = _by_key(key)
    if not engine:
        return {"ok": False, "output": f"unknown engine: {key}"}
    cmd = _update_cmd(engine)
    if not cmd:
        tool = "brew" if engine["kind"] == "brew" else "uv"
        return {"ok": False, "output": f"{tool} not found on PATH — cannot update {key}"}
    logger.info("updating engine %s: %s", key, " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        return {"ok": False, "output": f"updating {key} timed out (>15m)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": f"update failed to start: {exc}"}
    text = (out or b"").decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "installed": _installed(engine),  # post-update version
        "output": text[-6000:],
    }
