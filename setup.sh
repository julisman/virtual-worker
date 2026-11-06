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
# uv is required (it builds the backend venv). Auto-install it with the official
# no-sudo installer if missing, then make it visible to the rest of this run.
# Set VW_NO_AUTO_UV=1 to opt out and install uv yourself.
req_marker="$ROOT/backend/.venv/.requirements-installed"
if [[ ! -x "$ROOT/backend/.venv/bin/uvicorn" ]]; then
  if ! need uv; then
    [[ "${VW_NO_AUTO_UV:-0}" == "1" ]] && err "uv not installed — get it with:  curl -LsSf https://astral.sh/uv/install.sh | sh   (then re-run)"
    say "uv not found — installing it (astral.sh, no sudo)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh || err "uv install failed — install manually: https://docs.astral.sh/uv/getting-started/installation/"
    export PATH="$HOME/.local/bin:$PATH"; hash -r
    need uv || err "uv installed but not on PATH — open a new terminal and re-run ./setup.sh"
  fi
  say "creating backend venv (Python 3.13) + installing deps…"
  uv venv --python 3.13 "$ROOT/backend/.venv"
  uv pip install -p "$ROOT/backend/.venv" -r "$ROOT/backend/requirements.txt"
  touch "$req_marker"
elif [[ "$ROOT/backend/requirements.txt" -nt "$req_marker" ]]; then
  say "requirements.txt changed — syncing backend venv…"
  uv pip install -p "$ROOT/backend/.venv" -r "$ROOT/backend/requirements.txt"
  touch "$req_marker"
else
  say "backend venv ✓"
fi

# --- 1b. headless Chromium for the browser_search / browser_fetch tools ------ #
# Playwright ships the driver via pip (above); the browser binary is a separate
# one-time download. Idempotent — skips if already present. Best-effort: a
# failure only disables the web tools, so warn and continue.
if [[ -x "$ROOT/backend/.venv/bin/python" ]] && [[ "${VW_SKIP_BROWSER:-0}" != "1" ]]; then
  say "ensuring headless Chromium for the browser tools (idempotent)…"
  "$ROOT/backend/.venv/bin/python" -m playwright install chromium \
    || warn "Chromium install failed — browser_search/browser_fetch will be disabled until you run:
        cd backend && .venv/bin/python -m playwright install chromium"
fi

# --- 2. frontend deps (npm) ------------------------------------------------- #
# Reinstall when node_modules is missing OR stale — i.e. package.json /
# package-lock.json changed since the last install (npm records the install in
# node_modules/.package-lock.json). Without this, a `git pull` that adds a
# dependency (e.g. react-markdown) leaves node_modules out of date and Vite fails
# with "Failed to resolve import …". npm install is a no-op when already current.
_frontend_stale() {
  [[ -d "$ROOT/frontend/node_modules" ]] || return 0
  local marker="$ROOT/frontend/node_modules/.package-lock.json"
  [[ -f "$marker" ]] || return 0
  [[ "$ROOT/frontend/package.json" -nt "$marker" ]] && return 0
  [[ -f "$ROOT/frontend/package-lock.json" && "$ROOT/frontend/package-lock.json" -nt "$marker" ]] && return 0
  return 1
}

# Missing npm only disables the web UI — warn and skip (like llama.cpp below) so
# the backend + engines still install. Install Node.js to enable the frontend.
if _frontend_stale; then
  if need npm; then
    say "installing frontend deps…"
    ( cd "$ROOT/frontend" && npm install )
  else
    warn "npm not found — skipping frontend (web UI disabled).
        Install Node.js (https://nodejs.org) and re-run ./setup.sh to enable it."
  fi
else
  say "frontend deps ✓"
fi

# --- 3. llama.cpp (GGUF engine) --------------------------------------------- #
# Optional — MLX models don't need it. Prefer Homebrew; if brew is absent on
# macOS, fall back to the official prebuilt binaries in ~/.local (no sudo, like
# Node/uv). Installing Homebrew itself is too invasive to do here, so non-macOS
# hosts without brew just get a warning.
if need llama-server; then
  say "llama.cpp ✓"
elif need brew; then
  say "installing llama.cpp (Homebrew)…"
  brew install llama.cpp || err "brew install llama.cpp failed"
elif [[ "$(uname)" == "Darwin" ]]; then
  [[ "$(uname -m)" == "arm64" ]] && asset="macos-arm64" || asset="macos-x64"
  say "llama.cpp not found and no Homebrew — fetching prebuilt $asset binaries…"
  tag="$(curl -fsSL https://api.github.com/repos/ggml-org/llama.cpp/releases/latest 2>/dev/null \
         | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  tmp="$(mktemp -d)"
  if [[ -n "$tag" ]] \
     && curl -fsSL "https://github.com/ggml-org/llama.cpp/releases/download/${tag}/llama-${tag}-bin-${asset}.tar.gz" -o "$tmp/llama.tar.gz" \
     && tar -xzf "$tmp/llama.tar.gz" -C "$tmp"; then
    dest="$HOME/.local/lib/llama.cpp/$tag"
    rm -rf "$HOME/.local/lib/llama.cpp"; mkdir -p "$dest" "$HOME/.local/bin"
    cp -R "$tmp/llama-${tag}"/* "$dest"/
    # dylibs live beside the binaries; symlinking into ~/.local/bin still
    # resolves them (rpath is loader-relative). dylibs are lib*.dylib, so the
    # llama-* glob picks up only the executables.
    for b in "$dest"/llama-*; do [[ -f "$b" && -x "$b" ]] && ln -sf "$b" "$HOME/.local/bin/$(basename "$b")"; done
    hash -r
    need llama-server && say "llama.cpp ✓ ($tag)" \
      || warn "llama.cpp fetched but llama-server not on PATH — add ~/.local/bin to PATH"
  else
    warn "llama.cpp prebuilt download failed — install manually:
        https://github.com/ggml-org/llama.cpp/releases  (or install Homebrew: https://brew.sh)"
  fi
  rm -rf "$tmp"
else
  warn "llama-server not found and Homebrew missing — install llama.cpp manually:
        https://github.com/ggml-org/llama.cpp  (or install Homebrew: https://brew.sh)"
fi

# --- 4. MLX engines (Apple Silicon only): mlx-lm (text) + mlx-vlm (vision) --- #
if [[ "$(uname)" == "Darwin" && "$(uname -m)" == "arm64" && "${VW_SKIP_MLX:-0}" != "1" ]]; then
  if need uv; then
    # pkg:min pairs. Check the *version*, not just importability: mlx-vlm
    # before 0.6.5 loads qwen3_5 models happily but decodes them to garbage
    # tokens, and a presence-only check would keep a broken copy forever.
    for pair in "mlx-lm:0.31.3" "mlx-vlm:0.6.5"; do
      pkg="${pair%%:*}"; min="${pair##*:}"
      if "$ROOT/backend/.venv/bin/python" - "$pkg" "$min" <<'PY' 2>/dev/null
import sys
from importlib.metadata import version
pkg, min_v = sys.argv[1], sys.argv[2]
parse = lambda s: tuple(int(p) for p in s.split(".")[:3])
sys.exit(0 if parse(version(pkg)) >= parse(min_v) else 1)
PY
      then
        say "$pkg ✓"
      else
        say "installing ${pkg}>=${min}…"
        uv pip install -p "$ROOT/backend/.venv" -U "$pkg>=$min" \
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

say "setup complete — run ./dev.sh (dev) or ./vw start (service) to start Virtual Worker."
