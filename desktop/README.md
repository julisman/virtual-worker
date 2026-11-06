# Virtual Worker Desktop — one-click installer

Packages Virtual Worker (FastAPI backend + React UI + llama.cpp engine) into a native
**macOS `.app` inside a `.dmg`**, so a non-technical user can **double-click to
install and double-click to launch** — no Terminal, no Python, no Homebrew, no
`npm`. Updates install themselves.

## How it works

```
Virtual Worker.app
├─ Electron shell (main.js)           ← starts the backend, opens the window
└─ Contents/Resources/
   ├─ backend/vw-backend/            ← the Python backend, frozen (PyInstaller) — no system Python
   ├─ frontend/                       ← the built React SPA (served by the backend)
   └─ bin/llama-server (+ libs)       ← the model engine
```

On launch the shell picks a free localhost port, starts `vw-backend`, waits for
`/health`, then opens a window at `http://127.0.0.1:<port>/`. The backend serves
both the API and the UI (single origin). Everything stays on the machine; the only
outbound call is the update check.

## Build it

Requires (on the *build* machine only): **Python 3.13**, **Node/npm**, macOS.

```bash
VW_BUILD_PYTHON=python3.13 desktop/build.sh
# → desktop/release/Virtual Worker-<version>-<arch>.dmg   (arch = arm64 by default)
```

`build.sh` runs the whole pipeline: build frontend → freeze backend (PyInstaller,
`backend.spec`) → download a prebuilt `llama-server` → stage resources →
`electron-builder`.

### Apple Silicon vs Intel

Pick the target arch with `VW_TARGET_ARCH` (defaults to the host arch):

```bash
VW_TARGET_ARCH=arm64 VW_BUILD_PYTHON=python3.13 desktop/build.sh   # Apple Silicon
VW_TARGET_ARCH=x64   VW_BUILD_PYTHON=python3.13 desktop/build.sh   # Intel
```

Each run produces one arch-named installer (`Virtual Worker-<version>-arm64.dmg` /
`Virtual Worker-<version>-x64.dmg`) — ship both so either Mac can install.

**Intel caveat:** PyInstaller can't cross-compile, so the x64 backend must be
frozen on an x64 toolchain. Either build on a real Intel Mac, or on Apple Silicon
**under Rosetta with an x86_64 (or universal2) Python 3.13** passed via
`VW_BUILD_PYTHON`. When it detects `x64` on an `arm64` host, `build.sh`
auto-prefixes the freeze with `arch -x86_64` and **aborts early** if the given
Python can't run as x86_64 — so it never ships an arm64 backend inside an Intel
app. The freeze venv, PyInstaller dist, and the `llama-server` cache are all
arch-tagged, so alternating arm64/x64 builds don't contaminate each other.

> Update feed note: `latest-mac.yml` is regenerated per run, so the last arch
> built wins the auto-update manifest. For a two-arch update feed, publish/merge
> both arches' `latest-mac.yml` entries (or run the arm64 build last, since that's
> the majority of Macs). The `.dmg`s themselves are always fine to hand out.

## Updates

The app checks the GitLab feed on launch (and every 6h) via **electron-updater**.
There are two modes:

- **Notify mode (default — works UNSIGNED, for now).** When a newer version exists,
  the app shows *"Virtual Worker x.y is available → Download"* and opens the GitLab Releases
  page in the browser. The user downloads the new `.dmg` and drags it in. This is
  what your colleagues get today, no Apple Developer ID needed.
- **Silent auto-install (SIGNED builds only).** Build with `VW_AUTO_INSTALL=1` once
  you have a Developer ID — the update downloads in the background and installs on
  *"Restart now"*. macOS (Squirrel) refuses to install unsigned updates, which is
  why this is gated on signing.

User data + models live in `~/Library/Application Support/Virtual Worker` and are untouched by
either path.

### One command: `release.sh`

