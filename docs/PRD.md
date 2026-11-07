# PRD — Virtual Worker

_Status: Draft v0.3 · Last updated: 2026-11-07 · License: Apache-2.0 · Audience: open-source contributors_

> **Virtual Worker** is an open-source, local-first platform for running autonomous AI
> workers on your own machine. A worker is more than a chatbot: it has a job title, a
> persona, tools, skills, memory, a task board, schedules, and guardrails. It runs on a
> local model by default (or a provider you bring your own key for), and it is reachable
> from the chat apps people already use — **WhatsApp first**, then Telegram, Discord and
> Slack — as well as a web UI. Nothing leaves the machine unless you explicitly connect it.

---

## 1. Summary

Virtual Worker turns a laptop or a small server into a place where AI workers live and
work. The owner hires a worker (from a template or from scratch), gives it a role and a
working folder, attaches skills and tools, and then either chats with it, assigns tasks to
its board, schedules recurring jobs, or lets it respond to messages on a connected chat
channel. Everything the worker does is logged to an activity feed and a full trace of every
model call is kept locally for debugging.

The product is built around five ideas:

1. **Local-first and private.** State lives in one directory on disk (SQLite + files).
   Models run locally by default. Every network egress is explicit, per-worker, and opt-in.
2. **A real agent harness, not a prompt.** Tool registry, agent loop, context management,
   automatic guardrails, and a verification pass are first-class and shared by every run
   surface (chat, board worker, scheduler, triggers, chat channels).
3. **Bounded autonomy.** Workers act on their own inside limits enforced in code
   (working-folder confinement, shell deny-list, caps, kill switch) rather than through a
   human approving each step.
4. **Reachable where people already are.** Chat channels are transports over one run and
   reply spine. WhatsApp is the first channel because it needs no public host and it is
   where small businesses and individuals actually talk.
5. **Open and extensible.** Channels, tools, model providers, skills and templates are
   plugin points with stable interfaces, so the community can add integrations without
   touching the core.

## 2. Problem and motivation

- **Hosted agent products are a trust problem.** Businesses and individuals want AI help
  with real work (files, inboxes, customer chats, internal systems) but do not want that
  data streamed to a third party. Running on your own hardware is the simplest answer.
- **Local model tooling stops at chat.** Local inference servers and chat UIs exist, but
  the layer that turns a model into a *worker* (board, memory, schedules, tools, logging,
  safety) is missing or proprietary.
- **Chat is the natural interface, and the laptop is the natural host.** Most people would
  rather message a worker on WhatsApp than open a dashboard. But a laptop has no public
  address, so classic webhook integrations fail. Channel transports must dial *out*.
- **Good setups are not shareable.** A well-tuned support worker or research worker lives
  in one person's config. A file-based template and skill format makes workers portable
  and lets a community library form.

## 3. Goals and non-goals

**Goals**

- G1. **One-command local install** on macOS and Linux (Windows via WSL first), with a
  managed local model runtime and a built-in model hub.
- G2. **Workers** with persona, role, model, tools, skills, memory, working folder,
  guardrails, task board, activity feed, schedules and triggers.
- G3. **One agent harness** shared by all run surfaces, with automatic guardrails and a
  verification pass.
- G4. **Chat channels** as a plugin interface, with **WhatsApp** shipped first (pairing by
  QR code, no public host), followed by Telegram, Discord and Slack.
- G5. **Outbound delivery** of results and proactive messages (chat channel, webhook,
  desktop notification, email) with a full audit trail.
- G6. **Observability**: every model call traced locally with tokens, cost, latency, tool
  calls and errors, grouped by session and worker.
- G7. **Portable formats**: skills as folders, workers as template bundles, backups as one
  archive. Export, import and share as plain files.
- G8. **Bring your own model or key**: local GGUF/MLX models by default; any
  OpenAI-compatible server; per-worker BYOK providers (OpenAI, Anthropic, Google,
  DeepSeek, OpenRouter, custom gateways).
- G9. **Open-source hygiene**: permissive license, no telemetry, documented plugin
  interfaces, a contribution guide, CI with tests on every PR. _CI, templates and
  SECURITY.md added 2026-11-04._

**Non-goals (v1)**

- N1. Multi-tenant cloud hosting or a hosted marketplace with accounts and payments.
- N2. Fine-tuning and model evaluation suites (designed for, built later; see §13).
- N3. Rich interactive UI inside chat channels (buttons, forms, cards). v1 is text plus
  media.
