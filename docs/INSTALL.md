# Installing Virtual Worker

Virtual Worker runs entirely on your own machine. Chats, agents, models and
traces stay there unless you connect something yourself.

## Requirements

- macOS (Apple Silicon recommended) or Linux. Windows works through WSL.
- About 1 GB for the app and its tools. Models are a separate one-time download
  of a few GB each.
- For the WhatsApp linked-device channel: Node.js 20 or newer.

## From source

```bash
git clone https://github.com/julisman/virtual-worker.git
cd virtual-worker
./dev.sh
```

The first run bootstraps everything through `setup.sh`: a Python virtualenv
with `uv`, the frontend packages, llama.cpp, the MLX engines on Apple Silicon,
Playwright's Chromium for the browser tools, and the WhatsApp bridge packages.
It prints the exact command for anything it cannot install itself (`uv`,
Node.js, Homebrew).

| Mode | Command | Where |
|---|---|---|
| Development (hot reload) | `./dev.sh` | web `http://localhost:5173`, API `:8889`, model `:8081` |
| Always-on service | `./vw start` | one port, `http://127.0.0.1:8888`; `./vw status`, `./vw logs -f`, `./vw stop` |

Use service mode for a worker that should keep answering on WhatsApp or
Telegram after you close the terminal. Both modes can run at the same time; they
use separate ports and data directories.

## First model

1. Open **Models → Discover** and search. The list marks which files fit your
   machine.
2. **Download** one. Progress shows live and resumes if interrupted.
3. Switch to **On Device**, pick a context size, and **Activate**.
4. Open **Chat** and start talking.

To use a model server you already run, start with
`VW_UPSTREAM_URL=http://127.0.0.1:11434 ./dev.sh` (Ollama) or any
OpenAI-compatible URL. To use an external provider for one agent, add a key under
**Models → Providers** and pick it in that agent's settings.

## Connect a chat app

- **Telegram**: message `@BotFather`, send `/newbot`, copy the token, then
  **Channels → Connect a channel → Telegram**.
- **WhatsApp**: **Channels → Connect a channel → WhatsApp (linked device)**, click
  **Pair**, and scan the QR code from the phone under Linked devices. Use a
  dedicated number.

## Updating

```bash
git pull
./setup.sh        # syncs dependencies when they changed
./vw restart      # or restart ./dev.sh
```

Data lives outside the repository (`~/.virtual-worker` for the service, `./data`
for dev), so updates never touch your agents, chats or models.

## Desktop app

`desktop/` contains an Electron shell and a PyInstaller spec that freeze the
backend and bundle the built UI into a single app. Build it with
`desktop/build.sh`. Ready-made downloads are not published yet.

## Uninstall

Delete the repository and the data directory (`~/.virtual-worker`). Downloaded
base models live in the Hugging Face cache at `~/.cache/huggingface`.
