# AGENTS.md — Virtual Worker

Instructions for AI coding agents (and humans) working in this repository.

> **Virtual Worker** is an open-source, **local-first** platform for running autonomous
> AI workers: agents with a persona, tools, skills, memory, a task board, schedules and
> guardrails, served by a local model by default. A React app talks to a FastAPI backend
> that is also an OpenAI-compatible gateway in front of any model server (llama.cpp,
> MLX, Ollama, vLLM, or a remote provider). Nothing leaves the machine unless the user
> explicitly connects something. The product plan is `docs/PRD.md`.

## Repository structure

```
virtual-worker/
├── backend/            # FastAPI app (Python) — the gateway + all /api routes
│   ├── app.py          # entry point: registers routers, starts background loops
│   ├── config.py       # env + paths (VW_HOME, upstream, model map, …)
│   ├── routes/         # one module per API surface (agents, hub, chat, …)
│   ├── storage/        # SQLite-backed stores (app.db) + the settings file
│   ├── agent/          # the agent harness — runner.stream_run() → compose() → loop.run(); tools, worker,
│   │                   #   scheduler, triggers, inbox (flow-back), crusher (context compression), wiki,
│   │                   #   browser_control, skill_factory, team, vision_helper, doc_tools, …
│   ├── channels/       # chat-channel plugins: base contract, manager, whatsapp/ (+ Node bridge), telegram, discord, slack
│   ├── harness/        # coding harnesses: base.py (jobs, settings), claude_code.py, codex.py, commands.py (/code, /codex)
│   ├── vw_cli.py       # `./vw` service mode: detached supervisor, auto-restart, built UI
│   ├── runtime/        # managed model server + engine management
│   ├── gateway/        # protocol translators (Anthropic / Responses / Gemini) + providers.py /
│   │                   #   egress.py / pricing.py (BYOK external models normalized to one chunk shape)
│   ├── connectors/     # stub (portal connectors are not part of this build)
│   ├── middleware/     # api_debug (trace capture) + team_auth (optional auth)
│   └── tests/          # pytest suite (test_*.py)
├── frontend/           # React + Vite + TypeScript + Tailwind v4
│   └── src/
│       ├── App.tsx     # app shell: sidebar nav + routed content pane
│       ├── features/<name>/<name>-page.tsx   # one folder per screen
│       ├── lib/        # api.ts (apiJson/apiPost/apiFetch), auth.tsx
│       ├── assets/     # icon.svg, logo.svg
│       └── index.css   # the @theme design tokens (palette + fonts)
├── docs/               # PRD.md (product plan), ARCHITECTURE.md, INSTALL.md
├── desktop/            # Electron shell + PyInstaller spec for the packaged app
├── browser-extension/  # Chrome companion: groups agent tabs when attached to the user's Chrome
├── registry-scaffold/  # starter repo layout for a git-backed template registry
├── samples/            # example skill packs
├── dev.sh              # bootstrap-on-first-run + start backend + frontend (hot reload)
├── vw                  # service control: start | stop | restart | status | logs
└── setup.sh            # idempotent installer (venv, npm, llama.cpp, mlx)
```

## Build & dev commands

```bash
./dev.sh            # hot-reload dev profile
#   web   → http://localhost:5173
#   api   → http://127.0.0.1:8889   (OpenAPI docs: /docs)
#   model → http://127.0.0.1:8081/v1 (backend-managed)

./vw start          # always-on service: built UI + API on one port, auto-restart
#   app   → http://127.0.0.1:8888   (model server on :8080)
./vw status | logs -f | restart | stop
```

Dev and service use **different default ports** so both can run side by side
(`./vw` → 8888/8080, `./dev.sh` → 5173/8889/8081; override with `BACKEND_PORT`,
`LLAMA_PORT`, `FRONTEND_PORT`, `VW_PORT`). Each also gets its own data dir
(`./data` for dev, `~/.virtual-worker` for the service). Service mode is
`backend/vw_cli.py`: a detached supervisor (pid file `<VW_HOME>/service.json`,
log `<VW_HOME>/logs/service.log`) that runs uvicorn without `--reload` and
serves `frontend/dist` from the backend (`VW_SERVE_UI=1`).

