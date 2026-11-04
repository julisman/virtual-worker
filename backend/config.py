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


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    ensure_dir(APP_HOME)
    return APP_HOME / "app.db"