```bash
# build + tag only (the .dmg is all colleagues need to install):
VW_BUILD_PYTHON=python3.13 desktop/release.sh
# build + tag + publish the GitLab release (also powers the update notice):
GITLAB_TOKEN=<your-gitlab-token> VW_BUILD_PYTHON=python3.13 desktop/release.sh
```

It reads the version from `package.json`, runs `build.sh`, tags the commit, and —
with a token (scope `api`) — uploads the artifacts and creates the Release with the
correct asset paths. Without a token it stops after building and prints the manual
steps below.

### Publishing a GitLab release (so the feed works)

electron-builder has no native GitLab provider, so publishing is a manual upload of
the artifacts `build.sh` produces in `desktop/release/`:

1. Bump `desktop/package.json` `version`, run `desktop/build.sh`.
2. Create a **GitLab Release** for the tag (e.g. `v0.2.0`).
3. Attach these as **release asset links**, each with the matching **filepath** so
   they resolve under the "latest" permalink the app reads:
   - `latest-mac.yml`  → filepath `/latest-mac.yml`
   - `Virtual Worker-<ver>-arm64.dmg` (+ its `.blockmap`) → filepath `/Virtual Worker-<ver>-arm64.dmg`
   - `Virtual Worker-<ver>-arm64-mac.zip` (+ `.blockmap`) → filepath `/Virtual Worker-<ver>-arm64-mac.zip`

   The feed URL in `electron-builder.yml` is the GitLab *latest-release permalink*:
   `…/-/releases/permalink/latest/downloads`, so it always points at your newest
   release without editing config.

> If the repo is private, the in-app auto-*check* may need auth to read
> `latest-mac.yml` and will just stay quiet if it can't — colleagues still update
> by opening the Releases page (where they're logged in). For a silent check that
> always works, host `latest-mac.yml` + artifacts on **GitLab Pages** and point the
> feed `url` there instead.

## Sharing with colleagues (no Apple Developer ID)

> 👉 Non-technical, paste-ready install instructions for teammates live in
> **[`docs/INSTALL.md`](../docs/INSTALL.md)** — send them that alongside the `.dmg`.

An **unsigned** `.dmg` works fine for internal use — the only difference is a
one-time Gatekeeper prompt. Hand a colleague the `.dmg` and tell them:

1. Open the `.dmg`, drag **Virtual Worker** to **Applications**.
2. First launch only: **right-click (or Control-click) the Virtual Worker app → Open →
   "Open"** in the dialog. (Double-clicking shows "cannot be opened" — right-click
   → Open is the way past it. They only do this once.)
3. If macOS still blocks it, run once in Terminal:
   `xattr -dr com.apple.quarantine /Applications/Virtual Worker.app`

After that it launches normally. Updates come via the in-app "Download" prompt
(see above). When you're ready to remove even the right-click step, get a Developer
ID and sign — nothing else changes.

## Signing & notarization (for a true one-click experience)

Without an Apple **Developer ID** ($99/yr), the `.dmg` still works but Gatekeeper
makes the user **right-click → Open** the first time. To ship a clean double-click:

```bash
# a "Developer ID Application" cert in your login keychain (or CSC_LINK/CSC_KEY_PASSWORD)
export APPLE_ID="you@example.com"
export APPLE_APP_SPECIFIC_PASSWORD="<app-specific-password>"   # appleid.apple.com
export APPLE_TEAM_ID="ABCDE12345"
desktop/build.sh    # signs + notarizes automatically (assets/notarize.js)
```

## v1 scope / limitations (all addable later)

- **macOS Apple Silicon + Intel** — both via `VW_TARGET_ARCH` (see "Apple Silicon
  vs Intel" above). Windows: add a target in `electron-builder.yml`.
- **Bundled backend excludes** Playwright (browser tools), MLX (Apple-only
  inference + fine-tuning), and transformers — to keep the bundle ~small and the
  freeze reliable. Each is import-guarded, so those features simply show an
  "install to enable" state rather than breaking. Core serve/chat/agents/models
  all work.
- **Models are downloaded on first run** from the in-app Hub (multi-GB, one-time).
  The installer ships the *app*, not the models — the right trade for local-first.
