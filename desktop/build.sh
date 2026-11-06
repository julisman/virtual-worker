#!/usr/bin/env bash
# Build the one-click Virtual Worker desktop app (.dmg) end to end.
#
#   desktop/build.sh                 # unsigned .dmg (right-click → Open to run)
#   CSC_LINK=... CSC_KEY_PASSWORD=... \
#     APPLE_ID=... APPLE_APP_SPECIFIC_PASSWORD=... APPLE_TEAM_ID=... \
#     desktop/build.sh               # signed + notarized .dmg (true one-click)
#
# Stages: build frontend → freeze backend (PyInstaller) → fetch llama-server →
# stage resources → electron-builder. Output: desktop/release/Virtual Worker-<ver>-<arch>.dmg
#
# Target architecture (Apple Silicon vs Intel) is chosen with VW_TARGET_ARCH:
#   VW_TARGET_ARCH=arm64 desktop/build.sh    # Apple Silicon (default on M-series)
#   VW_TARGET_ARCH=x64   desktop/build.sh    # Intel — see the cross-build note below
# Default is the host arch. PyInstaller can't cross-compile, so an x64 build must
# freeze the backend on an x64 toolchain: either run on a real Intel Mac, or on
# Apple Silicon under Rosetta with an x86_64 Python 3.13 (pass it via
# VW_BUILD_PYTHON). This script auto-prefixes the freeze with `arch -x86_64` when
# it's asked to build x64 on an arm64 host; it aborts early if that can't produce
# an x86_64 interpreter, rather than shipping an arm64 backend in an Intel app.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESK="$ROOT/desktop"
RES="$DESK/resources"
PY="${VW_BUILD_PYTHON:-python3}"          # a Python 3.13 to build the freeze venv
LLAMA_TAG="${LLAMA_TAG:-b9881}"            # llama.cpp release tag to bundle

say() { printf "\n\033[1;34m▶ %s\033[0m\n" "$*"; }
die() { printf "\n\033[1;31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

# --- Resolve target arch (arm64 | x64) and any cross-build prefix ---------- #
case "$(uname -m)" in arm64) HOST_ARCH=arm64;; x86_64) HOST_ARCH=x64;; *) HOST_ARCH="";; esac
TARGET_ARCH="${VW_TARGET_ARCH:-$HOST_ARCH}"
case "$TARGET_ARCH" in
  arm64) LA="macos-arm64" ;;
  x64)   LA="macos-x64" ;;
  *) die "unsupported VW_TARGET_ARCH='$TARGET_ARCH' (use arm64 or x64)" ;;
esac
# Cross-build: x64-on-arm64 is supported via Rosetta; arm64-on-x64 is not (no
# reverse translator). Everything that produces native artifacts (the freeze
# venv + PyInstaller) runs under this prefix so it emits x86_64 Mach-O. The
# native case uses `env` (a no-op prefix) rather than an empty array — macOS's
# bash 3.2 errors on "${arr[@]}" for an empty array under `set -u`.
ARCH_PREFIX=(env)
CROSS=""
if [ "$TARGET_ARCH" != "$HOST_ARCH" ]; then
  [ "$TARGET_ARCH" = "x64" ] && [ "$HOST_ARCH" = "arm64" ] \
    || die "can't cross-build $TARGET_ARCH on a $HOST_ARCH host — build on an $TARGET_ARCH machine"
  command -v arch >/dev/null || die "'arch' not found — needed to run the x64 freeze under Rosetta"
  ARCH_PREFIX=(arch -x86_64)
  CROSS=" — cross-building under Rosetta"
  # The interpreter must actually be able to run as x86_64 (universal2 or Intel).
  "${ARCH_PREFIX[@]}" "$PY" -c 'import platform,sys; sys.exit(0 if platform.machine()=="x86_64" else 1)' 2>/dev/null \
    || die "VW_BUILD_PYTHON='$PY' can't run as x86_64 under Rosetta — install an x86_64/universal Python 3.13 and pass it via VW_BUILD_PYTHON"
fi
say "Target arch: $TARGET_ARCH (host: ${HOST_ARCH:-unknown})$CROSS"

# 1) Frontend -------------------------------------------------------------- #
say "Building frontend (vite)…"
( cd "$ROOT/frontend" && npm install && npm run build )

# 2) Freeze the backend ---------------------------------------------------- #
# venv + dist are arch-suffixed so an arm64 and an x64 build never reuse each
# other's native artifacts (a shared venv would pin the wrong architecture).
say "Freezing backend (PyInstaller, $TARGET_ARCH)…"
VENV="$DESK/.freeze-venv-$TARGET_ARCH"
PYI_DIST="$DESK/.pyi-dist-$TARGET_ARCH"
[ -x "$VENV/bin/python" ] || "${ARCH_PREFIX[@]}" "$PY" -m venv "$VENV"
"${ARCH_PREFIX[@]}" "$VENV/bin/python" -m pip install -q --upgrade pip
# Force prebuilt wheels (never compile native deps from source). Belt-and-braces
# for the whole freeze, and essential for the x64-under-Rosetta build: without it
# pip fell back to building `cryptography` (a transitive dep) from its Rust/OpenSSL
# sdist, which fails under emulation. Every dep here publishes a macOS wheel
# (cryptography ships a universal2 wheel that satisfies x86_64), so this resolves
# cleanly and fails loudly rather than silently needing a compiler if that changes.
"${ARCH_PREFIX[@]}" "$VENV/bin/python" -m pip install -q --only-binary=:all: -r "$DESK/requirements-desktop.txt"
( cd "$ROOT" && PYTHONPATH=backend "${ARCH_PREFIX[@]}" "$VENV/bin/pyinstaller" --clean --noconfirm \
    --distpath "$PYI_DIST" --workpath "$DESK/.pyi-build-$TARGET_ARCH" "$DESK/backend.spec" )