- N4. A per-action human approval queue. Safety comes from guardrails, not gates.
- N5. Voice and real-time audio.
- N6. Company-specific connectors. Connectors ship as generic, configurable plugins only.

## 4. Users and personas

- **Solo operator.** A freelancer or founder who wants an assistant that triages mail,
  researches, drafts, and keeps a to-do board, and who wants to message it from their
  phone.
- **Small business owner.** Runs customer conversations on WhatsApp and wants a support
  worker that answers from their own documents, hands off to a human when needed, and never
  sends customer data to a third party.
- **Developer or builder.** Wants a hackable agent runtime with clean plugin interfaces,
  an OpenAI-compatible gateway for coding agents, and traces to debug behavior.
- **Small team.** Shares a few workers on one machine, with a shared task board and a
  group chat where teammates mention the worker.
- **Security-conscious admin.** Will not open inbound ports or run tunnels. Wants
  credentials on-device, allow-lists, an audit trail, and a kill switch.

## 5. User stories

**Hiring and configuring**

- As an owner, I install Virtual Worker, download a model that fits my machine, and deploy
  a "Personal Assistant" from the template gallery in under fifteen minutes.
- As an owner, I create a worker with a role, a persona, a working folder, and a set of
  attached skills, and I choose whether it runs on a local model or a provider key.
- As a builder, I export a tuned worker as a template bundle and import it on another
  machine.

**Working**

- As an owner, I chat with a worker in the web UI and watch its tool calls inline.
- As an owner, I put tasks on a worker's board, set it on duty, and watch the activity
  feed while it works them one at a time.
- As an owner, I schedule a worker to produce a daily digest at 08:00 and deliver it to my
  WhatsApp.
- As an owner, I set a trigger so that a new file in a folder starts a worker run.

**Chat channels**

- As an owner, I pair a WhatsApp number by scanning a QR code, bind it to a worker, and
  message that worker from my phone. The reply arrives in the same chat with the worker's
  skills and memory applied.
- As a business owner, I set the DM policy to allow-list so only known numbers can drive
  the worker, and the group policy to mention-only.
- As a team lead, I mention the worker in a group and it replies in-thread, keeping context
  per group.
- As an admin, I flip the channel off and the connection closes immediately.

**Trust and debugging**

- As a developer, I open the trace for any run and see the exact prompts, tool calls,
  token usage and cost.
- As an admin, I review the delivery audit log to see every message the worker sent out.
- As an owner, I take a one-click backup of all workers, memory, skills and settings.

## 6. Product principles

1. **Local-first is a hard constraint.** Any egress (chat channel, provider, web search,
   webhook, remote MCP) is explicit, visible, and opt-in per worker.
2. **Guardrails, not gates.** Safety is enforced in code (confinement, deny-list, caps,
   kill switch), never only in a prompt.
3. **Everything is logged.** If a worker did it, it is in the activity feed and the trace.
4. **One run path.** Chat, board, scheduler, triggers and channels all funnel through one
   runner so behavior and guardrails are identical.
5. **Files over databases for portable things.** Skills, templates, wikis and backups are
   folders and archives anyone can inspect, zip, or put in git.
6. **Dependency-light core, plugin-rich edges.** The core stays small; channels, tools and
   providers are plugins with stable interfaces.
7. **Customer-facing channels never leak reasoning.** Thinking is stripped before anything
   reaches an external chat.

## 7. Concepts and glossary

| Concept | Meaning |
|---|---|
| **Worker** | An AI agent with a role, persona, model, tools, skills, memory, working folder, guardrails, board and schedules. |
| **Run** | One execution of the agent loop for a worker, started by chat, the board worker, a schedule, a trigger, or a channel message. |
| **Harness** | The shared engine: tool registry, model adapter, context management, guardrails, loop, verify. |
| **Skill** | A folder with a `SKILL.md` (frontmatter + instructions) and optional `references/`, `assets/`, `scripts/`. Loaded on demand. |
| **Tool** | A callable the model can invoke: built-in, custom (HTTP/script), MCP, or channel-provided. |
| **Board** | A worker's Kanban of tasks: `backlog → todo → in_progress → review/blocked → done/cancelled`. |
| **Duty** | The on/off switch for the autonomous board worker. Off is the kill switch. |
| **Channel** | A chat transport plugin (WhatsApp, Telegram, Discord, Slack, web). Provides inbound events and an outbound send. |
| **Binding** | Which worker handles a given channel account or conversation. |
| **Delivery** | An audited outbound message to a channel, webhook, notification or email. |
| **Template** | A portable bundle describing a deployable worker, with typed variables. |
| **Trace** | The captured record of one model call: request, response, tool calls, usage, cost. |

