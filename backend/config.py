"""Runtime configuration and paths for Virtual Worker."""

import json
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Packaged mode: when Virtual Worker ships as a bundled desktop app (Electron), the launcher
# sets VW_PACKAGED=1. Frozen backend + no dev repo on disk → pick user-facing
# defaults (state under the OS app-data dir, the bundled llama-server binary, the
# built frontend served by the backend). All still overridable by explicit env.
PACKAGED = os.environ.get("VW_PACKAGED", "0") not in ("0", "false", "False")
# Directory the frozen backend / bundled resources live in (set by the launcher).
RESOURCES_DIR = Path(os.environ.get("VW_RESOURCES_DIR") or Path(__file__).resolve().parent)


def _system_chrome_installed() -> bool:
    """Is real Google Chrome installed on this machine?"""
    if sys.platform == "darwin":
        return any(Path(p, "Google Chrome.app").exists()
                   for p in ("/Applications", str(Path.home() / "Applications")))
    if os.name == "nt":
        return any(Path(base, "Google", "Chrome", "Application", "chrome.exe").exists()
                   for base in filter(None, (os.environ.get("PROGRAMFILES"),
                                             os.environ.get("PROGRAMFILES(X86)"),
                                             os.environ.get("LOCALAPPDATA"))))
    return bool(shutil.which("google-chrome") or shutil.which("google-chrome-stable"))


# Which browser Playwright drives. "chrome" = the system-installed Google Chrome —
# the browser the user actually lives in (real UA, corporate SSO, no "Chrome for
# Testing" branding); the default whenever it's installed. Empty = Playwright's
# own Chromium (`playwright install chromium`), the fallback for machines without
# Chrome. Override with VW_BROWSER_CHANNEL (e.g. "" to force bundled Chromium,
# "msedge" for Edge). The packaged app never bundles Chromium, so it's always
# "chrome" there.
BROWSER_CHANNEL = os.environ.get(
    "VW_BROWSER_CHANNEL",
    "chrome" if (PACKAGED or _system_chrome_installed()) else "")


def _default_home() -> Path:
    """Where app state lives. Dev: ~/.virtual-worker. Packaged: the OS app-data dir so it
    survives app upgrades and matches platform conventions."""
    if os.environ.get("VW_HOME"):
        return Path(os.environ["VW_HOME"])
    if PACKAGED:
        if os.name == "nt":  # Windows: %APPDATA%\Virtual Worker
            base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
            return Path(base) / "Virtual Worker"
        if _is_macos():      # macOS: ~/Library/Application Support/Virtual Worker
            return Path.home() / "Library" / "Application Support" / "Virtual Worker"
    return Path.home() / ".virtual-worker"


def _is_macos() -> bool:
    import platform
    return platform.system() == "Darwin"


# App data dir (db, caches). Override with VW_HOME.
APP_HOME = _default_home()


def builtin_skills_dir() -> Path | None:
    """The read-only dir of skill packs shipped with Virtual Worker (installable from the
    Skills tab). Dev: ``<repo>/skills``. Packaged: bundled into the frozen
    backend (``sys._MEIPASS/skills``, see desktop/backend.spec). ``None`` when
    absent — the gallery is then simply empty."""
    if getattr(sys, "frozen", False):
        p = Path(getattr(sys, "_MEIPASS", RESOURCES_DIR)) / "skills"
    else:
        p = Path(__file__).resolve().parent.parent / "skills"
    return p if p.is_dir() else None

# --- Managed llama.cpp runtime --------------------------------------------- #
# When managed (default), the backend starts/stops/switches a single
# `llama-server` itself, so the active model can be changed from the UI.
#   Dev: `llama-server` on PATH (Homebrew / ~/.local/bin).
#   Packaged: the copy shipped inside the app bundle (RESOURCES_DIR/bin).
def _default_llama_bin() -> str:
    if os.environ.get("LLAMA_BIN"):
        return os.environ["LLAMA_BIN"]
    if PACKAGED:
        exe = "llama-server.exe" if os.name == "nt" else "llama-server"
        bundled = RESOURCES_DIR / "bin" / exe
        if bundled.exists():
            return str(bundled)
    return "llama-server"


LLAMA_BIN = _default_llama_bin()

# --- Managed engines --------------------------------------------------------- #
# The frozen desktop backend excludes the MLX packages (heavy, Apple-Silicon-
# only) and ships a fixed llama-server. Both are installable/updatable at
# runtime into APP_HOME/engines (runtime/engines.py):
#   engines/mlx     a uv venv holding mlx-lm + mlx-vlm (+ its managed CPython)
#   engines/llama   a newer llama-server release than the bundled one
ENGINES_DIR = APP_HOME / "engines"
MLX_VENV = ENGINES_DIR / "mlx"
LLAMA_ENGINE_DIR = ENGINES_DIR / "llama"


