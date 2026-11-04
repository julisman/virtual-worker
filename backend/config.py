"""Runtime configuration and paths for Virtual Worker."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# App data dir (db, settings, caches). Override with VW_HOME.
APP_HOME = Path(os.environ.get("VW_HOME", str(Path.home() / ".virtual-worker")))

# --- Managed llama.cpp runtime --------------------------------------------- #
# When managed (default), the backend starts/stops/switches a single
# `llama-server` itself, so the active model can be changed from the UI.
LLAMA_BIN = os.environ.get("LLAMA_BIN", "llama-server")
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

# If set, every gateway request must present this key; otherwise any key passes.
GATEWAY_KEY = os.environ.get("VW_GATEWAY_KEY")


# --- Security ---------------------------------------------------------------- #
# Browser origins allowed to call the API (CORS). Default: only local origins
# (any port) so a random web page can't drive the API through the user's browser.
# Add more with VW_CORS_ORIGINS="https://app.example.com,https://other" (exact).
CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
CORS_ORIGINS: list[str] = [o.strip() for o in (os.environ.get("VW_CORS_ORIGINS") or "").split(",") if o.strip()]
# Host-header allow-list (DNS-rebinding guard). Local names are always allowed;
# add your public host for webhooks / a reverse proxy: VW_ALLOWED_HOSTS="bot.example.com".
ALLOWED_HOSTS: list[str] = ["localhost", "127.0.0.1", "[::1]", "testserver"] + [
    h.strip() for h in (os.environ.get("VW_ALLOWED_HOSTS") or "").split(",") if h.strip()]

# --- Retention (the maintenance loop keeps app.db and media bounded) --------- #
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default

TRACE_RETENTION_DAYS = _int_env("VW_TRACE_RETENTION_DAYS", 30)        # Debug traces (model calls)
TRACE_MAX_RUNS = _int_env("VW_TRACE_MAX_RUNS", 20000)                 # hard cap on trace rows
ACTIVITY_RETENTION_DAYS = _int_env("VW_ACTIVITY_RETENTION_DAYS", 90)  # agent activity feed
DELIVERY_RETENTION_DAYS = _int_env("VW_DELIVERY_RETENTION_DAYS", 90)  # outbound audit rows
CHAT_EVENT_RETENTION_DAYS = _int_env("VW_CHAT_EVENT_RETENTION_DAYS", 7)   # channel dedup ids
CHANNEL_MEDIA_RETENTION_DAYS = _int_env("VW_CHANNEL_MEDIA_RETENTION_DAYS", 14)  # received files
MAINTENANCE_INTERVAL_S = _int_env("VW_MAINTENANCE_INTERVAL_S", 3600)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    ensure_dir(APP_HOME)
    return APP_HOME / "app.db"