Prerequisites it can't install: **uv**, **Node/npm**, **Homebrew** (for llama.cpp).
Re-run the installer any time with `./setup.sh`. Skip MLX with `VW_SKIP_MLX=1`.

### Backend (Python, FastAPI)
```bash
cd backend
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8889 --reload   # run (dev port)
.venv/bin/python -m pytest -q                                     # all tests
.venv/bin/python -m pytest -q tests/test_scheduler.py             # one file
```
The venv is created by `setup.sh` with `uv`; deps are in `requirements.txt`.
**No new dependency without a clear reason.** The core leans on stdlib + `httpx` +
`fastapi` and stays dependency-light; integrations belong behind plugin interfaces.

### Frontend (React + Vite + TS)
```bash
cd frontend
npm install
BACKEND_PORT=8889 npm run dev   # Vite dev server (proxies /api, /v1, /v1beta → BACKEND_PORT)
npm run build       # tsc --noEmit && vite build — MUST pass before a PR
npm run typecheck   # tsc --noEmit only
```

## Architecture (backend)

```
inbound (browser / coding agents / later: chat channels)
  → ApiDebugMiddleware (tees a bounded copy of every model call → app.db)
  → TeamAuthMiddleware (no-op in solo mode; gates /api when team mode is on)
  → gateway translators (/v1/messages · /v1/responses · /v1beta/… → OpenAI shape)
  → routes/* (the /api surface)
  → agent.compose(agent) → registry {name:{schema, fn}} → agent.loop.run(...)
  → runtime.llama_manager (managed model server) ─httpx─▶ upstream (:8080)
```

- **Coding harnesses** (`harness/`, PRD §8.13): `base.py` holds the adapter contract,
  settings, the `Job` record and subprocess supervision; `claude_code.py` reads
  `~/.claude/sessions` (live registry) + `~/.claude/projects/*/*.jsonl` (transcripts) and
  runs `claude -p --output-format stream-json` (a live session is `--fork-session`ed);
  `codex.py` reads `~/.codex/sessions/**/rollout-*.jsonl` and runs `codex exec --json`
  (result via `-o`). Surfaces: `agent/harness_tools.py` (`code_*` with a `harness` arg,
  gated by `guardrails.codingHarness`), `harness/commands.py` (`/code`, `/codex`; results
  flow back via `manager.send`), `routes/harness.py`, Settings card. New sessions only
  under `allowedRoots`. `VW_CLAUDE_HOME/BIN`, `VW_CODEX_HOME/BIN` override paths. To add a
  harness: implement the `Adapter` protocol, `base.register()` it, import it in commands.
- **Document tools** (`agent/doc_tools.py`): `document_read` / `document_info` for PDF
  (pypdf), docx/xlsx/pptx (stdlib zip + xml), CSV and text. Always composed; paths must be
  under the working folder or `<VW_HOME>/channels` (where chat attachments land). Output
  is chunked with `offset`/`pages`/`sheet`.
- **Delivery kinds** (`agent/delivery.py`): `in_app`, `desktop`, `webhook` (URL
  allow-list), `chat` (`<accountId>:<conversation>`), `email` (SMTP target with an
  `allowTo` allow-list; password write-only under `deliveryTargetSecrets`). Every send is
  audited in `deliveries`.
- **One run entry point.** Every surface (chat route, worker, scheduler, triggers,
  inbox flow-back, chat channels) calls `agent/runner.py:stream_run()`, which resolves
  the model target (local or a BYOK provider via `gateway/providers.py:resolve_target`),
  composes the system prompt + tool registry, wires MCP servers, and drives `loop.run`
  with the agent's guardrail knobs. `execute_run()` collects `(text, error, run_id)`.