## 8. Functional requirements

### 8.1 Model runtime and hub

- FR-M1. Managed local inference: the backend starts, stops and switches a local model
  server. Engines chosen by artifact: llama.cpp for GGUF; MLX for Apple Silicon models.
  One active local model at a time.
- FR-M2. Any OpenAI-compatible upstream (Ollama, vLLM, LM Studio, a remote gateway) can
  replace the managed server via one setting.
- FR-M3. Model hub: search a public model registry, show which files fit the machine
  (RAM/VRAM estimate), download with resume and cancel, list on-device models, activate.
- FR-M4. Per-worker model source: `local`, or a named provider with a BYOK key. Provider
  credentials are stored once, write-only, and never returned to the UI or the model.
- FR-M5. Provider adapters normalize every provider to one streaming chunk shape so the
  loop is provider-agnostic. Retry with backoff and rate-limit handling.
- FR-M6. Optional outbound gateway: expose the active model on OpenAI-, Anthropic- and
  Gemini-shaped endpoints so coding agents and SDKs can use local models. Protected by
  minted API tokens once the first token exists.

### 8.2 Workers

- FR-W1. CRUD for workers: name, role, persona, model source, working folder, enabled
  tools, attached skills, MCP servers, guardrail settings, web search provider.
- FR-W2. Memory: long-term notes the worker reads on every turn and can update via tools;
  owner feedback is written to memory.
- FR-W3. Working folder confinement: file tools refuse paths outside the folder; shell
  runs with the folder as cwd.
- FR-W4. Guardrails: shell deny-list (destructive, privileged, exfil, publish), per-task
  and daily iteration caps, tool-result size caps, run timeouts, and a kill switch.
- FR-W5. Persistent chat sessions per worker; auto-rollover when the context is exhausted.

### 8.3 Agent harness

- FR-H1. Tool registry assembled per run from built-ins, skills loader, file tools, memory,
  board tools, shell, custom tools, MCP servers, and channel tools.
- FR-H2. Agent loop: stream a turn, execute tool calls, feed results back, repeat to a
  bounded iteration count. Repeat-call and empty-reply guards.
- FR-H3. Context management: cap each tool result, trim conversation to a budget while
  protecting the system prompt, the task and recent turns. Optional token-accurate window
  tracking.
- FR-H4. Verify: a tool-less reviewer pass that checks a finished task against its
  definition of done before it is closed. `NEEDS_WORK` sends it back, bounded by attempts.
- FR-H5. Built-in tools (v1): web search (self-hosted SearXNG or an API key), web fetch
  with an SSRF guard, read/write/edit file, shell (tiered), memory read/write, load skill
  and load skill resource, document read (PDF, docx, xlsx, pptx, csv), board tools,
  schedule tools, deliver, and channel send.
  _Document read implemented 2026-11-05 (`agent/doc_tools.py`)._
- FR-H6. MCP client: attach stdio or HTTP MCP servers per worker with a tool allow-list.
- FR-H7. Skills: progressive disclosure. Catalog line in the system prompt, full body on
  `load_skill`, bundled files on `load_skill_resource`. Upload as folder or zip.

### 8.4 Board, activity, autonomous worker

- FR-B1. Task CRUD per worker with priority, status machine, ordering, comments, a
  definition-of-done field and a result summary.
- FR-B2. Append-only activity feed per worker: task events, tool calls, file changes,
  notes, errors, standups. Queryable by worker, task and day. Links to the trace.
- FR-B3. Board worker: when a worker is on duty it pulls the highest-priority `todo`,
  runs the harness, verifies, closes or blocks, with idle backoff. Never auto-starts on
  launch.
- FR-B4. Delegation: a worker can assign a task to another worker's board; when the task
  closes, the result flows back to the assigning worker in the originating conversation.
  _Implemented 2026-11-07 (`agent/inbox.py`; chat-channel origins flow back through the channel manager)._

