// WhatsApp linked-device bridge for Virtual Worker.
//
// A thin sidecar around the open-source Baileys library. It speaks JSON lines:
// events on stdout, commands on stdin (see ../linked.py for the schema). It
// holds no policy and runs no agent — it only moves messages, and the Python
// backend restarts it if it ever dies.
//
// Usage: node bridge.mjs --auth-dir <dir> --media-dir <dir> [--max-media-bytes N] [--name "Virtual Worker"]

import { createRequire } from "node:module";
import { createInterface } from "node:readline";
import { mkdir, writeFile, rm } from "node:fs/promises";
import { createReadStream } from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";

const require = createRequire(import.meta.url);

const args = parseArgs(process.argv.slice(2));
const AUTH_DIR = args["auth-dir"] || "./auth";
const MEDIA_DIR = args["media-dir"] || "./media";
const MAX_MEDIA = Number(args["max-media-bytes"] || 25 * 1024 * 1024);
const APP_NAME = args["name"] || "Virtual Worker";

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) { out[argv[i].slice(2)] = argv[i + 1]; i++; }
  }
  return out;
}

function emit(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }
function log(level, msg) { emit({ type: "log", level, msg: String(msg) }); }

let baileys, pino, QRCode;
try {
  baileys = await import("@whiskeysockets/baileys");
  pino = (await import("pino")).default;
  QRCode = (await import("qrcode")).default;
} catch (err) {
  emit({ type: "status", state: "error", error: "bridge dependencies missing — run npm install in " + path.dirname(new URL(import.meta.url).pathname) });
  process.exit(2);
}

const {
  default: makeWASocket, DisconnectReason, useMultiFileAuthState, fetchLatestBaileysVersion,
  Browsers, downloadMediaMessage, getContentType, jidNormalizedUser, isJidGroup,
} = baileys;

const logger = pino({ level: "silent" });
let sock = null;
let me = null;          // normalized own jid
let meName = null;
let stopping = false;

function userOf(jid) { return (jid || "").split("@")[0].split(":")[0]; }

async function connect() {
  await mkdir(AUTH_DIR, { recursive: true });
  await mkdir(MEDIA_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let version;
  try { ({ version } = await fetchLatestBaileysVersion()); } catch { version = undefined; }

  emit({ type: "status", state: "connecting" });
  sock = makeWASocket({
    version,
    auth: state,
    logger,
    browser: Browsers.ubuntu(APP_NAME),
    markOnlineOnConnect: false,
    syncFullHistory: false,
    generateHighQualityLinkPreview: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      let dataUrl = null;
      try { dataUrl = await QRCode.toDataURL(qr, { margin: 1, width: 320 }); } catch (e) { log("warn", "qr render failed: " + e); }
      emit({ type: "status", state: "qr", qr: dataUrl, detail: "scan with WhatsApp → Linked devices" });
    }
    if (connection === "open") {
      me = sock.user?.id ? jidNormalizedUser(sock.user.id) : null;
      meName = sock.user?.name || null;
      emit({ type: "status", state: "open", me, name: meName, detail: "connected" });
    } else if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      if (loggedOut) {
        emit({ type: "status", state: "logged_out", error: "logged out from the phone — pair again" });
        try { await rm(AUTH_DIR, { recursive: true, force: true }); } catch {}
      } else {
        emit({ type: "status", state: "reconnecting", detail: `connection closed (${code ?? "unknown"})` });
      }
      if (!stopping) {
        const delay = loggedOut ? 1000 : 3000;
        setTimeout(() => connect().catch((e) => { log("warn", "reconnect failed: " + e); process.exit(1); }), delay);
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      try { await onMessage(m); } catch (e) { log("warn", "message handling failed: " + e); }
    }
  });
}

