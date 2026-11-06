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
#   KEEP_AWAKE=1 ./dev.sh           # (macOS) keep the Mac + network awake while
#                                   # locked, so always-on chat channels
#                                   # don't drop their WebSockets on idle sleep.
#
# Override any of: LLAMA_MODEL, LLAMA_PORT, LLAMA_CTX, LLAMA_NGL,
#                  BACKEND_PORT, FRONTEND_PORT.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- keep-awake: re-exec under `caffeinate` so the machine (and its network)
# never idle-sleeps while this runs. macOS only; -d display -i idle -m disk
# -s system -u user-active. The guard var stops an infinite re-exec loop. ------ #
if [[ "${KEEP_AWAKE:-0}" == "1" && -z "${_VW_CAFFEINATED:-}" ]]; then
  if [[ "$(uname)" == "Darwin" ]] && command -v caffeinate >/dev/null; then
    printf '\033[36m[dev]\033[0m KEEP_AWAKE=1 — running under caffeinate (no idle/display/disk/system sleep)\n'
    export _VW_CAFFEINATED=1
    exec caffeinate -dimsu "${BASH_SOURCE[0]}" "$@"
  else
    printf '\033[33m[dev]\033[0m KEEP_AWAKE=1 ignored — caffeinate is macOS-only\n' >&2
  fi
fi

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
# under ./data/ (gitignored) so the app is self-contained & portable — UNLESS
# the repo lives inside a macOS TCC-protected folder (~/Documents, ~/Desktop,
# ~/Downloads). A long-running backend there loses access to its own SQLite db
# the moment the screen is locked ("sqlite3.OperationalError: unable to open
# database file") — those folders get special macOS treatment (TCC, cloud sync,
# purgeable-space management) that a background process can't rely on while the
# session is locked. Fatal for an always-on agent host. Redirect state to a
# safe machine-level dir (and migrate any existing ./data once) so it survives
# lock. An explicitly-set VW_HOME always wins and is never second-guessed.
if [[ -n "${VW_HOME:-}" ]]; then
  export VW_HOME
else
  _in_project="$ROOT/data"
  case "$ROOT/" in
    "$HOME/Documents/"* | "$HOME/Desktop/"* | "$HOME/Downloads/"*)
      _safe_home="$HOME/.virtual-worker-data/$(basename "$ROOT")"
      if [[ ! -f "$_safe_home/app.db" && -f "$_in_project/app.db" ]]; then
        printf '\033[33m[dev]\033[0m repo is in a macOS-protected folder (%s)\n' "${ROOT/#$HOME/~}"
        printf '\033[33m[dev]\033[0m the db there becomes unreadable when the screen locks — migrating state to %s\n' "${_safe_home/#$HOME/~}"
        mkdir -p "$_safe_home"
        rsync -a "$_in_project"/ "$_safe_home"/ 2>/dev/null || cp -R "$_in_project"/. "$_safe_home"/
      else
        mkdir -p "$_safe_home"
      fi
      printf '\033[36m[dev]\033[0m data dir → %s (survives screen lock)\n' "${_safe_home/#$HOME/~}"
      export VW_HOME="$_safe_home"
      ;;
    *)
      export VW_HOME="$_in_project"
      ;;
  esac
fi

# Chrome cannot open the same persistent profile in two instances. Keep the
# dev control browser separate from stable by default; an explicit profile wins.
export VW_BROWSER_PROFILE="${VW_BROWSER_PROFILE:-$HOME/.virtual-worker/browser-profile-dev}"

# SearXNG is machine-level and stable owns the only managed instance. Reuse it
# when available, otherwise disable web search instead of fighting for :8899.
WEB_SEARCH_DISABLED=0
if [[ -z "${VW_SEARXNG_URL:-}" ]]; then
  if command -v curl >/dev/null && curl -sf --max-time 1 http://127.0.0.1:8899 >/dev/null 2>&1; then
    export VW_SEARXNG_URL="http://127.0.0.1:8899"
  else
    export VW_MANAGED_SEARXNG=0
    WEB_SEARCH_DISABLED=1
  fi
fi

# If the caller already provides an OpenAI-compatible server, point the backend
# at it (this also disables managed mode) and don't launch our own llama-server.
USE_OWN_UPSTREAM=0
if [[ -n "${VW_UPSTREAM_URL:-}" ]]; then
  USE_OWN_UPSTREAM=1
  export VW_MANAGED_LLAMA=0
