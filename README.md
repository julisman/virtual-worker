<p align="center">
  <img src="frontend/src/assets/logo.svg" alt="Virtual Worker" width="720" />
</p>

# Virtual Worker

**Virtual Worker** is an open-source, local-first platform for running autonomous AI
workers on your own machine. A worker is more than a chatbot: it has a persona, tools,
skills, memory, a task board, schedules, and guardrails. It runs on a local model by
default (or a provider you bring your own key for), and the roadmap makes it reachable
from the chat apps people already use, starting with **WhatsApp**.

Everything is local by default: chats, agents, memory, downloaded models, and API traces
stay on your machine. Nothing leaves it unless you explicitly use web search, a remote
MCP server, a remote upstream, or a connected chat channel.

> Status: early. The core runtime (agents, harness, board, schedules, triggers, delivery,
> skills, knowledge, templates, gateway, traces) and the first chat channel (WhatsApp)
> work today. See [`docs/PRD.md`](docs/PRD.md) for the full product plan.

## What it does today

- **Chat channels**: message an agent from **WhatsApp** or **Telegram**. WhatsApp pairs a
  number by scanning a QR code (linked device, no public URL needed) or uses the official
  Cloud API webhook; Telegram takes a bot token and long-polls. DM and group policies, an
  allow-list with pairing approval, per-chat agent routing, per-conversation history, media
  in and out, and an audited `channel_send` tool for proactive messages. The adapter
  interface is the plugin point for Discord and Slack.
- **Service mode**: `./vw start` runs everything detached with automatic restarts and the
  built UI on one port, so a worker keeps answering after you close the terminal.
- **Agents** with a persona, model, working folder, memory, skills, MCP servers, custom
  tools, and guardrails. Chat with them in the web UI with streaming tool activity.
- **Agent harness**: tool registry, agent loop, context budgeting, automatic guardrails
  (working-folder confinement, shell deny-list, caps, kill switch), and a verify pass.
- **Task board and activity feed** per agent, with an always-on worker that pulls tasks
  when the agent is on duty.
- **Schedules** (cron and one-time) and **triggers** (file watch, inbound webhook).
- **Outbound delivery** to in-app chat, desktop notifications and webhooks, with an audit
  log.
- **Skills** as portable `SKILL.md` packs with progressive disclosure.
- **Knowledge bases** with local embeddings and cited retrieval.
- **Templates**: deploy a ready-made agent from a gallery, export or import bundles.
- **Models**: discover and download models from Hugging Face, see what fits your
  machine, activate one. Managed llama.cpp (GGUF) and MLX (Apple Silicon) engines, or
  any OpenAI-compatible upstream (Ollama, vLLM, LM Studio).
- **Multi-protocol gateway**: use your local model from coding agents over OpenAI,
  Anthropic Messages, OpenAI Responses and Gemini shaped endpoints, protected by minted
  API tokens.
- **Traces**: every model call captured locally with tokens, cost, latency and tool
  calls. Analytics and monitoring on top.
- **Optional team mode** with accounts and roles for a shared host. Off by default.
- **Backup and restore** of the whole data directory.

## Run

```bash
./dev.sh                 # hot-reload dev profile
#   web   → http://localhost:5173
#   api   → http://127.0.0.1:8889   (OpenAPI docs: /docs)
#   model → http://127.0.0.1:8081/v1   (backend-managed)

./vw start               # always-on service: built UI + API on one port, auto-restart
#   app   → http://127.0.0.1:8888   (model server on :8080)
./vw status | ./vw logs -f | ./vw restart | ./vw stop
```

Dev and service use different default ports and data directories (`./data` for dev,
`~/.virtual-worker` for the service), so both can run side by side.

Either launcher bootstraps on first run via `setup.sh` (backend venv with `uv`, frontend
deps, llama.cpp via Homebrew, and the MLX engines on Apple Silicon). Prerequisites it
cannot install for you: **uv**, **Node.js**, and **Homebrew** (for llama.cpp). Then open
**Models**, download a model, and **Activate** it.

Use your own model server instead of the managed one:

```bash
VW_UPSTREAM_URL=http://127.0.0.1:11434 ./dev.sh   # e.g. Ollama
```

## Configuration

All local state lives under `VW_HOME` (default `~/.virtual-worker`; `dev.sh` sets it to
`./data`). The database is `app.db`, app settings are `settings.json`.

| Env var | Default | Purpose |
|---|---|---|
| `VW_HOME` | `~/.virtual-worker` | data dir (db, settings, logs, pid) |
| `VW_PORT` | `8888` | service port for `./vw start` (dev uses `BACKEND_PORT`, default `8889`) |
| `VW_UPSTREAM_URL` | `http://127.0.0.1:8080` | OpenAI-compatible model server; setting it disables managed mode |
| `VW_MANAGED_LLAMA` | `1` | backend starts, stops and switches the model server |
| `VW_MODEL_MAP` / `VW_DEFAULT_MODEL` | – | map vendor model ids to your local model |
| `VW_GATEWAY_KEY` | – | require a static key on the gateway |
| `VW_DEBUG_CAPTURE` | `1` | capture model calls into the trace store |
| `VW_SKIP_MLX` | `0` | skip the MLX engines on Apple Silicon |
| `VW_DL_CONNECTIONS` / `VW_DL_CHUNK_MB` | `16` / `16` | parallel download tuning |
| `LLAMA_*` / `MLX_*` | – | engine binaries, port, context, GPU layers |
| `HF_TOKEN` | – | gated or private Hugging Face repos |
| `TAVILY_API_KEY` / `BRAVE_API_KEY` | – | optional web search providers |
| `VW_NODE_BIN` | `node` on PATH | Node binary for the WhatsApp bridge sidecar |
| `VW_WHATSAPP_MAX_MEDIA_BYTES` | `26214400` | cap on a received WhatsApp attachment |
| `VW_TELEGRAM_MAX_MEDIA_BYTES` | `20971520` | cap on a received Telegram attachment |

## Connect a chat app

**Telegram** (quickest): message `@BotFather`, send `/newbot`, copy the token. Open
**Channels → Connect a channel → Telegram**, paste the token, pick the default agent. Done.
No public URL is needed; the bot long-polls.

**WhatsApp (linked device):**

1. Open **Channels → Connect a channel**, choose **WhatsApp (linked device)**, pick the
   default agent, and connect.
2. Click **Pair**, then on the phone: Settings → Linked devices → Link a device, and scan
   the QR code.
3. Message the number from another phone. Unknown senders receive a pairing code you
   approve on the card (the default DM policy); switch to **open** or **allowlist** as
   you prefer. In groups the agent answers only when mentioned.

The linked-device transport is a small Node sidecar built on the open-source
[Baileys](https://github.com/WhiskeySockets/Baileys) library, managed by the backend
like the model server. It is an unofficial client: use a dedicated number and accept
WhatsApp's terms. Businesses with a public URL can use the official **Cloud API**
transport instead.

## Tests

```bash
cd backend  && .venv/bin/python -m pytest -q
cd frontend && npm run build
```

## Roadmap

The product plan lives in [`docs/PRD.md`](docs/PRD.md). Next milestones:

1. Discord and Slack channels on the same adapter interface; delegation with flow-back
   into the originating chat.
2. Security hardening pass and desktop packaging.
3. Knowledge wikis, template registries, analytics budgets.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Licensed under [Apache-2.0](LICENSE).
