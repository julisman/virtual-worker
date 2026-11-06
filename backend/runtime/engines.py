"""Inference-engine versions + installs/updates (llama.cpp, mlx-lm, mlx-vlm, mlx).

Powers the Settings → Engines panel: report the installed version of each
inference runtime, check the latest published version (PyPI for the MLX
packages, Homebrew/GitHub for llama.cpp), and install or update one from the UI.

Two homes for an engine:
- Dev (repo checkout): the MLX packages live in the backend venv and update via
  ``uv pip`` against this interpreter; llama.cpp is Homebrew's.
- Packaged (desktop app): the frozen backend deliberately excludes MLX and
  ships a fixed llama-server, so engines are *managed* under APP_HOME/engines —
  a uv venv for the MLX packages (uv is bundled with the app and can download
  its own CPython), and a newer llama-server release than the bundled one.
  config.mlx_bin()/llama_bin() prefer the managed copies automatically.

Reading installed versions is local + fast. Checking *latest* needs the network
(it's an explicit user action).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx

import config

logger = logging.getLogger(__name__)

# This interpreter == the backend venv; dev-mode pip updates target it.
_VENV_PY = sys.executable or "python3"

_LLAMA_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
_LLAMA_RELEASE_DL = "https://github.com/ggml-org/llama.cpp/releases/download"

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
    llama = config.llama_bin()  # managed copy > bundled/brew
    binary = shutil.which(llama) or llama
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    text = (out.stderr or "") + (out.stdout or "")
    m = re.search(r"version:\s*(\S+)", text)  # "version: 9301 (1c0f6db54)"
    return m.group(1) if m else None


def _managed_pip_installed(pkg: str) -> str | None:
    """Version of ``pkg`` in the managed MLX venv, read from its dist-info dir
    name (cheap — no subprocess into the venv)."""
    norm = pkg.replace("-", "_")
    for info in sorted(config.MLX_VENV.glob(f"lib/python*/site-packages/{norm}-*.dist-info")):
        return info.name[len(norm) + 1:-len(".dist-info")]
    return None


def _pip_installed(pkg: str) -> str | None:
    managed = _managed_pip_installed(pkg)
    if managed:
        return managed
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
    if engine["kind"] == "brew":
        # Packaged: self-update by downloading a release binary (macOS only).
        if config.PACKAGED:
            return platform.system() == "Darwin"
        return bool(shutil.which("brew"))
    return bool(config.uv_bin())


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


async def _github_latest_llamacpp() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
            r = await c.get(_LLAMA_RELEASE_API)
        return r.json().get("tag_name")  # e.g. "b9881"
    except Exception as exc:  # noqa: BLE001
        logger.debug("github latest llama.cpp failed: %s", exc)
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
    # brew / build-number: compare leading integers ("9301" vs "b9881")
    cn, ln = _digits(current_v), _digits(latest)
    if cn is not None and ln is not None:
        return ln > cn
    return str(latest) != str(current_v)


async def _latest(engine: dict) -> str | None:
    if engine["kind"] == "brew":
        # Packaged updates install GitHub release binaries, so compare against
        # the GitHub tag; brew's formula can lag it by days.
        if config.PACKAGED:
            return await _github_latest_llamacpp()
        return await _brew_latest(engine["formula"])
    return await _pypi_latest(engine["pkg"])


async def check() -> list[dict]:
    """Installed + latest + updateAvailable for each engine (hits the network)."""
    rows = await asyncio.to_thread(current)  # shells out for the llama version
    latests = await asyncio.gather(*[_latest(e) for e in ENGINES])
    for row, eng, latest in zip(rows, ENGINES, latests):
        row["latest"] = latest
        row["updateAvailable"] = _update_available(row["installed"], latest, eng["kind"])
    return rows


# --------------------------------------------------------------------------- #
# install / update
# --------------------------------------------------------------------------- #

def _pip_target_python() -> str:
    """Interpreter uv installs into: the managed venv when packaged (or when it
    already exists), else the backend's own venv (dev)."""
    venv_py = config.mlx_venv_python()
    if config.PACKAGED or venv_py.exists():
        return str(venv_py)
    return _VENV_PY


def _update_cmd(engine: dict) -> list[str] | None:
    if engine["kind"] == "brew":
        brew = shutil.which("brew")
        return [brew, "upgrade", engine["formula"]] if brew else None
    uv = config.uv_bin()
    return [uv, "pip", "install", "--python", _pip_target_python(), "-U", engine["pkg"]] if uv else None