def uv_bin() -> str | None:
    """The uv executable: env override > bundled (packaged) > PATH."""
    if os.environ.get("VW_UV_BIN"):
        return os.environ["VW_UV_BIN"]
    exe = "uv.exe" if os.name == "nt" else "uv"
    bundled = RESOURCES_DIR / "bin" / exe
    if PACKAGED and bundled.exists():
        return str(bundled)
    import shutil
    return shutil.which("uv")


def mlx_venv_python() -> Path:
    return MLX_VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _managed_or(name: str, env: str) -> str:
    """An MLX server entry point: env override > managed venv > bare PATH name."""
    if os.environ.get(env):
        return os.environ[env]
    managed = MLX_VENV / "bin" / name
    if managed.exists():
        return str(managed)
    return name


def mlx_bin() -> str:
    return _managed_or("mlx_lm.server", "MLX_BIN")


def mlx_vlm_bin() -> str:
    return _managed_or("mlx_vlm.server", "MLX_VLM_BIN")


def llama_bin() -> str:
    """llama-server: managed (updated) copy > env/bundled default.

    The managed copy outranks LLAMA_BIN because the desktop launcher always
    sets LLAMA_BIN to the app-bundle binary — an engine update would otherwise
    never take effect."""
    managed = LLAMA_ENGINE_DIR / ("llama-server.exe" if os.name == "nt" else "llama-server")
    if managed.exists() and os.access(managed, os.X_OK):
        return str(managed)
    return _default_llama_bin()
# MLX (Apple Silicon) inference servers — optional alternative backends.
MLX_BIN = os.environ.get("MLX_BIN", "mlx_lm.server")          # text models
MLX_VLM_BIN = os.environ.get("MLX_VLM_BIN", "mlx_vlm.server")  # vision/audio models
LLAMA_MODEL = os.environ.get("LLAMA_MODEL")  # default GGUF to load on startup
LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "8080"))
# 32k default — coding agents (Claude Code/Codex) send ~25k-token system prompts,
# so 8192 overflows immediately. Override per-model from the UI or via env.
LLAMA_CTX = int(os.environ.get("LLAMA_CTX", "32768"))
LLAMA_NGL = int(os.environ.get("LLAMA_NGL", "99"))  # offload all layers to GPU/Metal
MANAGED_LLAMA = os.environ.get("VW_MANAGED_LLAMA", "1") not in ("0", "false", "False")

# Upstream OpenAI-compatible inference server the /v1 proxy forwards to. In
# managed mode it's our llama-server on LLAMA_PORT; override to point elsewhere
# (vLLM, ollama, a remote base URL) — that also disables managed-mode autostart.
UPSTREAM_URL = os.environ.get("VW_UPSTREAM_URL") or f"http://127.0.0.1:{LLAMA_PORT}"
# An explicit upstream override means "don't manage llama for me".
if os.environ.get("VW_UPSTREAM_URL"):
    MANAGED_LLAMA = False

# --- SearXNG (managed web-search backend for the browser_search tool) -------- #
# Virtual Worker can run a local SearXNG the same way it manages llama: if it's installed
# under SEARXNG_HOME, the backend spawns it on startup and browser_search uses it
# with no URL to set. Install once: see docs / ~/.virtual-worker/searxng/run.sh.
# SEARXNG_HOME is a **machine-level** location (default ~/.virtual-worker/searxng), NOT tied
# to VW_HOME — so a dev.sh run (VW_HOME=./data) still finds the shared install.
SEARXNG_HOME = Path(os.environ.get("VW_SEARXNG_HOME") or (Path.home() / ".virtual-worker" / "searxng"))
SEARXNG_PORT = int(os.environ.get("VW_SEARXNG_PORT", "8899"))   # must match settings.yml server.port
MANAGED_SEARXNG = os.environ.get("VW_MANAGED_SEARXNG", "1") not in ("0", "false", "False")
# An explicit VW_SEARXNG_URL points at an external instance → don't manage one.
if os.environ.get("VW_SEARXNG_URL"):
    MANAGED_SEARXNG = False

