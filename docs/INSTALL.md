# Installing Virtual Worker

Virtual Worker is a local AI studio that runs **entirely on your Mac** — your chats, agents,
and models stay on your machine. This guide is written for anyone; no technical
setup required.

> **Requirements:** a Mac with **Apple Silicon** (M1/M2/M3/M4), macOS 12 or newer.
> The app is a few hundred MB; AI models are a separate one-time download.

---

## Install (about 2 minutes)

1. **Download** the latest `Virtual Worker-<version>-arm64.dmg` from the team's
   **Google Drive folder** (ask the Virtual Worker maintainer for the link), or from the
   releases page:
   https://github.com/virtual-worker
2. **Open the `.dmg`** and drag **Virtual Worker** into your **Applications** folder.
3. **First launch only — right-click to open:**
   In Applications, **right-click** (or Control-click) **Virtual Worker → Open**, then click
   **Open** in the dialog.

   > Why? Virtual Worker isn't distributed through the App Store, so the very first time macOS
   > asks you to confirm. A normal double-click will say *"Virtual Worker can't be opened"* —
   > **right-click → Open** is the way past it, and you only do it **once**. Every
   > launch after that is a normal double-click.

   Still blocked? Open the **Terminal** app, paste this line, press Return, then try
   step 3 again:
   ```
   xattr -dr com.apple.quarantine /Applications/Virtual Worker.app
   ```

---

## First run — add an AI model

Virtual Worker opens in its own window. The first time, it has no model loaded yet. Pick
**one** of the two options below.

### Option A — the team model, LocalFT (recommended)

**LocalFT** is our own fine-tuned model (Supply Chain ID identity + tool use,
runs fully on your Mac). It's in the same Google Drive folder as the installer.

> **Requires Virtual Worker 0.2.5 or newer** — older versions don't scan the `models`
> folder and won't list a hand-copied model. The version is shown next to the
> Virtual Worker logo in the sidebar; if it's older, install the latest `.dmg` first.

1. **Download** `LocalFT-9B-MLX-6bit-ft_c310a6ec.zip` from the Drive folder
   (~6.6 GB — one time) and **double-click to unzip** it. You get a folder named
   `LocalFT-9B-MLX-6bit-ft_c310a6ec` — don't rename it.
2. **Move that folder into Virtual Worker's models directory.** In Finder press
   **⌘⇧G** (Go to Folder), paste:
   ```
   ~/Library/Application Support/Virtual Worker/models
   ```
   and press Return. (If the `models` folder doesn't exist yet, create it there
   first.) Drop the unzipped folder inside.
3. **Open Virtual Worker** → **Models** → **On Device**. `LocalFT-9B-MLX-6bit-ft_c310a6ec`
   is in the list — click **Activate**.
4. Open **Chat** and say hi. It should introduce itself as **LocalFT**.

> You need ~7 GB free disk and it runs comfortably on 16 GB+ Macs.
> Developer running from source instead of the app? The models directory is
> `<repo>/data/models` (when using `./dev.sh`) or `~/.virtual-worker/models`.

### Option B — any public model

1. Go to **Models** → **Discover**, search for a small model (the list shows which
   ones **fit** your Mac).
2. Click **Download** (a one-time, multi-GB download — it stays on your Mac).
3. Switch to **On Device** and click **Activate**.
4. Open **Chat** and start talking.

That's it — you're running a private AI studio locally.

---

## Getting updates

When a newer version is available, Virtual Worker shows an **"Update available → Download"**
message and opens the releases page. Download the new `.dmg` and drag it into
Applications (replacing the old one). **Your chats and models are kept** — they live
outside the app.

---

## Good to know

- **Private by default.** Everything runs on your machine. Nothing is sent anywhere
  unless *you* turn on web search or connect a remote model.
- **Uninstall:** drag **Virtual Worker** from Applications to the Trash. Your data stays in
  `~/Library/Application Support/Virtual Worker` until you delete that folder too.
- **Problems?** Ping the Virtual Worker maintainer with a screenshot — most first-launch issues
  are the right-click → Open step above.

---

### 📋 Short version to paste into chat

> **Install Virtual Worker + the LocalFT team model (Mac, Apple Silicon):**
> 0. Need **Virtual Worker 0.2.5 or newer** for step 4–5 (version shows in the sidebar).
> 1. From the team Google Drive folder, download `Virtual Worker-*.dmg` **and**
>    `LocalFT-9B-MLX-6bit-ft_c310a6ec.zip`.
> 2. Open the `.dmg`, drag **Virtual Worker** to **Applications**.
> 3. First time only: **right-click Virtual Worker → Open → Open** (a plain double-click warns
>    the first time — right-click → Open gets past it, just once).
> 4. Unzip the model. In Finder press ⌘⇧G, go to
>    `~/Library/Application Support/Virtual Worker/models` (create `models` if missing) and
>    drop the `LocalFT-9B-MLX-6bit-ft_c310a6ec` folder there.
> 5. In Virtual Worker: **Models → On Device → LocalFT… → Activate** → **Chat**.
>    Everything runs locally. 🦦

## Run from source

Developers can choose between two local modes after running `./setup.sh`:

- `./vw start` runs the stable service detached, with the built UI and API
  on `http://127.0.0.1:8888` (use `./vw status`, `logs`, and `stop`).
- `./dev.sh` runs the foreground hot-reload profile on the stable-safe defaults
  `:5173` (web), `:8889` (API), and `:8081` (model); press Ctrl+C to stop it.