### 8.5 Schedules and triggers

- FR-S1. Cron and one-time schedules per worker with a prompt, a delivery target, and run
  history.
- FR-S2. Triggers: file-watch on a folder, inbound webhook with a shared secret, and
  channel events. Each fires a run with the event payload.
- FR-S3. All background loops are cancellable tasks started with the app and are
  best-effort: they log errors and never crash.

### 8.6 Chat channels (plugin interface)

- FR-C1. A `Channel` interface every transport implements: `start/stop`, `status`,
  `on_inbound(event)`, `send(target, message)`, `capabilities` (media, typing, groups,
  threads), and a settings schema the UI renders.
- FR-C2. Inbound events normalize to one shape: channel, account, conversation id, kind
  (dm/group), sender id and display name, text, attachments, reply-to, event id, timestamp.
- FR-C3. Routing: bindings map `(channel, account)` and optionally `(conversation)` to a
  worker; unbound conversations use the account's default worker.
- FR-C4. Sessions: one chat session per conversation (`<channel>:<conversation id>`), so
  follow-ups keep context. Per-conversation FIFO so turns never interleave.
- FR-C5. Policies per account: DM policy `open | pairing | allowlist`; group policy
  `open | mention | allowlist`; allow-list of sender ids; debounce window to merge rapid
  consecutive messages.
- FR-C6. Dedup by event id so reconnect redeliveries never double-run.
- FR-C7. Reasoning is stripped on every external channel. Web UI may show it.
- FR-C8. Long runs send a best-effort acknowledgement or typing indicator; a hard per-turn
  timeout cancels wedged runs and replies with a friendly error.
- FR-C9. Media: receive images and documents into the worker's working folder (size cap,
  retention pruning); send text, images and files where the channel supports it.
- FR-C10. Outbound tools: `channel_send(target, text)` for proactive messages, bounded by
  an opt-in per worker and recorded as a delivery. A self-send guard prevents loops.
- FR-C11. Every turn produces a trace and an activity entry deep-linked to the run.
- FR-C12. Credentials and session state for each channel are stored on-device, write-only
  from the UI, redacted from logs and traces.

### 8.7 WhatsApp channel (first channel)

- FR-WA1. **Transport A (default): linked device.** Pair a phone number through the
  WhatsApp multi-device web protocol by scanning a QR code shown in the UI. The adapter
  dials out and holds the connection. No public host is needed. Session keys persist on
  disk under the data directory and survive restarts.
- FR-WA2. **Transport B (optional): Cloud API.** For operators with a WhatsApp Business
  account and a public URL, support the official Cloud API webhook with signature
  verification and the verification challenge. Same downstream path.
- FR-WA3. One or more accounts per instance, each with its own binding, policies and
  status line (`Connected`, `Pairing`, `Reconnecting`, `Logged out`, `Off`).
- FR-WA4. Reconnect with exponential backoff and jitter; a logged-out state requires
  re-pairing and is surfaced clearly.
- FR-WA5. Supported inbound: text, image with caption, document, reply quotes, group
  mentions. Supported outbound: text, image, document.
- FR-WA6. Pairing policy: in `pairing` mode an unknown sender receives a one-time code
  prompt; the owner approves from the UI, which adds the sender to the allow-list.
- FR-WA7. The UI states plainly that the linked-device transport uses an unofficial
  client library and that the operator accepts the platform's terms. The Cloud API is the
  recommended path for business accounts at scale.

### 8.8 Outbound delivery

- FR-D1. Delivery channel kinds: chat channel account, webhook, desktop notification,
  email (SMTP). Each is a configured, enabled record with non-secret config and write-only
  secrets.
  _All five kinds implemented 2026-11-05 (email: SMTP target with recipient allow-list)._
- FR-D2. Schedules, triggers and the board worker can target a delivery; a worker can also
  call `deliver` mid-run when allowed.
- FR-D3. Every send is recorded with target, status, error and the run that produced it.

### 8.9 Knowledge

- FR-K1. Knowledge bases: ingest local files (markdown, text, PDF, docx) into chunks with a
  local embedding model; vectors in SQLite; cited retrieval via a `kb_search` tool.
- FR-K2. Knowledge wikis: a folder of markdown pages the worker can search, read and write,
  portable as a plain directory.
  _Implemented 2026-11-07 (`agent/wiki.py`, `storage/wiki_store.py`)._

