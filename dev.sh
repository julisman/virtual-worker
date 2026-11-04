#!/usr/bin/env bash
#
# Single-command dev launcher: starts the llama.cpp model server, the FastAPI
# backend (pointed at it), and the Vite frontend — and tears all three down
# together on Ctrl+C.
#
# Usage:
#   ./dev.sh                        # start everything with defaults
#   LLAMA_MODEL=/path/to.gguf ./dev.sh
#   VW_UPSTREAM_URL=http://127.0.0.1:11434 ./dev.sh   # use your own server
#                                                            # (e.g. ollama); skips llama
#
# Override any of: LLAMA_MODEL, LLAMA_PORT, LLAMA_CTX, LLAMA_NGL,
#                  BACKEND_PORT, FRONTEND_PORT.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Dev ports are offset from the service ports (./vw start → :8888 / :8080) so a
# hot-reload dev instance can run next to the always-on one.
export LLAMA_PORT="${LLAMA_PORT:-8081}"
export BACKEND_PORT="${BACKEND_PORT:-8889}"
export FRONTEND_PORT="${FRONTEND_PORT:-5173}"
export VITE_BACKEND_PORT="$BACKEND_PORT"
export VITE_FRONTEND_PORT="$FRONTEND_PORT"
export LLAMA_CTX="${LLAMA_CTX:-32768}"   # coding agents send ~25k-token prompts
export LLAMA_NGL="${LLAMA_NGL:-99}"   # offload all layers to Metal GPU
export LLAMA_MODEL="${LLAMA_MODEL:-}"   # optional: a GGUF to load on startup

# Managed mode (default): the BACKEND starts/stops/switches llama-server itself,
# so the active model is changeable from the UI. Set VW_MANAGED_LLAMA=0 to
# have dev.sh launch a fixed llama-server the old way.
export VW_MANAGED_LLAMA="${VW_MANAGED_LLAMA:-1}"

# Keep all local state (app.db, chat sessions, tokens, llama logs) in-project
# under ./data/ (gitignored) so the app is self-contained & portable.
export VW_HOME="${VW_HOME:-$ROOT/data}"

# If the caller already provides an OpenAI-compatible server, point the backend
# at it (this also disables managed mode) and don't launch our own llama-server.
USE_OWN_UPSTREAM=0
if [[ -n "${VW_UPSTREAM_URL:-}" ]]; then
  USE_OWN_UPSTREAM=1
  export VW_MANAGED_LLAMA=0
elif [[ "$VW_MANAGED_LLAMA" != "1" ]]; then
  # legacy: dev.sh runs llama-server, backend just proxies to it
  export VW_UPSTREAM_URL="http://127.0.0.1:${LLAMA_PORT}"
fi
# In managed mode we intentionally DON'T export VW_UPSTREAM_URL — the
# backend derives it from LLAMA_PORT and manages the process.