async def _run(cmd: list[str], timeout: float, env: dict | None = None) -> tuple[int | None, str]:
    """Run a command capturing combined output. Returns (returncode, output)."""
    logger.info("engines: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, **(env or {})})
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return None, f"timed out (>{int(timeout)}s)"
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to start: {exc}"
    return proc.returncode, (out or b"").decode("utf-8", "replace")


async def _ensure_mlx_venv() -> str | None:
    """Create the managed MLX venv if missing. Returns an error string or None.

    uv downloads a managed CPython when the host has none; it's kept under
    APP_HOME/engines/python so the whole engine stays inside Virtual Worker's data dir.
    """
    if config.mlx_venv_python().exists():
        return None
    uv = config.uv_bin()
    if not uv:
        return "uv not found — cannot create the MLX engine environment"
    config.ENGINES_DIR.mkdir(parents=True, exist_ok=True)
    env = {"UV_PYTHON_INSTALL_DIR": str(config.ENGINES_DIR / "python")}
    code, out = await _run([uv, "venv", "--python", "3.12", str(config.MLX_VENV)],
                           timeout=600, env=env)
    if code != 0:
        return f"creating the MLX engine venv failed:\n{out[-2000:]}"
    return None


def _extract_llama_release(tar_path: Path, dest: Path) -> list[str]:
    """Copy llama-server + its dylibs/metal shaders out of a release tarball,
    flattened into ``dest`` (basenames only — no archive paths touch disk)."""
    copied: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            name = Path(member.name).name
            if not member.isfile():
                continue
            if name == "llama-server" or name.endswith((".dylib", ".metal")):
                src = tf.extractfile(member)
                if src is None:
                    continue
                with open(dest / name, "wb") as f:
                    shutil.copyfileobj(src, f)
                copied.append(name)
    server = dest / "llama-server"
    if server.exists():
        server.chmod(0o755)
    return copied


async def _update_llamacpp_release() -> dict:
    """Packaged-mode llama.cpp update: download the latest release binary into
    APP_HOME/engines/llama (config.llama_bin() prefers it over the bundled copy)."""
    if platform.system() != "Darwin":
        return {"ok": False, "output": "managed llama.cpp updates are macOS-only for now"}
    tag = await _github_latest_llamacpp()
    if not tag:
        return {"ok": False, "output": "could not resolve the latest llama.cpp release (network?)"}
    arch = "macos-arm64" if platform.machine() == "arm64" else "macos-x64"
    asset = f"llama-{tag}-bin-{arch}.tar.gz"
    url = f"{_LLAMA_RELEASE_DL}/{tag}/{asset}"
    try:
        with tempfile.TemporaryDirectory(prefix="vw-llama-") as tmp:
            tar_path = Path(tmp) / asset
            async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=20),
                                         follow_redirects=True) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code != 200:
                        return {"ok": False,
                                "output": f"download failed ({r.status_code}) for {url}"}
                    with open(tar_path, "wb") as f:
                        async for chunk in r.aiter_bytes():
                            f.write(chunk)
            files = await asyncio.to_thread(
                _extract_llama_release, tar_path, config.LLAMA_ENGINE_DIR)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": f"llama.cpp update failed: {exc}"}
    if "llama-server" not in files:
        return {"ok": False, "output": f"release {tag} had no llama-server for {arch}"}
    (config.LLAMA_ENGINE_DIR / ".tag").write_text(tag)
    return {
        "ok": True, "returncode": 0,
        "installed": _llamacpp_installed(),  # post-update version (managed copy)
        "output": f"installed llama.cpp {tag} ({len(files)} files) into {config.LLAMA_ENGINE_DIR}",
    }


async def update(key: str) -> dict:
    """Install or update an engine. Returns {ok, returncode, installed, output}."""
    engine = _by_key(key)
    if not engine:
        return {"ok": False, "output": f"unknown engine: {key}"}
    if engine["kind"] == "brew" and config.PACKAGED:
        return await _update_llamacpp_release()
    if engine["kind"] == "pip" and _pip_target_python() == str(config.mlx_venv_python()):
        err = await _ensure_mlx_venv()
        if err:
            return {"ok": False, "output": err}
    cmd = _update_cmd(engine)
    if not cmd:
        tool = "brew" if engine["kind"] == "brew" else "uv"
        return {"ok": False, "output": f"{tool} not found on PATH — cannot update {key}"}
    code, text = await _run(cmd, timeout=900)
    return {
        "ok": code == 0,
        "returncode": code,
        "installed": _installed(engine),  # post-update version
        "output": text[-6000:],
    }