# Interactive browser control (browser_open / _click / _type / …). A separate,
# **visible**, **persistent-profile** Chromium from the headless browser_fetch, so
# the user can log into sites once and have that login stick across runs. The
# profile dir is machine-level (default ~/.virtual-worker/browser-profile), NOT tied to
# VW_HOME — so a dev.sh run (VW_HOME=./data) still reuses saved logins.
BROWSER_PROFILE = Path(os.environ.get("VW_BROWSER_PROFILE") or (Path.home() / ".virtual-worker" / "browser-profile"))
# Visible by default (headless=0) — the whole point is a window you can log into.
CONTROL_BROWSER_HEADLESS = os.environ.get("VW_CONTROL_BROWSER_HEADLESS", "0") not in ("0", "false", "False")
# Attach the control browser to YOUR already-running Chrome instead of launching
# a separate one: a CDP endpoint like "http://127.0.0.1:9222". Chrome must be
# started with --remote-debugging-port=9222 --user-data-dir=<dedicated dir>
# (Chrome 136+ ignores the debugging flag on its default profile). Virtual Worker never
# closes an attached browser — shutdown only disconnects, so tabs/logins live on.
BROWSER_CDP_URL = (os.environ.get("VW_BROWSER_CDP_URL") or "").strip()

# Headless browser_fetch (throwaway, read-only context). Default headless.
BROWSER_FETCH_HEADLESS = (os.environ.get("VW_BROWSER_HEADLESS") or "1") != "0"


# --- Runtime browser settings (Settings → Browser) ------------------------- #
# Browser configuration is a runtime setting (settings.json ``browser`` object) as
# well as an env var. Precedence per key: an explicitly-set env var wins (a
# *presence* check on os.environ, so "set to the default value" still counts as
# an override), else the persisted setting, else the built-in default (the
# module constants above, which already fold in env-at-import / auto-detection).
# Resolvers read the settings store at USE time, so a change from the Settings
# UI takes effect on the next launch / call without a backend restart. Import is
# lazy to avoid a config↔storage.settings import cycle.

# Which key of the ``browser`` settings object each env var overrides (a present
# env var pins that field; focusPolicy has no env — it's settings-only).
BROWSER_ENV_VARS = {
    "controlMode": "VW_CONTROL_BROWSER_HEADLESS",
    "channel": "VW_BROWSER_CHANNEL",
    "cdpUrl": "VW_BROWSER_CDP_URL",
    "fetchHeadless": "VW_BROWSER_HEADLESS",
    "profileDir": "VW_BROWSER_PROFILE",
}

_UNSET = object()


def _browser_setting(key: str, default=None):
    """Read one key from the persisted ``browser`` settings object (or default)."""
    try:
        from storage import settings
        b = settings.get("browser") or {}
        if isinstance(b, dict) and key in b:
            return b.get(key)
    except Exception:  # noqa: BLE001 — a missing/corrupt store must not break a launch
        pass
    return default


def control_browser_headless() -> bool:
    """Whether the interactive control browser runs headless (controlMode)."""
    if "VW_CONTROL_BROWSER_HEADLESS" in os.environ:
        return os.environ["VW_CONTROL_BROWSER_HEADLESS"] not in ("0", "false", "False")
    mode = _browser_setting("controlMode")
    if mode in ("visible", "headless"):
        return mode == "headless"
    return CONTROL_BROWSER_HEADLESS


def browser_focus_policy() -> str:
    """"allow" (default) or "never" — whether the control browser may steal OS
    focus (bring_to_front). Settings-only; there is no env override."""
    return "never" if _browser_setting("focusPolicy") == "never" else "allow"


def browser_channel() -> str:
    """Which browser Playwright drives (channel). "" forces bundled Chromium;
    a null/absent setting means auto (the BROWSER_CHANNEL default)."""
    if "VW_BROWSER_CHANNEL" in os.environ:
        return os.environ["VW_BROWSER_CHANNEL"]
    ch = _browser_setting("channel")
    if isinstance(ch, str):  # "", "chrome", "msedge" — an explicit choice
        return ch
    return BROWSER_CHANNEL


def browser_cdp_url() -> str:
    """The CDP endpoint to attach the control browser to ("" = launch our own)."""
    if "VW_BROWSER_CDP_URL" in os.environ:
        return (os.environ["VW_BROWSER_CDP_URL"] or "").strip()
    cdp = _browser_setting("cdpUrl")
    if isinstance(cdp, str):
        return cdp.strip()
    return BROWSER_CDP_URL


def browser_fetch_headless() -> bool:
    """Whether the headless browser_fetch browser runs headless (fetchHeadless)."""
    if "VW_BROWSER_HEADLESS" in os.environ:
        return os.environ["VW_BROWSER_HEADLESS"] != "0"
    hl = _browser_setting("fetchHeadless")
    if isinstance(hl, bool):
        return hl
    return BROWSER_FETCH_HEADLESS