### 8.10 Templates

- FR-T1. Template bundle: a manifest (persona, role, recommended model, tools, skills,
  guardrails, MCP requirements, starter tasks and schedules) plus an assets folder.
- FR-T2. Typed variables with prompts, defaults and validation; deploy is a guided form.
- FR-T3. Built-in gallery: Personal Assistant, Customer Support Rep, Research Analyst,
  Content Writer, Data Clerk, Code Reviewer.
- FR-T4. Export any worker as a template; import a bundle or a folder; dependency check on
  deploy (missing model, skill, MCP server).
- FR-T5. Optional git-backed template registries for teams: versioned templates, provenance
  stamped on deployed workers, one-click update with a diff.
  _Implemented 2026-11-07 (`routes/registries.py`, `storage/registry_store.py`, `registry-scaffold/`)._

### 8.11 Observability

- FR-O1. Capture middleware tees every model call (local and provider) into the trace
  store without buffering client streams. Best-effort: capture failure never breaks a run.
- FR-O2. Trace viewer: filter by worker, session, errors; search; per-entry view of
  system/user/assistant/tool-call/usage.
- FR-O3. Analytics: per-worker tokens, cost, latency, tool-call counts, error rates over
  time; optional budgets with alerts.
- FR-O4. Retention: bounded trace and activity tables with a maintenance loop.
  _Implemented 2026-11-04 (`runtime/maintenance.py`, env-tunable retention)._

### 8.12 Trust, backup, packaging

- FR-X1. Solo mode is localhost-only with no auth. Optional team mode adds accounts, roles
  and audit on a shared host; it is the one trust-boundary change and ships behind a flag.
  _Hardening pass done 2026-11-07: role floor in middleware, audit log, login rate limit, secure cookie behind TLS._
- FR-X2. Security baseline: strict CORS, auth gates on every mutating API in team mode,
  SSRF guard on fetch, path traversal checks, secrets redacted everywhere.
  _Implemented 2026-11-04: local-only CORS, Host allow-list, log redaction, webhook
  exemptions from the cookie gate, `SECURITY.md`. Team-mode hardening pass still open._
- FR-X3. Backup and restore: one archive of the data directory (workers, memory, skills,
  templates, settings, channel sessions), scheduled local backups, machine migration.
- FR-X4. Packaging: a dev launcher, a service mode (detached, auto-restart, built UI), and
  a desktop app build (frozen backend plus UI) as a later milestone.
  _Desktop build scaffold present under `desktop/` (2026-11-07); release pipeline not yet wired._

## 9. UX and surfaces

**Web UI** (sidebar app, light and dark):

- **Home**: active model, worker statuses, recent activity, channel connection health.
- **Workers**: list and detail with tabs Overview, Chat, Board, Activity, Schedules,
  Triggers, Conversations, Settings (model, tools, skills, MCP, guardrails).
- **Models**: discover, download, on-device, activate; provider keys.
- **Channels**: one card per account (WhatsApp, Telegram, Discord, Slack): pairing/QR or
  token entry, status, policies, bindings, allow-list, on/off.
- **Skills**: library of installed packs, upload, inspect.
- **Templates**: gallery, deploy form, export, registries.
- **Knowledge**: knowledge bases and wikis.
- **Deliveries**: audit log of everything sent out.
- **Traces**: the call-by-call debug viewer.
- **Settings**: data directory, upstream, search provider, retention, backup, team mode.

**Chat channel UX**: concise replies, markdown reduced to what the channel renders, an
acknowledgement on long runs, a short friendly error on failure, and simple slash commands
(`/reset`, `/status`, `/help`) gated by an allow-list.

**CLI**: `vw` for install, start/stop/status/logs, backup, template import/export, and a
headless `vw run <worker> "<prompt>"`.

## 10. Data model (SQLite with WAL, plus files)

Data directory: `~/.virtual-worker` by default (`VW_HOME`). One database `app.db`, a
settings file `settings.json` (secrets write-only), and folders `skills/`, `templates/`,
`wikis/`, `kb/`, `channels/<channel>/<account>/` (session state), `backups/`, `logs/`.