- **Agent tools** are assembled per run in `agent/compose.py` as a registry
  `{name: {"schema": <openai fn schema>, "fn": <async callable>}}`, then passed to
  `agent/loop.py:run()`. Per-agent tool factories follow one pattern (see
  `make_memory_tools`, `make_schedule_tools`, `make_kb_tools`, `make_custom_tools`,
  `make_team_tools`, `make_deliver_tool`). To add a tool, write a factory and wire it
  in `compose()`.
- **Always-on background loops**, started in `app.py`'s startup hook: the
  **scheduler** (`agent/scheduler.py`), the **trigger engine** (`agent/triggers.py`),
  the board **worker** (`agent/worker.py`, opt-in autonomous task execution with a
  verify pass), the **channel manager**, and the **maintenance** loop
  (`runtime/maintenance.py`: retention pruning + WAL checkpoint + the opt-in scheduled
  backup snapshot, hourly). Loops are
  best-effort: log errors, never crash.
- **Security baseline** (`app.py`): CORS local-origins-only (+ `VW_CORS_ORIGINS`),
  `TrustedHostMiddleware` (+ `VW_ALLOWED_HOSTS`), httpx logging quieted and a
  `_RedactFilter` scrubbing tokens from log lines; webhook paths bypass the team-mode
  cookie gate because they carry their own proof (HMAC / unguessable token). Keep every
  new inbound endpoint under that rule. See `SECURITY.md`.
- **Team mode** (`middleware/team_auth.py`): when enabled, every `/api` call needs a
  session; mutating calls must meet the role floor in `_ROLE_POLICY` (longest prefix
  wins, default `member`; installation-wide config/secrets are `admin`); every mutating
  call is written to `audit_log` (`storage/audit_store.py`). A new admin-only route must
  be added to `_ROLE_POLICY` or it defaults to member.
- **Stores** (`storage/*_store.py`) all open `app.db` the same way: a `_conn()` with
  `PRAGMA journal_mode=WAL` + `row_factory=sqlite3.Row`, ISO timestamps, a
  `threading.Lock`, and `camelCase` dicts out. Mirror an existing store when adding
  one, and register any new route in `app.py`.
- **Chat channels** (`backend/channels/`, PRD §8.6): `base.py` is the plugin contract
  (`ChannelAdapter` + `InboundMessage` + `registry`); `manager.py` is the shared spine
  (dedup → policy → resolve agent → per-conversation FIFO/debounce → slash commands →
  history from the chat session → `execute_turn` with timeout/typing → strip reasoning →
  persist → chunk + send → delivery audit). Adapters only move messages. WhatsApp ships
  two: `whatsapp/linked.py` spawns the Node sidecar in `whatsapp/bridge/` (Baileys,
  JSON lines over stdio, restarted with backoff) and `whatsapp/cloud.py` is the official
  webhook (`routes/channels.py` hosts `/api/channels/webhook/{kind}/{account}`).
  **Telegram** (`telegram.py`) is pure httpx long polling (`getUpdates`), the model for
  any Bot-API-style transport. `channels/builtin.py` imports every adapter so it registers.
  Accounts/bindings/dedup/pairing live in `storage/chat_channel_store.py`; secrets go to
  `settings.json` under `chatChannelSecrets` and are never returned. To add a channel:
  subclass `ChannelAdapter`, decorate with `@registry.register`, import it in
  `channels/whatsapp/__init__.py`-style from `app.py`'s startup, add tests with a fake
  adapter (see `tests/test_channel_manager.py`).

## Configuration & data (local-first)

All state lives under **`VW_HOME`** (default `~/.virtual-worker`; `dev.sh` sets it to
`./data`). The SQLite db is `app.db`; app settings are `settings.json`.