# 3) Fetch a prebuilt llama-server (the model engine) ---------------------- #
# LA (macos-arm64 | macos-x64) was resolved from TARGET_ARCH above. The cache is
# tagged with the arch it holds; a mismatch (e.g. an arm64 binary left from a
# previous build) is wiped so an x64 app never ships the arm64 engine.
say "Fetching llama-server ($LLAMA_TAG, $LA)…"
mkdir -p "$RES/bin"
if [ "$(cat "$RES/bin/.arch" 2>/dev/null)" != "$TARGET_ARCH" ]; then
  rm -f "$RES/bin/llama-server" "$RES/bin"/*.dylib "$RES/bin"/*.metal 2>/dev/null || true
fi
if [ -n "$LA" ] && [ ! -x "$RES/bin/llama-server" ]; then
  # Release assets are now .tar.gz with a flat llama-<tag>/ dir (binary + dylibs + .metal).
  TARBALL="llama-$LLAMA_TAG-bin-$LA.tar.gz"
  URL="https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/$TARBALL"
  TMP="$(mktemp -d)"
  if curl -fL "$URL" -o "$TMP/$TARBALL" 2>/dev/null; then
    ( cd "$TMP" && tar -xzf "$TARBALL" )
    # Keep the binary next to its dylibs/metal shaders (it loads them via @loader_path).
    find "$TMP" \( -name 'llama-server' -o -name '*.dylib' -o -name '*.metal' \) -exec cp {} "$RES/bin/" \;
    chmod +x "$RES/bin/llama-server" 2>/dev/null || true
    printf '%s' "$TARGET_ARCH" > "$RES/bin/.arch"   # tag the cache with its arch
    echo "  bundled llama-server + $(ls "$RES/bin"/*.dylib 2>/dev/null | wc -l | tr -d ' ') libs"
  else
    echo "  ⚠ could not download llama-server — the app will look for one on PATH."
  fi
  rm -rf "$TMP"
fi

# 3a) Fetch uv (the managed-engine installer) ------------------------------ #
# Settings → Engines uses uv at runtime to install/update the MLX packages into
# a venv under APP_HOME/engines (the frozen backend excludes them — heavy).
# uv can download its own CPython, so the user's Mac needs nothing preinstalled.
say "Fetching uv (engine installer)…"
if [ ! -x "$RES/bin/uv" ] || [ "$(cat "$RES/bin/.uv-arch" 2>/dev/null)" != "$TARGET_ARCH" ]; then
  UV_TRIPLE=$([ "$TARGET_ARCH" = "arm64" ] && echo "aarch64-apple-darwin" || echo "x86_64-apple-darwin")
  if [ -n "${UV_TAG:-}" ]; then
    UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_TAG/uv-$UV_TRIPLE.tar.gz"
  else
    UV_URL="https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_TRIPLE.tar.gz"
  fi
  TMP="$(mktemp -d)"
  if curl -fL "$UV_URL" -o "$TMP/uv.tar.gz" 2>/dev/null; then
    ( cd "$TMP" && tar -xzf uv.tar.gz )
    find "$TMP" -name uv -type f -perm +111 -exec cp {} "$RES/bin/uv" \;
    chmod +x "$RES/bin/uv"
    printf '%s' "$TARGET_ARCH" > "$RES/bin/.uv-arch"
    echo "  bundled uv $("$RES/bin/uv" --version 2>/dev/null | head -1 || echo '(version unknown — cross-arch)')"
  else
    echo "  ⚠ could not download uv — in-app MLX engine install will need uv on PATH."
  fi
  rm -rf "$TMP"
fi

# 3b) Browser engine ------------------------------------------------------- #
# The app drives the user's system Google Chrome (config.BROWSER_CHANNEL=chrome in
# packaged mode), so we DON'T bundle a ~150 MB Chromium. Only the Playwright node
# driver (inside the frozen backend) is needed. Set VW_BROWSER_CHANNEL="" and
# uncomment below to bundle Chromium instead (self-contained but larger).
# PLAYWRIGHT_BROWSERS_PATH="$RES/ms-playwright" "$VENV/bin/python" -m playwright install chromium
rm -rf "$RES/ms-playwright"   # ensure no stale bundled Chromium from an earlier build

# 4) Stage resources ------------------------------------------------------- #
say "Staging resources…"
rm -rf "$RES/backend" "$RES/frontend"
mkdir -p "$RES/backend" "$RES/frontend"
cp -R "$PYI_DIST/vw-backend" "$RES/backend/vw-backend"
cp -R "$ROOT/frontend/dist/." "$RES/frontend/"

# 5) Package --------------------------------------------------------------- #
# electron-builder mounts a DMG volume (/Volumes/Virtual Worker <ver>) while building and
# detaches it after. A volume left mounted by a previous or failed run makes
# hdiutil's detach fail and aborts packaging — proactively unmount any stragglers.
for _v in /Volumes/Virtual Worker\ *; do
  [ -d "$_v" ] && hdiutil detach -force "$_v" >/dev/null 2>&1 || true
done
# Package the single arch matching the staged resources. electron-builder.yml
# pins no arch, so this --$arch flag alone decides it (a config arch would be
# additive → both arches in one run; see the yml comment).
say "Packaging with electron-builder ($TARGET_ARCH)…"
( cd "$DESK" && npm install && npx electron-builder "--$TARGET_ARCH" --config electron-builder.yml )

say "Done. Installer(s) in: $DESK/release"
ls -lh "$DESK/release"/*.dmg 2>/dev/null || true
