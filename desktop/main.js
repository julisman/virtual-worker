// Virtual Worker desktop launcher (Electron main process).
//
// Responsibilities:
//   1. Start the frozen Python backend (a PyInstaller binary — no system Python).
//   2. Wait until it answers on 127.0.0.1:<port>/health.
//   3. Open a window pointing at the backend, which serves both the API and the
//      built React UI (single origin).
//   4. Auto-update the whole app via electron-updater (GitHub Releases feed).
//
// Everything runs on localhost; nothing leaves the machine except an update check.

const { app, BrowserWindow, dialog, shell, powerSaveBlocker } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const net = require("net");
const path = require("path");
const fs = require("fs");

let backendProc = null;
let mainWindow = null;
let backendPort = 8888;

const isPackaged = app.isPackaged;
const resourcesPath = process.resourcesPath; // Contents/Resources in a packaged app

// --- locate the bundled pieces -------------------------------------------- //
function backendBinary() {
  // Dev override so we can test the launcher against a locally-frozen backend.
  if (process.env.VW_BACKEND_BIN) return process.env.VW_BACKEND_BIN;
  const exe = process.platform === "win32" ? "vw-backend.exe" : "vw-backend";
  return path.join(resourcesPath, "backend", "vw-backend", exe);
}
function staticDir() {
  return process.env.VW_STATIC_DIR || path.join(resourcesPath, "frontend");
}
function llamaBin() {
  const exe = process.platform === "win32" ? "llama-server.exe" : "llama-server";
  const p = path.join(resourcesPath, "bin", exe);
  return fs.existsSync(p) ? p : "";
}

// --- pick a free localhost port ------------------------------------------- //
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// --- start the backend ---------------------------------------------------- //
function startBackend() {
  const bin = backendBinary();
  if (!fs.existsSync(bin)) {
    dialog.showErrorBox("Virtual Worker", `Backend not found at:\n${bin}\n\nThe install may be corrupt — reinstall Virtual Worker.`);
    app.quit();
    return;
  }
  const env = {
    ...process.env,
    VW_PACKAGED: "1",
    VW_BACKEND_PORT: String(backendPort),
    VW_RESOURCES_DIR: resourcesPath,
    VW_STATIC_DIR: staticDir(),
  };
  const llama = llamaBin();
  if (llama) env.LLAMA_BIN = llama;

  backendProc = spawn(bin, [], { env, stdio: ["ignore", "pipe", "pipe"] });
  backendProc.stdout.on("data", (d) => process.stdout.write(`[backend] ${d}`));
  backendProc.stderr.on("data", (d) => process.stderr.write(`[backend] ${d}`));
  backendProc.on("exit", (code) => {
    backendProc = null;
    if (!app.isQuitting) {
      dialog.showErrorBox("Virtual Worker", `The Virtual Worker backend stopped unexpectedly (code ${code}).`);
      app.quit();
    }
  });
}

// --- wait for /health ----------------------------------------------------- //
function waitForHealth(timeoutMs = 60000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: "127.0.0.1", port: backendPort, path: "/health", timeout: 2000 }, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on("error", retry);
      req.on("timeout", () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (Date.now() - start > timeoutMs) return reject(new Error("backend did not become healthy in time"));
      setTimeout(tick, 400);
    };
    tick();
  });
}

// --- window --------------------------------------------------------------- //
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Virtual Worker",
    backgroundColor: "#F4F1E8",
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  // Open external links in the system browser, not inside the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.loadFile(path.join(__dirname, "loading.html"));
  mainWindow.once("ready-to-show", () => mainWindow.show());
  return mainWindow;
}

// --- updates -------------------------------------------------------------- //
// Where humans go to download a new build (GitLab Releases page).
const RELEASES_PAGE =
  process.env.VW_RELEASES_URL ||
  "https://github.com/virtual-worker";
