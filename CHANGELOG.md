# Changelog

All notable changes to Virtual Worker are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-07

First public release. Virtual Worker is a local-first platform for running
autonomous AI workers on your own machine: agents with a persona, tools, skills,
memory, a task board, schedules and guardrails, served by a local model by
default and reachable from the chat apps you already use.

### Highlights

- **Runs on your machine.** A managed local model server (llama.cpp, or MLX on
  Apple Silicon) with a model hub that shows what fits your hardware, resumable
  downloads and one-click activation. Any OpenAI-compatible server works as an
  upstream. Nothing leaves the machine unless you connect something.
- **Workers, not chatbots.** Each worker has a persona, a working folder it
  cannot leave, tools, skills, memory that learns from feedback, a task board
  with a definition of done, schedules, triggers and guardrails with a kill
  switch. An opt-in duty worker picks up board tasks on its own and verifies
  its work before closing them.
- **Chat where people already are.** WhatsApp (linked device with QR or pairing
  code, or the official Cloud API), Telegram, Discord and Slack, through one
  channel plugin interface. Every channel dials out, so no public host is
  needed. Pairing approval, allow-lists, group mention policies, per-chat
  routing, media in and out, and a full delivery audit.
- **Guided first run.** A setup wizard picks a model for your hardware, deploys
  a starter worker from a template and optionally connects a chat app.
- **Everything audited.** Every outbound send has a row in Deliveries. Every
  model call is captured in the trace viewer. Analytics shows cost, usage and
  per-worker scorecards.

### Added

#### Model runtime and providers
- Managed llama.cpp and MLX engines with version checks and updates.
- Model hub with fit estimates, chunked resumable downloads and gated-repo
  support through `HF_TOKEN`.
- Per-worker external providers (bring your own key) for DeepSeek, OpenAI,
  Anthropic, OpenCode Go and any OpenAI-compatible gateway, normalized to one streaming
  shape with retries, prompt caching, reasoning effort and discovered context
  windows. Credentials are stored once and never returned.
- ChatGPT subscription sign-in through the open-source Codex CLI's public OAuth
  flow, and a pasted `claude setup-token` for Claude. Both opt-in per worker
  and subject to the vendor's terms.
- An OpenAI-compatible gateway with Anthropic Messages, OpenAI Responses and
  Gemini translators, so coding agents can use the local model. Minted
  `sk-vw-…` tokens, hashed at rest.

#### Workers and harness
- Worker CRUD with persona, model source, tools, skills, MCP servers,
  knowledge bases, wikis, custom tools, guardrails and a working folder.
- A bounded agent loop with repeat and empty-reply guards, context compression
  with recoverable originals, token-accurate context windows, a verify pass,
  and one run entry point shared by every surface.
- Built-in tools: files, shell with tiers, web fetch and search (self-hosted
  SearXNG by default), browser control through Playwright, documents (PDF,
  Word, Excel, PowerPoint, CSV), memory, knowledge, wiki, schedules, delivery,
  team delegation and chat-channel sends.
- Skills as portable `SKILL.md` packs with progressive disclosure, a skill
  factory, and a Skills library in the app.
- Multi-agent teams: delegation, ask-and-wait with budgets, and an inbox with
  flow-back into the originating chat or channel.

#### Board, schedules, delivery
- Task board with definition of done, an activity feed and the duty worker.
- Cron and one-time schedules, file-watch and inbound webhook triggers.
- Delivery to in-app chat, desktop notifications, webhooks (allow-listed),
  email over SMTP (allow-listed recipients), connected chat channels and
  Obsidian vaults, with a Deliveries page over the audit log.

#### Chat channels
- Channel plugin contract and a shared manager: dedup, policies, pairing,
  bindings, per-conversation queues with debounce, slash commands, history
  with tool provenance, reasoning stripped from replies, acks on long runs,
  timeouts, typing indicators and media handling.
- WhatsApp linked device (Node sidecar on Baileys with QR and pairing code),
  WhatsApp Cloud API webhook with signature checks, Telegram long polling,
  Discord Gateway with session resume, Slack Socket Mode.

#### Knowledge and templates
- Knowledge bases with cited retrieval, and agent-maintained wikis as plain
  markdown folders with lint and a graph view.
- Templates: bundle format with typed variables, a gallery, deploy, export and
  import with dependency checks, and git-backed registries with provenance,
  version checks and diffed updates. A registry scaffold and a template lint
  tool that runs in CI.

#### Observability, trust and operations
- Capture middleware and a trace viewer for every model call, analytics with
  budgets, a retention loop with tunable windows.
- Feedback capture on chat replies (up, down, edit) with a curated JSONL
  export for fine-tuning.
- Security baseline: local-only CORS, host allow-list, log redaction,
  write-only secrets, private-address guards on outbound requests, path
  confinement.
- Optional team mode behind a flag: accounts, roles with a per-route floor,
  an audit log and a login rate limiter.
- Backup and restore of the whole data directory as one `.vwbak` file, with a
  rollback snapshot before every import and opt-in scheduled local snapshots.
- A dev launcher with hot reload, an always-on service mode (`./vw`) with a
  supervisor, log file and install-as-login-service, a headless `vw run`, and
  an Electron plus PyInstaller desktop scaffold.
- In-app documentation for every screen, an architecture guide, an install
  guide, a security policy and a contributing guide.

### Known limitations

- Windows runs through WSL only.
- Slack file uploads are not sent; the reply names the file instead.
- Desktop builds are not published; the scaffold builds locally.
- The browser recorder, voice, evaluation suites and fine-tuning are on the
  roadmap but not in this release.
- The WhatsApp linked-device transport relies on an unofficial client library.
  Use a dedicated number and read the disclosure in the app.

[Unreleased]: https://github.com/OWNER/virtual-worker/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/virtual-worker/releases/tag/v0.1.0