# --- preflight: bootstrap on first run (delegates to setup.sh) -------------- #
err() { printf '\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

needs_setup=0
[[ -x "$ROOT/backend/.venv/bin/uvicorn" ]] || needs_setup=1
[[ -d "$ROOT/frontend/node_modules" ]] || needs_setup=1
[[ -d "$ROOT/backend/channels/whatsapp/bridge/node_modules" ]] || needs_setup=1
[[ "$USE_OWN_UPSTREAM" -eq 0 ]] && ! command -v llama-server >/dev/null && needs_setup=1
# Apple Silicon: also (re)run setup if an MLX engine isn't installed yet
# (mlx-lm for text, mlx-vlm for vision/audio models).
if [[ "$(uname)" == "Darwin" && "$(uname -m)" == "arm64" && "${VW_SKIP_MLX:-0}" != "1"
      && -x "$ROOT/backend/.venv/bin/python" ]]; then
  "$ROOT/backend/.venv/bin/python" -c "import mlx_lm, mlx_vlm" 2>/dev/null || needs_setup=1
fi

if [[ "$needs_setup" -eq 1 ]]; then
  printf '\033[36m[dev]\033[0m first-run setup — installing prerequisites…\n'
  [[ -x "$ROOT/setup.sh" ]] || err "setup.sh missing/not executable — run: chmod +x setup.sh"
  "$ROOT/setup.sh" || err "setup failed — fix the issue above, or run ./setup.sh manually"
fi

# A model is only required up front when dev.sh launches llama itself (legacy);
# in managed mode you download/activate one in the Hub UI.
if [[ "$USE_OWN_UPSTREAM" -eq 0 && "$VW_MANAGED_LLAMA" != "1" && ! -f "$LLAMA_MODEL" ]]; then
  err "model not found: $LLAMA_MODEL
  Pass another with: LLAMA_MODEL=/path/to/model.gguf ./dev.sh"
elif [[ "$USE_OWN_UPSTREAM" -eq 0 && ( -z "$LLAMA_MODEL" || ! -f "$LLAMA_MODEL" ) ]]; then
  printf '\033[33m[dev]\033[0m no default model on disk yet — open Hub to download one, then Activate it.\n'
fi

# --- teardown: all children share this script's process group --------------- #
cleanup() {
  trap - INT TERM EXIT
  printf '\n\033[33mshutting down…\033[0m\n'
  # The backend-managed llama-server runs detached (own session) so it survives
  # uvicorn --reload; kill it explicitly here via its pid file.
  pidfile="${VW_HOME:-$HOME/.virtual-worker}/llama-server.json"
  if [[ -f "$pidfile" ]]; then
    lpid=$(sed -n 's/.*"pid":[[:space:]]*\([0-9]*\).*/\1/p' "$pidfile" 2>/dev/null)
    [[ -n "$lpid" ]] && kill "$lpid" 2>/dev/null || true
  fi
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Prefix each service's output with a colored tag (fflush => line-buffered).
prefix() { awk -v p="$1" '{ print p $0; fflush() }'; }
C_LLAMA=$'\033[35m[llama]\033[0m '
C_API=$'\033[36m[api]  \033[0m '
C_WEB=$'\033[32m[web]  \033[0m '

# --- llama.cpp -------------------------------------------------------------- #
if [[ "$USE_OWN_UPSTREAM" -eq 1 ]]; then
  printf '\033[36m[api]  \033[0m using existing upstream: %s\n' "$VW_UPSTREAM_URL"
elif [[ "$VW_MANAGED_LLAMA" == "1" ]]; then
  printf '\033[35m[llama]\033[0m backend-managed — pick and switch the active model in the UI\n'
else
  printf '\033[35m[llama]\033[0m loading %s\n' "$(basename "$LLAMA_MODEL")"
  ( llama-server \
      --model "$LLAMA_MODEL" \
      --alias "$(basename "$LLAMA_MODEL" .gguf)" \
      --host 127.0.0.1 --port "$LLAMA_PORT" \
      --ctx-size "$LLAMA_CTX" --n-gpu-layers "$LLAMA_NGL" \
      --jinja 2>&1 | prefix "$C_LLAMA" ) &
fi

# --- backend (FastAPI) ------------------------------------------------------ #
( cd "$ROOT/backend" && \
  .venv/bin/uvicorn app:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload 2>&1 | prefix "$C_API" ) &

# --- frontend (Vite) -------------------------------------------------------- #
( cd "$ROOT/frontend" && npm run dev 2>&1 | prefix "$C_WEB" ) &

printf '\n\033[1mVES dev\033[0m\n'
printf '  web      → http://localhost:%s\n' "$FRONTEND_PORT"
printf '  api      → http://127.0.0.1:%s  (docs: /docs)\n' "$BACKEND_PORT"
[[ "$USE_OWN_UPSTREAM" -eq 0 ]] && printf '  model    → http://127.0.0.1:%s/v1  (backend-managed: llama.cpp / MLX)\n' "$LLAMA_PORT"
printf '  Ctrl+C to stop all.\n\n'

wait
