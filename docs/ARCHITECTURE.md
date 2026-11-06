# Virtual Worker — Architecture

_Last updated: 2026-07-03_

Virtual Worker is a local-first studio for **serving, debugging, and fine-tuning** LLMs,
and for running autonomous "virtual worker" agents (tools, memory, a task
board, and schedules). It is a single React frontend talking to a FastAPI backend,
which acts as an **OpenAI-compatible gateway** in front of any model server
(llama.cpp / vLLM / Ollama / a remote provider). Every call through the gateway
is transparently **captured** into a local SQLite DB and surfaced in an
API-debug trace viewer.

> Temporary home: lives in the `asetin` monorepo at `asetin/ves` with its
> own git history; will be split out as it matures.

---

## 1. High-level diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Browser — http://localhost:5173                                             │
│  React + Vite + TS + Tailwind v4 (navy-on-cream brand theme)                 │
│   • Playground   — streaming chat + model list                               │
│   • Debug        — API-call trace viewer                                      │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │  Vite dev proxy:  /api, /v1  ──▶  127.0.0.1:8888
                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Backend — FastAPI @ :8888                                                    │
│                                                                               │
│   ApiDebugMiddleware  ◀── outermost: tees a bounded copy of req/resp bytes    │
│        │                  (never buffers the client stream)                   │
│        ▼                                                                       │
│   ┌─ /v1/*          inference proxy ──httpx──▶  upstream model server         │
│   │     chat/completions · completions · embeddings · models                  │
│   ├─ /api/debug/*   read API over app.db                                   │
│   └─ /health                                                                   │
│                                                                               │
│   after response → worker thread parses + normalizes → writes app.db       │
└───────────────┬─────────────────────────────────────────┬───────────────────┘
                │ VW_UPSTREAM_URL (default :8080)     │
                ▼                                           ▼
┌───────────────────────────────────┐      ┌──────────────────────────────────┐
│  Upstream model server            │      │  ~/.virtual-worker/app.db (SQLite)  │
│  llama.cpp / vLLM / Ollama / API  │      │  debug_runs + debug_entries (WAL) │
└───────────────────────────────────┘      └──────────────────────────────────┘
```

`dev.sh` starts all three local processes (model server + backend + frontend)
together; see §7.

---

## 2. Components

### 2.1 Frontend (`frontend/`)
- **Stack:** React 18, Vite 6, TypeScript, Tailwind CSS v4. Plain React — no
  data-fetching library; `fetch` via `src/lib/api.ts` (`apiJson`/`apiPost`/`apiFetch`).
- **Shell:** `src/App.tsx` — a collapsible sidebar that routes between feature
  panes (Chat, Agents, Models/Hub, Connect, Debug, Monitoring, Knowledge, Wiki, Team, …).
  No router library; one `features/<name>/<name>-page.tsx` per screen.
- **Chat** (`features/playground/playground-page.tsx`) — runs *as an agent*
  (persona + tools + skills + the agent's chosen model); streams the agentic tool
  loop from `POST /api/chat` (SSE), showing tool activity inline.
- **Agents** (`features/agents/`) — create/edit agents; per-agent detail tabs
  (Overview, Board, Activity, Schedule, Triggers, **Conversations**, Settings).
- **Debug** (`features/debug/debug-page.tsx`) — reads `/api/debug/*`; filter by
  errors / **agent** and search, 10s auto-refresh.
- **Brand:** navy primary on warm cream, Fredoka (display) + Nunito (body), the
  otter mascot. Tailwind v4 `@theme` tokens in `src/index.css` (`bg-primary`,
  `text-ink`, `bg-base`, `border-pressed`, …) — always use the tokens, never hex.
- **Dev proxy:** `vite.config.ts` forwards `/api`, `/v1`, `/v1beta` to `:8888`, so
  the browser only ever talks to one origin.

### 2.2 Backend (`backend/`)
FastAPI app assembled in `app.py`:

| Module | Responsibility |
|---|---|
| `app.py` | App + middleware wiring; mounts routers; startup loops; `/health`. |
| `config.py` | Paths & env: `APP_HOME` (`~/.virtual-worker`), `UPSTREAM_URL`, model map, `db_path()`. |
| `middleware/api_debug.py` | Pure-ASGI capture middleware (the heart of the debug feature). |
| `gateway/` | Upstream client (`upstream.py`) + protocol translators; **external providers**: `providers.py` (catalog + per-agent target resolution), `egress.py` (OpenAI/Anthropic/Codex egress), `{openai,anthropic}_oauth.py` (subscription sign-in). |
| `agent/` | The agent harness — `compose.py`, `loop.py`, tools, and the always-on `worker.py` / `scheduler.py` / `triggers.py` / `verify.py`. |
| `routes/*` | One module per `/api` surface (agents, chat, sessions, tasks, providers, debug, hub, …). |
| `storage/*_store.py` | SQLite stores in `app.db` (agents, tasks, chat_sessions, custom_tools, debug, …). |

**Middleware order (`app.py`):** CORS is added first (inner), `ApiDebugMiddleware`
added last → **outermost**, so it tees the exact bytes the client receives without
blocking streaming.

### 2.3 Model server (external)
Any OpenAI-compatible server at `VW_UPSTREAM_URL` (default
`http://127.0.0.1:8080`). `dev.sh` launches **llama.cpp** (`llama-server`) by
default; Ollama (`:11434`), vLLM, LM Studio, or a remote provider base URL all
work by pointing the env var at them.

### 2.4 Web search (managed SearXNG)
The `browser_search` tool queries a **self-hosted SearXNG** (open source, no API
key). Managed like the model server: `runtime/searxng_manager.py` auto-starts a
local SearXNG (installed once under `~/.virtual-worker/searxng`) on backend startup and
re-adopts it across `--reload`, so search works with no URL to set. `browser_fetch`
is separate — a headless Chromium (Playwright) for reading pages. Point at an
external instance with `VW_SEARXNG_URL`, or per-agent under Agents → Web search
(providers: SearXNG / Tavily / Brave / DuckDuckGo). Health shows on the Monitor page.

---

## 2b. The agent harness (pillars)

When an Virtual Worker agent runs (chat or the autonomous worker) it's driven by a
proper agent harness. The canonical pillars and where each lives:

| Pillar | What it is | In this codebase |
|---|---|---|
| **Tool Registry** | The set of callable tools (name → JSON schema + fn) offered to the model, assembled per run | `agent/compose.py` builds `{name: {schema, fn}}` from built-ins (`tools.py` — incl. `browser_search`/`browser_fetch`), `load_skill` (`skills.py`), the **skill factory** (`skill_factory.py` — `create_skill`/`update_skill`/`list_all_skills`, gated by the `skillFactory` guardrail, off by default), file tools (`fs_tools.py`), memory (`memory.py`), knowledge/RAG (`knowledge.py`), the OKF knowledge wiki (`wiki.py` — `wiki_search`/`wiki_read`/`wiki_write`/`wiki_list`/`wiki_lint`), intern board tools (`intern.py`), `shell` (`shell_tool.py`), and MCP (`mcp_client.py`) |
| **Model** | The LLM the loop calls, abstracted over providers | Local (default) via `gateway/upstream.py` + `gateway/models.py` (`resolve_model`), managed llama.cpp/MLX in `runtime/`. Per-agent **external providers** (opt-in egress): `providers.py:resolve_target(agent)` returns a `(model, target)` pair threaded into `loop.run(provider=…)`; `gateway/egress.py` normalizes every provider (DeepSeek/OpenAI, Anthropic, ChatGPT-Codex) back to the OpenAI chunk shape, so the loop is unchanged |
| **Context Management** | Keeping the model-facing conversation within the context window | `agent/loop.py`: per-tool-result cap (`_cap`) + conversation budget trim (`_fit_context`) protecting the system prompt, the task, and recent turns. Budgets are tunable; the `tightContext` guardrail opts an agent into tighter caps (faster on local). System prompt assembled in `compose.py` (persona + skills + memory + board) |
| **Guardrails** | Automatic bounds on what the agent may do (no human in the loop) | working-folder confinement (`fs_tools.py`), shell deny-list + tiers (`shell_tool.py`), worker caps + kill switch + idle backoff (`agent/worker.py`) |
| **Agent Loop** | Stream a turn → run tool calls → feed results back, until done | `agent/loop.py:run()` (iterate up to `MAX_ITERATIONS`), shared by the chat route and the worker |
| **Verify** | Close out by confirming the work is actually done, not just claimed | `agent/verify.py` (`verify_task` — a tool-less reviewer pass); the worker runs it when a task is marked `done` — VERIFIED keeps it, NEEDS_WORK sends it back to `todo` (bounded by `MAX_VERIFY_ATTEMPTS`) |

The autonomous worker (`agent/worker.py`) is this same harness driven on a
schedule: it pulls a task, runs the loop, **verifies the result**, and closes the
task — see `docs/INTERN_AGENT_PRD.md`. Its on/off-duty switch is `routes/duty.py`
(`POST /api/agents/{id}/duty`); "off" is the kill switch and duty never
auto-starts on launch (decision D2).

**One run entry point.** Every run surface — the chat route, the worker, the
scheduler, the trigger engine, and chat channel — funnels through
`agent/runner.py:stream_run()`, which does the shared setup once (resolve the
model target, `compose()`, wire the agent's MCP servers, drive `loop.run` with
its guardrail knobs) and streams the loop's events; `execute_run()` is the thin
`(text, error, run_id)` collector on top for callers that don't stream.

**Always-on background loops**, started/stopped in `app.py`'s lifespan handler:
the **scheduler** (`agent/scheduler.py`, cron/one-time runs), the **trigger
engine** (`agent/triggers.py`, file-watch + inbound webhooks), the **chat channel**
relay (`agent/channel.py`, below), and a **maintenance** loop
(`runtime/maintenance.py`) that periodically prunes the Debug trace + activity
tables and evicts finished download jobs so `app.db` stays bounded (retention
knobs in `config.py`). Related: `agent/repo_wiki.py` generates a DeepWiki-style
wiki from a repository (surfaced via `routes/wiki.py`).

### Agent inbox & flow-back

When `assign_to` runs from a chat channel or in-app session, it captures the origin in
`task.metadata.replyTo` (including the chat channel `CURRENT_INBOUND` reply/thread
fields). Worker, hand, and scheduled assignments have no origin and never flow
back. When a delegated task closes as `done` or `blocked`, `worker._finalize`
writes one idempotent `agent_inbox` row for the assigning agent and stamps
`metadata.flowBackAt`; the row carries a compact result payload and follows the
existing delegation guards.

`InboxManager` drains pending rows on an approximately two-second loop. It sends
chat channel items through `channel.inject_turn`, which is synthetic and joins the
conversation's FIFO without a busy notice; in-app items use `runner.execute_run`
with the session history and persist `inbox`/`inbox_reply` messages. The woken
agent receives ownership-checked `task_status` and `relay_task_result` tools: it
can inspect the result, relay the full summary verbatim, route it onward, merge
it, or do nothing. Delivery retries are capped at three and failures remain
visible in the local Inbox work tab.

**Inbound chat surfaces.** Beyond the web UI, an agent can be driven from an
external chat app. **chat channel** (PRD-15, `agent/channel.py`) is the first: a
`chat channelManager` background task dials *out* to chat channel over a WebSocket (so a
laptop needs no public host), and each inbound message runs the bound agent
through the same `execute_run` path (`session_id="channel:<conversationId>"`),
replying via a `channel` delivery channel. Credentials are write-only in
`settings.json`; an optional HTTP-webhook mode (`POST /api/channel/callback`, SHA-256
signature) serves operators who do have a public host.

---

## 3. Request & capture flow

For a Playground chat:

1. Browser `POST /v1/chat/completions` → Vite proxy → backend `:8888`.
2. `ApiDebugMiddleware.__call__` matches the path suffix (`/chat/completions`)
   and wraps `receive`/`send` to **tee** a bounded copy (≤2 MB) of request and
   response bytes — every ASGI event is still forwarded to the client unchanged,
   so streaming is never buffered or delayed.
3. `routes/inference.py` rebuilds the request with `httpx` and forwards it to
   `UPSTREAM_URL`. Streaming responses are passed through byte-for-byte
   (`StreamingResponse` over `aiter_raw`); non-stream responses are relayed whole.
   Upstream errors return a `502` with an OpenAI-style error body.
4. When the response finishes, the middleware schedules
   `_capture_and_persist(...)` on a **worker thread** (off the hot path).
5. That worker parses the captured bytes — handling OpenAI JSON, Anthropic JSON,
   and SSE streams — reduces streamed deltas into a final message + tool calls,
   normalizes token usage, and estimates cost (`_PRICE_PER_M`; local = $0).
6. It writes one `debug_runs` row + ordered `debug_entries` (system / user /
   assistant / tool_call / llm_fallback) to `app.db`.
7. The Debug tab reads it back via `/api/debug/*`.

**Capture is best-effort:** any parsing/persistence error is swallowed and
logged at debug level — a debugging feature can never break inference. Toggle off
with `VW_DEBUG_CAPTURE=0`.

**Session grouping:** runs are grouped by session id, taken from the
`x-vw-session` or `x-thread-id` request header, else `"adhoc"`.

---

## 4. Data model (`app.db`, SQLite + WAL)

- **`debug_runs`** — one row per captured API call: `run_id`, `session_id`,
  `agent_id` (which agent produced it → per-agent Debug filtering), start/end
  time, `duration_ms`, `method`, `path`, `endpoint`, `provider`, `model`,
  `status_code`, `is_error`, `stream`, token counts
  (`input/output/cache_read/cache_write/total`), `cost`, `tool_calls`,
  `first_user_text` / `last_assistant_text` previews, `request_json`,
  `response_text`.
- **`debug_entries`** — ordered (`seq`) child rows per run: `type`, `text`,
  `thinking`, `tool_name`, `tool_args`, `usage`, etc. — the readable trace.
- Indexes: `(session_id, start_time)` on runs, `(run_id, seq)` on entries.
- Read API aggregates these into `summary`, `sessions`, and per-session `runs`.

`app.db` also holds the app's domain tables (one `storage/*_store.py` each),
all opened the same way (WAL, `row_factory=Row`, `camelCase` out): `agents`
(persona + `model`/`model_source`/`provider` + guardrails + `web_search` + attached
KB/`wikis` ids), `tasks` (title + description + **`criteria`** / definition of done),
`chat_sessions` + `chat_messages` (per-agent conversations, keyed by `agent_id`),
`custom_tools`, plus skills/memory/KB/schedule/trigger stores. App settings +
provider credentials live in `settings.json` (not the DB).

**Skills** (`storage/skill_store.py`) are on-disk `SKILL.md` packs under
`~/.virtual-worker/skills/<slug>/` (not DB rows), added by operator upload **or** authored by
an agent at runtime via the skill factory (`create_skill`/`update_skill`, gated by
the per-agent `skillFactory` guardrail) — either way a spec-compliant folder the
`load_skill` tools read on demand. See PRD-14.

**Knowledge Wikis** (`storage/wiki_store.py`, PRD-13) are the exception to
DB-only storage: each wiki is a directory of **OKF markdown files** — the source
of truth — under `~/.virtual-worker/wikis/<id>/` (concept pages + reserved `index.md`/`log.md`),
with only a light registry row (`wiki` table) in `app.db`. Agents read/write
them via the `wiki_*` tools; search + lint are a pure-Python scan over the files
(no derived index to desync), so a wiki stays portable — a plain folder you can
zip, git, or open in any editor.

DB location: `VW_HOME` (default `~/.virtual-worker`) → `app.db`; wikis → `~/.virtual-worker/wikis/`.

---

## 5. API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/chat/completions` | Proxied chat (stream or not). |
| `POST` | `/v1/completions` | Proxied legacy completions. |
| `POST` | `/v1/embeddings` | Proxied embeddings. |
| `GET`  | `/v1/models` | Upstream model list. |
| `POST` | `/api/chat` | Agentic chat (SSE) — runs as an agent through the tool loop. |
| `GET`  | `/api/debug/sessions[/{id}]` | Trace summary / session list / per-session runs. |
| `GET/PUT` | `/api/providers[/{id}]` | External-provider catalog + BYOK/subscription creds (write-only); `…/{provider}/oauth/*` for ChatGPT/Claude sign-in. |
| `GET`  | `/health` | Liveness. |

The `/api` surface is broad — one router per domain (`agents`, `sessions`,
`tasks`, `providers`, `skills`, `mcp`, `tools`, `hub`, `runtime`, `monitor`, `kb`,
`wiki`, …), all registered in `app.py`. The inbound gateway also speaks Anthropic
(`/v1/messages`), OpenAI-Responses (`/v1/responses`), and Gemini (`/v1beta/…`)
via translators in `gateway/`.

> Solo mode is unauthenticated (localhost). Optional team mode + gateway tokens
> (`sk-vw-…`) gate access; set one before exposing beyond the local machine.

---

## 6. Configuration (env)

| Var | Default | Used by |
|---|---|---|
| `VW_UPSTREAM_URL` | `http://127.0.0.1:8080` | inference proxy target |
| `VW_HOME` | `~/.virtual-worker` | data dir (`app.db`) |
| `VW_DEBUG_CAPTURE` | `1` | set `0` to disable trace capture |
| `LLAMA_MODEL` / `LLAMA_PORT` / `LLAMA_CTX` / `LLAMA_NGL` | gemma gguf / 8080 / 8192 / 99 | `dev.sh` llama-server |
| `BACKEND_PORT` / `FRONTEND_PORT` | 8888 / 5173 | `dev.sh` |

---

## 7. Local development

```bash
./dev.sh          # llama-server (:8080) + FastAPI (:8888) + Vite (:5173); Ctrl+C stops all
```

- Backend runs in a **uv-managed venv** at `backend/.venv` (the system Homebrew
  Python's `venv`/`pip` is broken on this Mac — uv ships its own interpreter).
- `dev.sh` preflights llama-server, the model file, the venv, and `node_modules`.
- Use your own server instead of llama: `VW_UPSTREAM_URL=http://127.0.0.1:11434 ./dev.sh`.
- **Port note:** the monorepo's `asetin/frontend` also defaults to `5173` — run
  `FRONTEND_PORT=5174 ./dev.sh` if both are up.

Tests: `cd backend && .venv/bin/python -m pytest -q`.

---

## 8. Directory layout

```
ves/
├── dev.sh                      # one-command local launcher
├── README.md
├── docs/
│   ├── ARCHITECTURE.md         # this file
│   └── llm/                    # internal LLM learning notes (gitignored, never pushed)
├── backend/
│   ├── app.py  config.py
│   ├── middleware/        # api_debug + team_auth
│   ├── gateway/          # upstream + egress + providers + translators
│   ├── agent/            # compose, loop, tools, worker/scheduler/triggers/verify
│   ├── routes/           # one module per /api surface
│   ├── storage/          # *_store.py over app.db
│   ├── runtime/          # managed model server + SearXNG, fine-tune/eval runners
│   ├── tests/
│   └── requirements.txt   # fastapi, uvicorn, httpx, pydantic, pytest
└── frontend/
    ├── index.html  vite.config.ts
    └── src/
        ├── App.tsx  main.tsx  index.css   # @theme brand tokens
        ├── lib/api.ts
        └── features/<name>/<name>-page.tsx
```
