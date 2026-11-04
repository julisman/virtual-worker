#!/usr/bin/env bash
#
# One-time setup for Virtual Worker. Installs everything needed to run:
#   - backend venv (uv) + Python deps
#   - frontend deps (npm)
#   - llama.cpp  (GGUF engine, via Homebrew)
#   - mlx-lm     (MLX engine, Apple Silicon only)
#
# Idempotent — safe to re-run; skips what's already installed. dev.sh calls this
# automatically on first run, so a fresh clone just needs:  ./dev.sh
#
# Skip the MLX engine with:  VW_SKIP_MLX=1 ./setup.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[36m[setup]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[setup]\033[0m %s\n' "$1"; }
err()  { printf '\033[31m[setup] error:\033[0m %s\n' "$1" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

# --- 1. backend venv + deps (uv) -------------------------------------------- #
if [[ ! -x "$ROOT/backend/.venv/bin/uvicorn" ]]; then
  need uv || err "uv not installed — get it with:  curl -LsSf https://astral.sh/uv/install.sh | sh   (then re-run)"
  say "creating backend venv (Python 3.13) + installing deps…"
  uv venv --python 3.13 "$ROOT/backend/.venv"
  uv pip install -p "$ROOT/backend/.venv" -r "$ROOT/backend/requirements.txt"
else
  say "backend venv ✓"
fi

# --- 2. frontend deps (npm) ------------------------------------------------- #
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  need npm || err "npm not installed — install Node.js: https://nodejs.org  (then re-run)"
  say "installing frontend deps…"
  ( cd "$ROOT/frontend" && npm install )
else
  say "frontend deps ✓"
fi

# --- 3. llama.cpp (GGUF engine) --------------------------------------------- #
if need llama-server; then
  say "llama.cpp ✓"
elif need brew; then
  say "installing llama.cpp (Homebrew)…"
  brew install llama.cpp || err "brew install llama.cpp failed"
else
  warn "llama-server not found and Homebrew missing — install llama.cpp manually:
        https://github.com/ggml-org/llama.cpp  (or install Homebrew: https://brew.sh)"
fi

# --- 4. MLX engines (Apple Silicon only): mlx-lm (text) + mlx-vlm (vision) --- #
if [[ "$(uname)" == "Darwin" && "$(uname -m)" == "arm64" && "${VW_SKIP_MLX:-0}" != "1" ]]; then
  if need uv; then
    # pkg:module pairs
    for pair in "mlx-lm:mlx_lm" "mlx-vlm:mlx_vlm"; do
      pkg="${pair%%:*}"; mod="${pair##*:}"
      if "$ROOT/backend/.venv/bin/python" -c "import $mod" 2>/dev/null; then
        say "$pkg ✓"
      else
        say "installing ${pkg}…"
        uv pip install -p "$ROOT/backend/.venv" "$pkg" \
          && say "$pkg ✓" \
          || warn "$pkg install failed — those models will be disabled"
      fi
    done
  else
    warn "uv missing — MLX disabled"
  fi
fi

# --- 5. WhatsApp bridge (Node sidecar for the linked-device channel) ------- #
BRIDGE="$ROOT/backend/channels/whatsapp/bridge"
if [[ -d "$BRIDGE/node_modules/@whiskeysockets/baileys" ]]; then
  say "whatsapp bridge ✓"
elif need npm; then
  say "installing the WhatsApp bridge deps…"
  ( cd "$BRIDGE" && npm install --no-audit --no-fund ) && say "whatsapp bridge ✓" \
    || warn "whatsapp bridge install failed — the WhatsApp (linked device) channel will be unavailable"
else
  warn "npm missing — skipping the WhatsApp bridge"
fi

say "setup complete — run ./dev.sh to start Virtual Worker."