def browser_profile() -> Path:
    """The persistent profile dir for the control browser (profileDir)."""
    if "VW_BROWSER_PROFILE" in os.environ:
        return Path(os.environ["VW_BROWSER_PROFILE"])
    p = _browser_setting("profileDir")
    if isinstance(p, str) and p.strip():
        return Path(p)
    return BROWSER_PROFILE


# --- Gateway (multi-protocol) settings ------------------------------------- #
# Map vendor model ids -> the local model the upstream serves, e.g.
#   VW_MODEL_MAP='{"claude-3-5-sonnet-20241022":"local-qwen"}'
try:
    MODEL_MAP: dict[str, str] = json.loads(os.environ.get("VW_MODEL_MAP") or "{}")
    if not isinstance(MODEL_MAP, dict):
        raise ValueError("VW_MODEL_MAP must be a JSON object")
except (ValueError, json.JSONDecodeError) as exc:
    logger.warning("ignoring invalid VW_MODEL_MAP: %s", exc)
    MODEL_MAP = {}

# One local model for every unmapped request id (optional).
DEFAULT_MODEL = os.environ.get("VW_DEFAULT_MODEL")

# Bounded retries for transient provider failures in the agent loop.
LLM_RETRIES = int(os.environ.get("VW_LLM_RETRIES", "5"))
LLM_BACKOFF_BASE = float(os.environ.get("VW_LLM_BACKOFF_BASE", "0.5"))

# If set, every gateway request must present this key; otherwise any key passes.
GATEWAY_KEY = os.environ.get("VW_GATEWAY_KEY")

# --- Security ---------------------------------------------------------------- #
# Browser origins allowed to call the API (CORS). The API fronts real tools
# (shell, fs, browser), so never "*": any local origin (any port) is allowed,
# plus exact extras from VW_CORS_ORIGINS="https://app.example.com,…".
CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
CORS_ORIGINS: list[str] = [o.strip() for o in (os.environ.get("VW_CORS_ORIGINS") or "").split(",") if o.strip()]
ALLOWED_ORIGINS = CORS_ORIGINS  # back-compat alias
# Host-header allow-list (DNS-rebinding guard). Local names are always allowed;
# add your public host for webhooks / a reverse proxy: VW_ALLOWED_HOSTS="bot.example.com".
ALLOWED_HOSTS: list[str] = ["localhost", "127.0.0.1", "[::1]", "testserver"] + [
    h.strip() for h in (os.environ.get("VW_ALLOWED_HOSTS") or "").split(",") if h.strip()]


# Retention for the always-on app.db — capture logs every LLM call (full
# prompts + tool results) and the activity feed is append-only, so both grow
# without bound on a long-running intern. A periodic maintenance loop prunes to
# these ceilings. Defaults are generous (keep useful recent history); set an env
# var to 0 to disable that bound. Age in days; MAX_ROWS is a hard row ceiling.
DEBUG_RETENTION_DAYS = int(os.environ.get("VW_DEBUG_RETENTION_DAYS", "14"))
DEBUG_MAX_ROWS = int(os.environ.get("VW_DEBUG_MAX_ROWS", "5000"))
ACTIVITY_RETENTION_DAYS = int(os.environ.get("VW_ACTIVITY_RETENTION_DAYS", "90"))
ACTIVITY_MAX_ROWS = int(os.environ.get("VW_ACTIVITY_MAX_ROWS", "50000"))

# Retention for the rest of the always-on state (the maintenance loop).
DELIVERY_RETENTION_DAYS = int(os.environ.get("VW_DELIVERY_RETENTION_DAYS", "90"))
CHAT_EVENT_RETENTION_DAYS = int(os.environ.get("VW_CHAT_EVENT_RETENTION_DAYS", "7"))      # channel dedup ids
CHANNEL_MEDIA_RETENTION_DAYS = int(os.environ.get("VW_CHANNEL_MEDIA_RETENTION_DAYS", "14"))  # received files
MAINTENANCE_INTERVAL_S = int(os.environ.get("VW_MAINTENANCE_INTERVAL_S", "3600"))
# Aliases used by the channel/maintenance code.
TRACE_RETENTION_DAYS = DEBUG_RETENTION_DAYS
TRACE_MAX_RUNS = DEBUG_MAX_ROWS


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    ensure_dir(APP_HOME)
    return APP_HOME / "app.db"