async function onMessage(m) {
  const chat = m.key?.remoteJid || "";
  if (!chat || chat === "status@broadcast" || chat.endsWith("@newsletter")) return;
  const fromMe = !!m.key?.fromMe;
  const group = isJidGroup(chat);
  const sender = group ? (m.key?.participant || "") : chat;
  const content = m.message || {};
  const ctype = getContentType(content);
  if (!ctype || ctype === "protocolMessage" || ctype === "senderKeyDistributionMessage" || ctype === "reactionMessage") return;

  let text = "";
  let ctx = null;
  let media = null;
  if (ctype === "conversation") text = content.conversation || "";
  else if (ctype === "extendedTextMessage") { text = content.extendedTextMessage?.text || ""; ctx = content.extendedTextMessage?.contextInfo; }
  else if (ctype === "imageMessage" || ctype === "documentMessage" || ctype === "videoMessage" || ctype === "audioMessage" || ctype === "stickerMessage") {
    const inner = content[ctype] || {};
    text = inner.caption || "";
    ctx = inner.contextInfo;
    media = await saveMedia(m, ctype, inner);
  } else if (ctype === "documentWithCaptionMessage") {
    const doc = content.documentWithCaptionMessage?.message?.documentMessage || {};
    text = doc.caption || "";
    media = await saveMedia({ ...m, message: { documentMessage: doc } }, "documentMessage", doc);
  } else {
    return; // unsupported content type
  }

  const mentioned = !!(ctx?.mentionedJid || []).some((j) => me && userOf(j) === userOf(me));
  const replyTo = ctx?.stanzaId || null;
  emit({
    type: "message",
    id: m.key?.id || "",
    chat, group, sender, senderName: m.pushName || "",
    text, ts: m.messageTimestamp ? new Date(Number(m.messageTimestamp) * 1000).toISOString() : "",
    mentioned, fromMe, replyTo, media,
    key: m.key,
  });
}

async function saveMedia(m, ctype, inner) {
  const size = Number(inner.fileLength || 0);
  if (size > MAX_MEDIA) { log("warn", `media too large (${size} bytes), skipped`); return null; }
  try {
    const buf = await downloadMediaMessage(m, "buffer", {}, { logger, reuploadRequest: sock.updateMediaMessage });
    if (buf.length > MAX_MEDIA) return null;
    const mime = inner.mimetype || "application/octet-stream";
    const kind = ctype === "imageMessage" || ctype === "stickerMessage" ? "image"
      : ctype === "videoMessage" ? "video" : ctype === "audioMessage" ? "audio" : "document";
    const ext = (inner.fileName && path.extname(inner.fileName)) || ("." + (mime.split("/")[1] || "bin").split(";")[0]);
    const file = path.join(MEDIA_DIR, randomBytes(6).toString("hex") + ext);
    await writeFile(file, buf);
    return { path: file, mime, name: inner.fileName || path.basename(file), kind, caption: inner.caption || "" };
  } catch (e) {
    log("warn", "media download failed: " + e);
    return null;
  }
}

async function handleCommand(cmd) {
  const id = cmd.id;
  const reply = (ok, extra = {}) => emit({ type: "result", id, ok, ...extra });
  try {
    if (!sock && cmd.type !== "logout") throw new Error("socket not ready");
    if ((cmd.type === "send" || cmd.type === "typing" || cmd.type === "read") && !sock?.user) throw new Error("not connected — pair this number first");
    switch (cmd.type) {
      case "send": {
        let content;
        if (cmd.image?.path) content = { image: createReadStream(cmd.image.path), caption: cmd.image.caption || undefined };
        else if (cmd.document?.path) content = { document: createReadStream(cmd.document.path), fileName: cmd.document.name, mimetype: cmd.document.mime || "application/octet-stream" };
        else content = { text: String(cmd.text ?? "") };
        const opts = cmd.replyTo && cmd.quotedKey ? { quoted: { key: cmd.quotedKey, message: {} } } : {};
        const res = await sock.sendMessage(cmd.to, content, opts);
        reply(true, { messageId: res?.key?.id || "sent" });
        break;
      }
      case "typing": {
        await sock.sendPresenceUpdate(cmd.on === false ? "paused" : "composing", cmd.to);
        reply(true);
        break;
      }
      case "read": {
        if (cmd.key) await sock.readMessages([cmd.key]);
        reply(true);
        break;
      }
      case "pair": {
        const phone = String(cmd.phone || "").replace(/\D/g, "");
        if (!phone) throw new Error("phone number required");
        if (sock.authState?.creds?.registered) throw new Error("already paired");
        const code = await sock.requestPairingCode(phone);
        emit({ type: "status", state: "qr", pairingCode: code, detail: "enter this code on the phone: Linked devices → Link with phone number" });
        reply(true, { messageId: code });
        break;
      }
      case "logout": {
        stopping = true;
        try { await sock?.logout(); } catch {}
        try { await rm(AUTH_DIR, { recursive: true, force: true }); } catch {}
        reply(true);
        process.exit(0);
        break;
      }
      default:
        throw new Error("unknown command " + cmd.type);
    }
  } catch (e) {
    reply(false, { error: String(e?.message || e) });
  }
}

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let cmd;
  try { cmd = JSON.parse(line); } catch { return; }
  handleCommand(cmd);
});
rl.on("close", () => { stopping = true; process.exit(0); });
process.on("SIGTERM", () => { stopping = true; process.exit(0); });
process.on("SIGINT", () => { stopping = true; process.exit(0); });

connect().catch((e) => { emit({ type: "status", state: "error", error: String(e?.message || e) }); process.exit(1); });