| Table | Purpose |
|---|---|
| `workers` | persona, role, model source/provider/model, guardrails, tool flags, attached skills and KBs |
| `tasks`, `task_comments` | board items, criteria, status, result summary |
| `activity` | append-only feed per worker |
| `chat_sessions`, `chat_messages` | per-worker conversations, including channel sessions |
| `schedules`, `schedule_runs` | cron and one-time jobs |
| `triggers`, `trigger_events` | file-watch and webhook triggers |
| `channel_accounts` | one row per connected channel account: kind, policies, default worker, status |
| `channel_bindings` | `(channel, account, conversation?) → worker` |
| `channel_events` | dedup by event id, prunable |
| `deliveries`, `delivery_targets` | outbound audit and configured targets |
| `custom_tools`, `mcp_servers` | user-defined tools and MCP attachments |
| `memory` | long-term notes per worker |
| `kb`, `kb_chunks` | knowledge bases and embeddings |
| `templates`, `template_refs` | installed templates and provenance on deployed workers |
| `traces`, `trace_entries` | captured model calls and ordered entries |
| `api_tokens` | hashed gateway tokens |

## 11. API surface (HTTP, local)

- `/api/workers[/{id}]`, `/api/workers/{id}/duty`, `/chat` (SSE), `/board`, `/activity`,
  `/schedules`, `/triggers`, `/memory`, `/sessions`
- `/api/models/*` (hub, runtime, providers), `/api/tokens`
- `/api/channels[/{kind}/{account}]`, `/pair` (QR stream), `/bindings`, `/test`,
  `/webhook/{kind}/{account}` (only for webhook transports)
- `/api/deliveries`, `/api/delivery-targets`
- `/api/skills`, `/api/templates`, `/api/registries`, `/api/kb`, `/api/wikis`
- `/api/traces/*`, `/api/analytics/*`, `/api/backup`, `/api/settings`, `/health`
- Gateway (optional): `/v1/chat/completions`, `/v1/messages`, `/v1/responses`,
  `/v1beta/models/{model}:generateContent`

## 12. Architecture and technical approach

```
web UI / CLI / coding agents / chat channels
        │
        ▼
backend (single process)
  ├─ capture middleware ─────────────▶ traces (app.db)
  ├─ gateway translators (optional protocol shapes)
  ├─ routes/*  (the /api surface)
  ├─ runner: resolve model → compose tools → loop → verify
  │     ├─ harness: registry · loop · context · guardrails · verify
  │     └─ tools: built-ins · skills · fs · shell · memory · board · MCP · custom · channel
  ├─ background loops: scheduler · triggers · board worker · channel managers · inbox · maintenance
  ├─ channels/: whatsapp · telegram · discord · slack  (plugin interface)
  ├─ providers/: local upstream · openai · anthropic · google · openrouter · custom
  └─ storage/: SQLite stores + settings + files
        │
        ▼
model server (managed llama.cpp / MLX, or any OpenAI-compatible upstream, or a provider)
```

**Proposed stack** (to confirm in the first ADR): Python backend (FastAPI, httpx,
stdlib-first), React + TypeScript + Tailwind frontend, SQLite with WAL, Playwright for
browser tools, llama.cpp and MLX as managed engines. The WhatsApp linked-device transport
needs a multi-device protocol implementation; options are a Python binding to a Go
implementation, or a small Node sidecar process managed by the backend. The channel
interface is sidecar-agnostic so this choice stays local to the adapter.

**Plugin interfaces** (documented, versioned):

- `Channel`: start, stop, status, inbound events, send, capabilities, settings schema.
- `Provider`: stream a chat completion in the common chunk shape; credential schema.
- `Tool`: name, description, JSON schema, async execute; discovered from a package entry
  point or a custom-tool record.
- `Skill`: the `SKILL.md` folder spec (compatible with the agentskills.io format).
- `Template`: the manifest schema with versioning and typed variables.

**Run path invariant**: every surface calls `runner.stream_run(worker, prompt, session_id,
context)`; nothing else composes tools or calls the model.

**Safety invariants**: no inbound listener is required for any default channel; secrets are
write-only and redacted; file tools and shell are confined; reasoning never reaches an
external channel; capture and background loops are best-effort and never crash the app.

## 13. Milestones