// Silent auto-install only works on a SIGNED macOS build (Squirrel rejects
// unsigned updates). Until you have an Apple Developer ID, we run in "notify"
// mode: tell the user + open the download page. Build with VW_AUTO_INSTALL=1
// once signed to switch to true one-click auto-update.
const AUTO_INSTALL = process.env.VW_AUTO_INSTALL === "1";

function setupUpdates() {
  if (!isPackaged) return; // updates only make sense for an installed app
  let autoUpdater;
  try {
    ({ autoUpdater } = require("electron-updater"));
  } catch {
    return;
  }
  autoUpdater.autoDownload = AUTO_INSTALL;          // don't download when we can't install
  autoUpdater.autoInstallOnAppQuit = AUTO_INSTALL;

  // A newer version exists on the feed.
  autoUpdater.on("update-available", async (info) => {
    if (AUTO_INSTALL) return; // signed path: electron-updater downloads → 'update-downloaded'
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: "info",
      buttons: ["Download", "Later"],
      defaultId: 0,
      title: "Update available",
      message: `Virtual Worker ${info.version} is available — you have ${app.getVersion()}.`,
      detail: "Open the download page to get the new version. Your data and models are untouched.",
    });
    if (response === 0) shell.openExternal(RELEASES_PAGE);
  });

  // Signed path only: an update finished downloading → offer to restart.
  autoUpdater.on("update-downloaded", async (info) => {
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: "info",
      buttons: ["Restart now", "Later"],
      defaultId: 0,
      title: "Update ready",
      message: `Virtual Worker ${info.version} has been downloaded.`,
      detail: "Restart to install the update. Your data and models are untouched.",
    });
    if (response === 0) { app.isQuitting = true; autoUpdater.quitAndInstall(); }
  });

  autoUpdater.on("error", (err) => console.error("[updater]", err));
  // Check on launch, then every 6 hours while running. Failures (e.g. the feed
  // needs auth, or you're offline) are non-fatal — the app runs regardless.
  autoUpdater.checkForUpdates().catch((e) => console.error("[updater]", e));
  setInterval(() => autoUpdater.checkForUpdates().catch(() => {}), 6 * 60 * 60 * 1000);
}

// --- keep-awake ------------------------------------------------------------ //
// Virtual Worker is an always-on agent host: its background loops (scheduler, triggers, and
// live channels like chat channel) must keep running while the Mac is idle or locked.
// `prevent-app-suspension` keeps the system + network awake but still lets the
// display sleep (saves power, no burn-in) — the native, in-process equivalent of
// `caffeinate`, so no external process or manual macOS Energy-Saver change is
// needed. Set VW_KEEP_AWAKE=0 to opt out. Tied to the app lifetime: it starts
// when the app is ready and releases automatically when Virtual Worker quits.
let powerBlockerId = null;
function startKeepAwake() {
  if (process.env.VW_KEEP_AWAKE === "0" || powerBlockerId !== null) return;
  try {
    powerBlockerId = powerSaveBlocker.start("prevent-app-suspension");
    console.log(`[keep-awake] system suspension blocked (id=${powerBlockerId})`);
  } catch (e) { console.warn(`[keep-awake] failed: ${e.message}`); }
}
function stopKeepAwake() {
  if (powerBlockerId !== null && powerSaveBlocker.isStarted(powerBlockerId)) {
    powerSaveBlocker.stop(powerBlockerId);
  }
  powerBlockerId = null;
}

// --- lifecycle ------------------------------------------------------------ //
app.whenReady().then(async () => {
  createWindow();
  startKeepAwake();
  backendPort = await freePort();
  startBackend();
  try {
    await waitForHealth();
    await mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
  } catch (e) {
    dialog.showErrorBox("Virtual Worker", `Virtual Worker failed to start:\n${e.message}`);
    app.quit();
    return;
  }
  setupUpdates();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", () => { app.isQuitting = true; });
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });

// Always kill the backend child when Electron exits.
function killBackend() {
  if (backendProc) { try { backendProc.kill(); } catch { /* */ } backendProc = null; }
}
app.on("quit", () => { stopKeepAwake(); killBackend(); });
process.on("exit", killBackend);