| Env var | Default | Purpose |
|---|---|---|
| `VW_HOME` | `~/.virtual-worker` | data dir (db, settings, logs, pid) |
| `VW_UPSTREAM_URL` | `http://127.0.0.1:8080` | OpenAI-compatible model server; setting it disables managed mode |
| `VW_MANAGED_LLAMA` | `1` | backend starts/stops/switches the model server |
| `VW_MODEL_MAP` / `VW_DEFAULT_MODEL` | – | map vendor model ids → your local model |
| `VW_GATEWAY_KEY` | – | require a static key on the gateway |
| `VW_DEBUG_CAPTURE` | `1` | capture model calls into the trace store |
| `VW_SKIP_MLX` | `0` | skip the MLX engines on Apple Silicon |
| `LLAMA_*` / `MLX_*` | – | engine binaries, port, ctx, GPU layers |
| `HF_TOKEN` | – | gated/private Hugging Face repos |
| `VW_ALLOWED_HOSTS` / `VW_CORS_ORIGINS` | – | extra Host names / browser origins (local ones are always allowed) |
| `VW_*_RETENTION_DAYS`, `VW_TRACE_MAX_RUNS` | 30/90/90/7/14 | maintenance-loop retention (traces, activity, deliveries, chat dedup, media) |
| `VW_NODE_BIN` | `node` | Node binary for the WhatsApp bridge sidecar |
| `VW_WHATSAPP_MAX_MEDIA_BYTES` | 25 MB | cap on a received WhatsApp attachment |

API gateway tokens are `sk-vw-<hex>` (only a SHA-256 hash is stored). The gateway is
open until the first token is minted, then a valid token is required.

## Conventions

- **Python**: standard library style; type hints; small focused modules. Errors are
  best-effort in background loops (log, never crash the loop).
- **TypeScript/React**: functional components + hooks; `const` over `let`; camelCase
  vars, PascalCase types. Data fetching goes through `@/lib/api`. One screen = one
  `features/<name>/` folder.
- **Styling**: Tailwind v4 with the `@theme` tokens in `src/index.css`. Use the tokens
  (`bg-primary`, `text-ink`, `bg-base`, `border-pressed`, …), don't hard-code hex.
- **Local-first is a hard constraint.** Any network egress (web fetch, remote MCP,
  webhook delivery, remote upstream, chat channel) must be explicit and opt-in.
- **Secrets** are write-only: stored once, never echoed to the UI, redacted from logs
  and traces.
- **No company-specific code.** Integrations ship as generic, configurable plugins.
- **Commits**: conventional — `feat:`, `fix:`, `chore:`, `docs:`, with an optional
  scope such as `feat(channels):`.

## Workflow — branch + PR

- **Never push directly to `main`.** Create a feature branch, commit, push, open a PR,
  merge after review.
  ```bash
  git checkout -b feat/<scope>-<desc>
  # … work …
  git push -u origin feat/<scope>-<desc>
  gh pr create --base main --title "feat: …" --body "…"
  ```
- For parallel work, use a **git worktree**
  (`git worktree add ../vw-wt-<slug> -b <branch> main`).

## Testing — gate every change

A change isn't done until both pass:
```bash
cd backend  && .venv/bin/python -m pytest -q       # backend
cd frontend && npm run build                       # tsc --noEmit && vite build
```
- Add backend tests under `backend/tests/test_*.py`. **Isolate stores** by
  `monkeypatch.setattr(config, "APP_HOME", tmp_path)` (see `tests/test_scheduler.py`).
- Make tests hermetic: monkeypatch model activation / chat completion / embeddings so
  no real model is loaded.
- `scripts/smoke_onboarding.py` drives the first-run wizard in headless Chromium (Playwright) against a started service.
- For a real end-to-end check with a local model (activation → chat tool call → board
  worker → channel webhook turn), run `scripts/smoke_live.py` against a started service
  (usage in its docstring). It is not part of CI.

## Where to read more

- `docs/ARCHITECTURE.md` — the full backend/frontend architecture, run path, data model.
- `docs/PRD.md` — the product plan: concepts, requirements, milestones, open decisions.
- In-app **Docs** (the Docs tab) — end-user documentation for every screen.
