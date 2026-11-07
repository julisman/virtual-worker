# Virtual Worker — Architecture

_Last updated: 2026-11-07. Describes the code as it is in this repository; the
product plan lives in [`PRD.md`](PRD.md)._

Virtual Worker is a local-first platform for running autonomous AI workers. A
worker is an **agent** (the code's term) with a persona, a model, tools, skills,
memory, a working folder, a task board, schedules, triggers and guardrails. The
system is one React app talking to one FastAPI backend. The backend is also an
OpenAI-compatible gateway in front of a model server, manages that server, runs
the always-on loops, and speaks to chat apps so people can message a worker from
WhatsApp or Telegram.

Four rules shape everything below:

1. **Local-first.** State lives in one directory. The model runs locally unless
   an agent is explicitly put on an external provider. Every network egress is
   opt-in and named: a provider key, a chat channel account, a webhook target.
2. **One run path.** Every surface that makes an agent think goes through
   `agent/runner.py`. Guardrails, model resolution, MCP wiring and tracing are
   therefore identical for a chat message, a scheduled job, a board task, a
   trigger and a WhatsApp message.
3. **Guardrails in code.** Confinement, deny-lists, caps and the kill switch are
   enforced by the runtime, never only by the prompt.
4. **Everything is recorded.** Every model call is captured as a trace; every
   agent action lands in the activity feed; every outbound message is audited.

---

## 1. High-level diagram

```
          Browser                        Phone / chat apps
   React + Vite + Tailwind            WhatsApp · Telegram
   dev :5173  ·  service :8888          (adapters dial OUT)
            │                                   │
            │ /api, /v1, /v1beta                │ bridge stdio / long poll / webhook
            ▼                                   ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │  Backend — FastAPI  (dev :8889 · service :8888)                        │
  │   middleware: trace capture · trusted hosts · CORS · team auth         │
  │   routes/*  → the /api surface                                         │
  │   gateway/  → /v1 (OpenAI) · /v1/messages (Anthropic) · /v1/responses  │
  │               · /v1beta (Gemini)  — translated to one upstream shape   │
  │   agent/runner.stream_run()  ← chat · worker · scheduler · triggers    │
  │        │                        · inbox flow-back · channels           │
  │        ▼                                                               │
  │   compose() → tool registry + system prompt → loop.run()               │
  │        │                                                               │
  │        ├── local model  (managed llama.cpp / MLX, or any upstream)     │
  │        └── external provider (BYOK: OpenAI, Anthropic, DeepSeek, …)    │
  │                                                                        │
  │   always-on: scheduler · triggers · inbox · channel manager            │
  │              · board workers (on duty) · maintenance                   │
  └────────────────────────────────────────────────────────────────────────┘
            │                                   │
            ▼                                   ▼
   model server :8081 (dev) / :8080     ~/.virtual-worker/  (VW_HOME)
   llama-server · mlx_lm.server         app.db · settings.json · skills/ · wikis/
   or Ollama / vLLM / LM Studio         channels/ · models/ · backups/ · logs/
```

Two ways to run: `./dev.sh` (hot reload, Vite serves the UI) and `./vw start`
(detached supervisor, backend serves the built UI on one port). They use
different default ports and data directories so both can run at once.

---

## 2. Components

### 2.1 Frontend (`frontend/`)

React 18, Vite 6, TypeScript, Tailwind CSS v4. No router or data library: one
`features/<name>/<name>-page.tsx` per screen, a sidebar shell in `App.tsx`, and
`lib/api.ts` for fetches. Light and dark themes via `@theme` tokens in
`index.css`; markdown rendering through `react-markdown`.

| Screen | What it does |
|---|---|
| Chat (`playground`) | Streams an agent run over SSE from `POST /api/chat`, tool steps inline, attachments, per-agent history. |
| Agents | Roster, per-agent tabs (Overview, Board, Activity, Schedule, Triggers, Conversations, Inbox, Settings) and the settings editor (identity, model and provider, tools, knowledge, integrations, memory, guardrails). Sub-tabs: Skills, MCP, Tools. |
| Channels | Chat-channel accounts: connect, pair by QR, policies, allow-list and pairing approvals, per-chat routing, test send. |
| Team | Multi-agent org view and delegation feed. |
| Templates | Gallery, deploy with variables, export, import, git registries, update with diff. |
| Knowledge, Wiki | Knowledge bases (RAG) and agent-maintained wikis. |
| Models (`hub`), Connect | Discover and download models, activate; copy-paste setup for coding agents. |
| Deliveries | Audit log of every outbound send with filters and the sent text. |
| Debug, Monitor, Analytics | Trace viewer, live resources and health, cost and usage. |
| Docs, Settings | In-app documentation; engines, browser, email delivery, backup, team mode, vision helper. |

In dev, Vite proxies `/api`, `/v1` and `/v1beta` to the backend port from
`BACKEND_PORT`. In service and packaged mode the backend serves `frontend/dist`
itself with a single-page fallback.

### 2.2 Backend (`backend/`)

| Package | Responsibility |
|---|---|
| `app.py` | Assembles the app: middleware, routers, the lifespan that starts and stops the background loops, health, static UI. |
| `config.py` | Paths and environment: `APP_HOME`, model server and engine settings, browser settings, security (CORS, hosts), retention. |
| `routes/` | One module per `/api` surface. Routes are thin: validate, call a store or the runner, return `camelCase` JSON. |
| `agent/` | The harness and everything an agent can do: `runner`, `compose`, `loop`, tools, worker, scheduler, triggers, inbox, verify. |
| `channels/` | Chat-channel plugin contract, the shared manager, and the WhatsApp and Telegram adapters. |
| `gateway/` | The inbound protocol translators, the local upstream client, and the external-provider layer (`providers`, `egress`, `pricing`). |
| `runtime/` | Managed processes and housekeeping: `llama_manager`, `searxng_manager`, `engines`, `model_meta`, `maintenance`. |
| `storage/` | SQLite stores over `app.db` (one `*_store.py` per table group), the settings file, backup, template manifests. |
| `middleware/` | `api_debug` (trace capture) and `team_auth` (optional accounts gate). |
| `connectors/` | A stub. Portal connectors are not part of this build; the stub keeps template manifests that mention them parseable. |
| `vw_cli.py` | Service mode: a detached supervisor with auto-restart, pid file and log. |

**Middleware order** (`app.py`): team auth is added first so it is innermost,
then CORS, then trusted hosts, then trace capture last so it is outermost and
tees the exact bytes the client receives without buffering streams.

### 2.3 Model runtime

- **Managed mode** (default): the backend starts, stops and switches one local
  model server. The engine is chosen by artifact: `llama-server` for GGUF,
  `mlx_lm.server` or `mlx_vlm.server` for MLX models on Apple Silicon. The child
  runs detached with a pid file, so it survives a backend reload. Engines can be
  installed and updated at runtime under `VW_HOME/engines` (`runtime/engines.py`).
- **Your own server**: set `VW_UPSTREAM_URL` to any OpenAI-compatible server
  (Ollama, vLLM, LM Studio, a remote base URL). This disables managed mode.
- **Model hub** (`routes/hub.py`): search Hugging Face, estimate fit against
  the machine, download with resume and parallel connections, list on-device
  models, activate.

### 2.4 External providers (bring your own key, or a subscription)

Each agent stores a `modelSource` (`local`, `byok` or `subscription`), a `provider` and a
`model`. `gateway/providers.py:resolve_target()` turns that into a target the
loop passes through; `gateway/egress.py` speaks each provider's wire protocol
(OpenAI-compatible chat, Anthropic Messages, OpenAI Responses) and normalizes
every stream back to the same chunk shape the local upstream produces, so
`loop.py` never knows which it is talking to. Prompt caching, reasoning effort,
per-model thinking formats and discovered context windows are handled there;
`pricing.py` turns usage into cost. Credentials are stored once in
`settings.json` and never returned by the API.

Two subscription paths exist, both opt-in per agent. **ChatGPT**:
`gateway/openai_oauth.py` runs the same PKCE sign-in as the open-source Codex
CLI (public client id, loopback on `localhost:1455`), keeps only the refresh
token, re-derives the access credential on demand, and `egress.py` sends the
request to the Codex Responses API with the account-id header. **Claude**: a
long-lived token from `claude setup-token` pasted as the provider token and sent
with the Claude Code beta headers; there is no sign-in flow. Both are governed by
the vendor's terms for the account.

### 2.5 Web search and browsers

- `browser_search` queries a self-hosted SearXNG that `runtime/searxng_manager.py`
  starts and re-adopts like the model server, or an external instance via
  `VW_SEARXNG_URL`. Per-agent alternatives: Tavily, Brave, DuckDuckGo.
- `browser_fetch` reads a page with a throwaway headless Chromium (Playwright).
- `browser_open`, `browser_click`, `browser_type`, `browser_read`,
  `browser_screenshot` and friends (`agent/browser_control.py`) drive one
  visible browser with a persistent profile, so a person can log in once and the
  agent reuses the session. It can attach to a running Chrome over CDP.

---

## 3. The agent harness

| Pillar | Where | Notes |
|---|---|---|
| **Run entry point** | `agent/runner.py` | `stream_run(agent, messages, session_id=…)` resolves the model target, calls `compose()`, wires MCP servers, applies guardrail knobs and streams loop events. `execute_run()` collects `(text, error, run_id)`; `execute_run_streamed()` adds a delta callback for typewriter surfaces. |
| **Tool registry** | `agent/compose.py` | Builds `{name: {schema, fn}}` per run: built-ins (`tools.py`), skills loader, file tools scoped to the working folder, document tools, memory, schedule, deliver, team delegation, board tools and shell for agents with a role, knowledge base search, wiki tools, skill factory, step plan, browser control, channel send (opt-in), custom HTTP tools, MCP tools. Also assembles the system prompt: persona, time, skills catalog, memory, team, board, directives. |
| **Model** | `gateway/upstream.py`, `gateway/models.py`, `gateway/egress.py` | One streaming chunk shape regardless of backend. Bounded retries with backoff for transient provider failures. |
| **Loop** | `agent/loop.py:run()` | Stream a turn, execute tool calls (in parallel where safe), feed results back, repeat up to the iteration cap. Guards against repeated identical calls and empty replies. Records the full trace. |
| **Context management** | `agent/loop.py`, `agent/crusher.py`, `agent/context_window.py` | Tool results are capped, large ones are compressed to a summary with a `<<vw:HASH …>>` marker while the original is kept in `ccr_originals` for `retrieve_full`. The conversation is trimmed to a budget protecting the system prompt and recent turns; optional token-accurate window tracking with auto-compaction. Hidden big tool sets can sit behind a `search_tools` discovery hop. |
| **Guardrails** | `fs_tools.py`, `shell_tool.py`, `worker.py`, agent settings | Working-folder confinement, shell deny-list with tiers and per-agent extra blocks, iteration and daily caps, run timeouts, the duty switch as kill switch. |
| **Verify** | `agent/verify.py` | A tool-less reviewer pass before a board task is closed; `NEEDS_WORK` sends it back, bounded by attempts. |
| **Worker** | `agent/worker.py` | When an agent is on duty, pulls the next task, runs the harness, verifies, closes or blocks, with idle backoff. Never auto-starts after a restart. |

**Loop events** streamed to callers: `run` (ids), `text`, `text_reset`,
`tool_call`, `tool_result`, `status`, `vision_status`, `done`, `error`.

**Run surfaces** and their session ids, all through the runner:

| Surface | Session id | Entry |
|---|---|---|
| Web chat | `chat:<session>` | `routes/chat.py` |
| Board worker | `worker:<agent>` | `agent/worker.py` |
| Schedule | `schedule:<id>` | `agent/scheduler.py` |
| Trigger | `trigger:<id>` | `agent/triggers.py` |
| Chat channel | `<channel>:<account>:<conversation>` | `channels/manager.py` |
| Inbox flow-back | the originating session | `agent/inbox.py` |

### Delegation and flow-back

`assign_to` files a task on a teammate's board and records the origin
conversation in the task metadata (web chat session or chat-channel
conversation). When that task closes as done, blocked or review, one idempotent
`agent_inbox` row is written for the assigning agent. `InboxManager` drains it:
a chat-channel origin is woken through the channel manager's `inject_turn`,
which joins the conversation's queue like a real message; a web-chat origin is
re-run with the session history and persisted as a handoff bubble. The woken
agent gets `task_status` and `relay_task_result`. Retries are capped at three.

---

## 4. Chat channels (`channels/`)

An adapter implements the `ChannelAdapter` contract for one **account** and only
moves messages: `start`, `stop`, `status`, `send`, `typing`, `mark_read`,
`pair`, `logout`, plus normalized `InboundMessage` objects handed to the manager.
No adapter runs an agent or enforces policy.

`ChannelManager` is the shared spine for every transport:

```
inbound → dedup (chat_events) → policy (dm: open | pairing | allowlist;
group: mention | open | allowlist | off) → resolve agent (binding → account
default → default agent) → per-conversation queue + debounce → slash commands
(/reset /status /help) → history replay with tool-provenance notes →
execute_turn via runner (timeout, typing, "working on it" ack) → strip
reasoning and provenance notes → persist both turns to chat_sessions →
split to the transport's size cap and send → delivery audit + activity entry
```

Transports:

- **WhatsApp linked device** (`whatsapp/linked.py` + `whatsapp/bridge/`): a
  Node sidecar built on Baileys, spawned and supervised by the adapter, speaking
  JSON lines over stdio. QR or pairing-code pairing, media in and out, quoted
  replies, read receipts, automatic restart with backoff. Session keys persist
  under `VW_HOME/channels/<account>/auth`.
- **WhatsApp Cloud API** (`whatsapp/cloud.py`): the official webhook, HMAC
  signature verification, Graph API sends, media upload and download. The one
  transport that needs a public URL.
- **Telegram** (`telegram.py`): Bot API long polling in pure `httpx`.

Accounts, bindings, dedup ids and pairing requests live in
`storage/chat_channel_store.py`; secrets live write-only in `settings.json`.
Proactive sends go through `manager.send()`, which applies a self-send guard and
audits every message; agents reach it through the opt-in `channel_send` tool or
the `chat` delivery kind.

---

## 5. Delivery (`agent/delivery.py`)

`deliver(channel, target, text)` is the single audited outbound path. Kinds:
`in_app` (post into a chat session), `desktop` (macOS notification), `webhook`
(URL must be on an enabled target's allow-list), `chat` (`<account>:<conversation>`
on a connected channel), `email` (SMTP target with a recipient allow-list),
`obsidian` (a markdown note into a vault folder). Targets are always explicit,
never a remembered recipient. Every attempt lands in `deliveries`. Schedules
carry a `deliverTo` list; agents call the `deliver` tool.

---

## 6. Trace capture

`middleware/api_debug.py` is a pure-ASGI middleware that tees a bounded copy of
request and response bytes for model calls, then parses them on a worker thread
after the response finishes: OpenAI and Anthropic JSON, SSE streams reduced to a
final message and tool calls, token usage normalized, cost estimated. It writes
one `debug_runs` row and ordered `debug_entries`. Agent runs record the full
incoming conversation, each tool call and the final answer under the run's
session id, so the Debug screen shows exactly what the model saw. Capture is
best-effort and can be disabled with `VW_DEBUG_CAPTURE=0`.

---

## 7. Data

SQLite with WAL at `VW_HOME/app.db`. Every store opens the same way (row
factory, ISO timestamps, a lock, `camelCase` dicts out).

| Group | Tables |
|---|---|
| Agents and work | `agents`, `tasks`, `task_comments`, `activities`, `agent_memory`, `agent_inbox`, `schedules`, `triggers` |
| Conversations | `chat_sessions`, `chat_messages` |
| Chat channels | `chat_accounts`, `chat_bindings`, `chat_events`, `chat_pairing_requests` |
| Delivery | `channels` (delivery targets), `deliveries` |
| Knowledge | `kb`, `kb_chunk`, `wiki`, `wiki_proposal`, `wiki_repository_job`, `wiki_shell_refusal` |
| Tools | `custom_tools`, `mcp_servers` |
| Traces | `debug_runs`, `debug_entries`, `ccr_originals`, `reasoning_sidecars` |
| Access | `api_tokens`, `users`, `auth_sessions` |

Files, not rows: skills (`VW_HOME/skills/<slug>/SKILL.md` packs), wikis
(`VW_HOME/wikis/<id>/` markdown pages), templates and registries
(`VW_HOME/templates`, `VW_HOME/registries`), channel sessions and received
media (`VW_HOME/channels/<account>/`), downloaded engines and fused models,
backups and the service log. `settings.json` holds app settings and every
write-only secret (provider keys, channel credentials, delivery passwords).

---

## 8. API surface

| Prefix | Purpose |
|---|---|
| `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` | OpenAI-compatible gateway to the active model. |
| `/v1/messages`, `/v1/responses`, `/v1beta/models/{model}:generateContent` | Anthropic, OpenAI Responses and Gemini shapes for coding agents and SDKs. |
| `/api/chat`, `/api/sessions` | Agentic chat (SSE) and saved conversations. |
| `/api/agents/*` | Agents plus their memory, files, tasks, activity, duty, schedules, inbox. |
| `/api/channels/*` | Chat-channel accounts, pairing, bindings, pairing requests, Cloud API webhooks. |
| `/api/delivery-targets`, `/api/deliver`, `/api/deliveries` | Delivery targets, a manual send, the audit log. |
| `/api/triggers/*` | Triggers, including the inbound webhook `hook/{token}`. |
| `/api/providers`, `/api/tokens`, `/api/gateway` | Provider keys, gateway tokens, connect info. |
| `/api/hub`, `/api/runtime`, `/api/engines`, `/api/monitor` | Models, the managed server, engines, live health. |
| `/api/skills`, `/api/kb`, `/api/wiki`, `/api/templates`, `/api/registries`, `/api/mcp`, `/api/tools` | Skills, knowledge, templates and registries, MCP servers, custom tools. |
| `/api/team`, `/api/auth`, `/api/users`, `/api/backup`, `/api/browser`, `/api/analytics`, `/api/debug` | Team view, accounts, backup (export, import, schedule, snapshots), browser control, analytics, traces. |
| `/health` | Liveness. |

Interactive docs are at `/docs`.

---

## 9. Security model

- The backend binds to `127.0.0.1`. CORS allows local origins on any port plus
  `VW_CORS_ORIGINS`; never `*`. The Host header must match localhost names or
  `VW_ALLOWED_HOSTS` (DNS-rebinding guard).
- Solo mode has no login. Team mode (`/api/auth`, `/api/users`, a signed cookie
  checked by `middleware/team_auth.py`) gates every `/api` route except login,
  bootstrap and the webhooks, which carry their own proof: an HMAC signature for
  the Cloud API, an unguessable token for trigger webhooks.
- The model gateway requires a minted `sk-vw-…` token once the first one
  exists; only hashes are stored.
- Secrets are write-only. The API returns presence flags. A logging filter
  scrubs bot tokens, bearer tokens and API keys from log lines, and the HTTP
  client's request logging is quieted.
- Agent guardrails are enforced in code (section 3). Reasoning never reaches an
  external chat. Accounts never message themselves. Web fetches refuse private
  hosts; the static file handler refuses paths outside the build folder.

See `SECURITY.md` for the operator-facing summary and how to report issues.

---

## 10. Background loops and maintenance

Started and stopped by the lifespan in `app.py`, each isolated and best-effort:
the **scheduler** (cron and one-time runs), the **trigger engine** (file watch
and webhooks), the **inbox** drain, the **channel manager** (one adapter per
enabled account), board **workers** (only while on duty) and the
**maintenance** loop. Shutdown is bounded by a deadline so a wedged component
cannot keep the process alive.

Maintenance runs an hour after start and hourly after that: prune traces by age
and row cap, activity rows, delivery audit rows, channel dedup ids, received
channel media, expired compression originals and finished download jobs, then
checkpoint the write-ahead log. Retention is tunable with the `VW_*_RETENTION_*`
variables. The same pass writes the opt-in scheduled backup snapshot
(`storage/backup.py:scheduled_backup_once`) when the configured interval has
elapsed and prunes old scheduled snapshots to the configured count.

---

## 11. Configuration

All state lives under `VW_HOME` (default `~/.virtual-worker`; `./dev.sh` uses
`./data`, or a safe machine-level folder when the repo sits in a macOS-protected
directory). The most used variables:

| Variable | Default | Purpose |
|---|---|---|
| `VW_HOME` | `~/.virtual-worker` | data directory |
| `VW_UPSTREAM_URL` | managed server | use your own OpenAI-compatible server |
| `VW_MANAGED_LLAMA`, `LLAMA_PORT`, `LLAMA_CTX`, `LLAMA_NGL` | `1`, `8080`, `32768`, `99` | managed model server |
| `VW_MANAGED_SEARXNG`, `VW_SEARXNG_URL`, `VW_SEARXNG_HOME` | `1`, –, `~/.virtual-worker/searxng` | web search |
| `VW_BROWSER_*`, `VW_CONTROL_BROWSER_HEADLESS` | auto | which browser the tools drive, profile, CDP attach |
| `VW_CORS_ORIGINS`, `VW_ALLOWED_HOSTS` | – | extra origins and host names |
| `VW_GATEWAY_KEY`, `VW_MODEL_MAP`, `VW_DEFAULT_MODEL` | – | gateway key and model id mapping |
| `VW_DEBUG_CAPTURE`, `VW_DEBUG_RETENTION_DAYS`, `VW_DEBUG_MAX_ROWS` | `1`, `14`, `5000` | trace capture and retention |
| `VW_ACTIVITY_RETENTION_DAYS`, `VW_DELIVERY_RETENTION_DAYS`, `VW_CHAT_EVENT_RETENTION_DAYS`, `VW_CHANNEL_MEDIA_RETENTION_DAYS` | `90`, `90`, `7`, `14` | retention |
| `VW_NODE_BIN`, `VW_WHATSAPP_MAX_MEDIA_BYTES`, `VW_TELEGRAM_MAX_MEDIA_BYTES` | `node`, 25 MB, 20 MB | chat channels |
| `VW_PORT`, `BACKEND_PORT`, `FRONTEND_PORT` | `8888`, `8889`, `5173` | service and dev ports |
| `HF_TOKEN`, `TAVILY_API_KEY`, `BRAVE_API_KEY` | – | gated model repos, optional search providers |

`config.py` is the complete list.

---

## 12. Development, tests, packaging

```bash
./dev.sh                                   # hot reload: Vite :5173, API :8889, model :8081
./vw start | status | logs -f | stop        # service: built UI + API :8888, model :8080
cd backend  && .venv/bin/python -m pytest -q   # backend gate
cd frontend && npm run build                   # frontend gate (tsc + vite)
```

`setup.sh` is idempotent: backend venv with `uv`, frontend packages, llama.cpp,
MLX engines on Apple Silicon, Playwright's Chromium, and the WhatsApp bridge
packages. CI runs both gates plus bridge and shell checks on every push and PR.

Tests are hermetic: stores are isolated by pointing `config.APP_HOME` at a
temporary directory, model calls and adapters are faked, and the WhatsApp bridge
protocol is exercised against a Python stand-in process.

`desktop/` holds an Electron shell and a PyInstaller spec that freeze the
backend and bundle the built UI into a single app; `build.sh` produces it. The
release pipeline is not wired to a public host yet.

---

## 13. Directory layout

```
virtual-worker/
├── dev.sh · setup.sh · vw          # launchers (dev, installer, service)
├── backend/
│   ├── app.py · config.py · vw_cli.py
│   ├── agent/        # runner, compose, loop, tools, worker, scheduler, triggers, inbox, …
│   ├── channels/     # base contract, manager, telegram, whatsapp/ (+ bridge/)
│   ├── gateway/      # upstream, translators, providers, egress, pricing
│   ├── routes/       # one module per /api surface
│   ├── storage/      # SQLite stores, settings, backup, template manifests
│   ├── runtime/      # llama_manager, searxng_manager, engines, maintenance
│   ├── middleware/   # api_debug, team_auth
│   └── tests/
├── frontend/src/
│   ├── App.tsx · index.css · lib/
│   └── features/<name>/<name>-page.tsx
├── skills/           # built-in skill gallery (50 curated packs + meta skills)
├── desktop/          # Electron shell + PyInstaller spec
├── registry-scaffold/# starter layout for a git-backed template registry
├── docs/             # PRD.md, ARCHITECTURE.md, INSTALL.md, keep-awake.md
└── .github/          # CI workflow, issue and PR templates
```