| Milestone | Scope | Exit criteria |
|---|---|---|
| **M0 Foundation** | Repo, license, CI, launcher, data dir, settings, SQLite stores, capture middleware, managed local model, model hub, web UI shell, trace viewer | Fresh clone to a streaming local chat in one command; traces visible |
| **M1 Workers and harness** | Worker CRUD, persona, memory, skills, built-in tools, MCP, guardrails, chat sessions, shell tiers, verify | A worker completes a multi-tool task inside its folder with a trace |
| **M2 Board, schedules, delivery** | Board, activity, duty worker, cron and one-time schedules, file and webhook triggers, delivery targets (webhook, notification, email) and audit | A worker on duty works a board and delivers a result on schedule |
| **M3 WhatsApp channel** — **implemented 2026-11-04** | Channel plugin interface, WhatsApp linked-device transport with QR pairing, policies, bindings, per-conversation sessions, media, outbound send tool, Cloud API transport | Message a worker from a phone; reply in the same chat; allow-list enforced; no public host |
| **M4 Templates and knowledge** | Template format, gallery, deploy, export/import, knowledge bases, wikis, backup/restore | Deploy a Customer Support Rep that answers from uploaded docs in one sitting |
| **M5 More channels and teams** — Telegram 2026-11-04; providers, gateway, flow-back, custom tools, Discord and Slack 2026-11-07 | Telegram, Discord, Slack adapters; delegation and inbox flow-back; custom tool builder; analytics; BYOK providers and gateway | Two workers collaborate on a delegated task from a group chat |
| **M6 Packaging and trust** — service mode 2026-11-04, login service + `vw run` + team-mode hardening 2026-11-07; desktop build scaffold present | Service mode, desktop build, team mode with accounts and audit, security hardening pass | Runs unattended for a week; passes the security checklist |
| **Later** | Fine-tuning from feedback, evaluation suites, voice, browser control and flow recorder, case management for support teams, hosted template index | |

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Unofficial WhatsApp client libraries can break or lead to account restrictions | Offer the official Cloud API transport; isolate the protocol library behind the channel interface; clear in-product disclosure; rate limits and self-send guard |
| Small local models are weak at tool use | Templates recommend tested models; tool-call enforcement nudges; context compression; BYOK as an opt-in upgrade per worker |
| Autonomy causes damage | Confinement, deny-list, caps, kill switch, verify pass, everything logged |
| Scope creep from a large roadmap | Milestones are strict; the core stays dependency-light; integrations are plugins |
| Prompt injection via inbound chat or fetched pages | Untrusted content is delimited and marked; tools gated per worker; no publish or spend commands on the deny-list |
| Secrets leak into traces or logs | Redaction at capture and logging layers; write-only storage; tests that assert masking |
| Contributor friction | Documented plugin interfaces, example adapters, a contributing guide, CI on PRs |

## 15. Success metrics

- Time from clone to first streaming chat: under ten minutes on a supported machine.
- Time from install to a paired WhatsApp worker replying: under fifteen minutes.
- Board worker closes assigned tasks with `VERIFIED` on the first attempt most of the time
  on the reference model and template set.
- Zero inbound ports required for the default install and the default channel.
- Every outbound message is present in the delivery audit log.
- A community-contributed channel or tool plugin lands without changes to the core.

## 16. Open questions and decisions

- **D1 Naming — decided (2026-11-04).** Product name is **Virtual Worker**. Derived
  defaults: CLI `vw`, env prefix `VW_`, data dir `~/.virtual-worker`, repo
  `virtual-worker`.
- **D2 License — Apache-2.0 placed in the repo** (patent grant, contributor-friendly).
  Confirm the copyright holder line before the first public push.
- **D3 Stack.** Python backend plus React frontend proposed, consistent with the prior
  work. A Go core is the alternative for a single static binary; decide in the first ADR.
- **D4 WhatsApp linked-device implementation — decided (2026-11-04): Node sidecar.**
  `backend/channels/whatsapp/bridge/` wraps Baileys 6.7.x behind a JSON-lines stdio
  protocol; the backend spawns and supervises it like the model server. Chosen because
  Node is already a prerequisite, the unofficial library stays isolated from the Python
  core, and a crash can never take the backend down.
- **D5 Channel ordering after WhatsApp — Telegram shipped second** (Bot API long polling,
  pure Python, no public URL). Discord and Slack follow on the same adapter interface.
- **D6 Team mode timing.** It changes the trust boundary; keep it behind a flag until the
  hardening pass in M6.
- **D7 Web search default.** Self-hosted SearXNG managed like the model server, versus a
  free-tier API. SearXNG proposed for the no-key default.