elif [[ "${VW_SHARE_UPSTREAM:-0}" == "1" ]]; then
  export VW_UPSTREAM_URL="http://127.0.0.1:8080"
  export VW_MANAGED_LLAMA=0
  USE_OWN_UPSTREAM=1
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
# Trigger when requirements.txt changed since the last install (e.g. after a
# `git pull` that added a dependency), so we don't boot the API against a
# stale venv ("ModuleNotFoundError").
[[ "$ROOT/backend/requirements.txt" -nt "$ROOT/backend/.venv/.requirements-installed" ]] && needs_setup=1
# Only (re)run setup for the frontend when npm exists — otherwise setup can't
# build node_modules anyway, and we fall back to backend-only mode below.
# Trigger when node_modules is missing OR stale (package.json / lock changed
# since the last install — e.g. after a `git pull` that added a dependency),
# so we don't boot Vite against out-of-date deps ("Failed to resolve import …").
if command -v npm >/dev/null; then
  fe_marker="$ROOT/frontend/node_modules/.package-lock.json"
  if [[ ! -d "$ROOT/frontend/node_modules" || ! -f "$fe_marker"
        || "$ROOT/frontend/package.json" -nt "$fe_marker"
        || ( -f "$ROOT/frontend/package-lock.json" && "$ROOT/frontend/package-lock.json" -nt "$fe_marker" ) ]]; then
    needs_setup=1
  fi
fi
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
  trap - INT EXIT
  # Ignore our own TERM: `kill 0` below signals the whole process group, which
  # includes THIS script. Without this the launcher would kill itself mid-cleanup
  # and nothing would supervise the stragglers — the reason a wedged backend used
  # to survive Ctrl+C and need a manual `kill -9`.
  trap '' TERM
  printf '\n\033[33mshutting down…\033[0m\n'
  # The backend-managed llama-server runs detached (own session) so it survives
  # uvicorn --reload; kill it explicitly here via its pid file.
  pidfile="${VW_HOME:-$HOME/.virtual-worker}/llama-server.json"
  if [[ -f "$pidfile" ]]; then
    lpid=$(sed -n 's/.*"pid":[[:space:]]*\([0-9]*\).*/\1/p' "$pidfile" 2>/dev/null)
    [[ -n "$lpid" ]] && kill "$lpid" 2>/dev/null || true
  fi
  kill 0 2>/dev/null || true            # SIGTERM the whole process group
  # Give the group up to ~10s to exit on its own (uvicorn graceful shutdown +
  # the app's bounded lifespan teardown), then force-kill any survivor. Group-
  # wide by design: under --reload, uvicorn is a reloader parent + worker child;
  # a targeted kill of the parent orphans the worker still holding the API port.
  # Poll our own background jobs (the `( … ) &` services) via the `jobs` builtin
  # — no external process, so nothing contaminates the sample. Exit as soon as
  # they're all gone; force-kill the whole group only if some survive the grace.
  local i
  for i in $(seq 1 40); do              # up to ~10s grace
    [[ -z "$(jobs -pr)" ]] && exit 0
    sleep 0.25
  done
  printf '\033[31m[dev]\033[0m some processes did not exit in time — force-killing.\n'
  kill -9 0 2>/dev/null || true
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
  printf '\033[35m[llama]\033[0m backend-managed — it will load %s and you can switch in the UI\n' "$(basename "$LLAMA_MODEL")"
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
  .venv/bin/uvicorn app:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload \
    --timeout-graceful-shutdown "${VW_SHUTDOWN_TIMEOUT:-8}" 2>&1 | prefix "$C_API" ) &

# --- frontend (Vite) -------------------------------------------------------- #
if command -v npm >/dev/null && [[ -d "$ROOT/frontend/node_modules" ]]; then
  ( cd "$ROOT/frontend" && npm run dev 2>&1 | prefix "$C_WEB" ) &
  FRONTEND_UP=1
else
  FRONTEND_UP=0
  printf '\033[33m[web]  \033[0m npm not installed — web UI disabled. Install Node.js (https://nodejs.org), then ./setup.sh to enable it.\n'
fi

printf '\n\033[1mVirtual Worker dev\033[0m\n'
printf '  stable-safe dev ports (stable stays on :8888/:8080)\n'
[[ "$FRONTEND_UP" -eq 1 ]] && printf '  web      → http://localhost:%s\n' "$FRONTEND_PORT"
printf '  api      → http://127.0.0.1:%s  (docs: /docs)\n' "$BACKEND_PORT"
[[ "$USE_OWN_UPSTREAM" -eq 0 ]] && printf '  model    → http://127.0.0.1:%s/v1  (backend-managed: llama.cpp / MLX)\n' "$LLAMA_PORT"
[[ "$WEB_SEARCH_DISABLED" -eq 1 ]] && printf '  web search disabled in dev (stable SearXNG is not running)\n'
printf '  Ctrl+C to stop all.\n\n'

wait
