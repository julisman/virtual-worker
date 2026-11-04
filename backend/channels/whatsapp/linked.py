"""WhatsApp (linked device) — Transport A, the default (PRD FR-WA1).

Pairs a phone number as a *linked device* through the WhatsApp multi-device web
protocol. The protocol itself is implemented by a small Node sidecar
(``bridge/bridge.mjs``, built on the open-source Baileys library) that this
adapter spawns and talks to over **JSON lines on stdin/stdout** — the same
"managed child process" pattern the model server uses. The bridge dials *out*,
so no public host or inbound port is needed.

Why a sidecar: the protocol library is Node-only, it isolates an unofficial,
fast-moving dependency from the Python core, and a crash there can never take
the backend down (the adapter just restarts it with backoff).

Protocol (one JSON object per line):

  bridge → backend   {"type":"status","state":..,"qr":..,"me":..,"error":..}
                     {"type":"message", ...normalized inbound...}
                     {"type":"result","id":..,"ok":..,"messageId"|"error":..}
                     {"type":"log","level":..,"msg":..}
  backend → bridge   {"type":"send","id":..,"to":..,"text":..,"replyTo":..,
                      "image":{path,caption}|"document":{path,name,mime}}
                     {"type":"typing","to":..,"on":true|false}
                     {"type":"read","chat":..,"id":..,"participant":..}
                     {"type":"pair","phone":"628..."}    (pairing code instead of QR)
                     {"type":"logout"}

Session keys persist under ``<VW_HOME>/channels/<account>/auth`` and survive
restarts; media lands under ``<VW_HOME>/channels/<account>/media``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from channels import base
from channels.base import Attachment, ChannelAdapter, InboundMessage, registry

logger = logging.getLogger(__name__)

BRIDGE_DIR = Path(__file__).resolve().parent / "bridge"
BRIDGE_MAIN = BRIDGE_DIR / "bridge.mjs"
MAX_MEDIA_BYTES = int(os.environ.get("VW_WHATSAPP_MAX_MEDIA_BYTES", str(25 * 1024 * 1024)))


def node_bin() -> str | None:
    return os.environ.get("VW_NODE_BIN") or shutil.which("node")


def bridge_installed() -> bool:
    return (BRIDGE_DIR / "node_modules" / "@whiskeysockets" / "baileys").exists()


def jid_user(jid: str | None) -> str:
    """``6281234@s.whatsapp.net`` / ``6281234:3@lid`` → ``6281234``."""
    if not jid:
        return ""
    user = jid.split("@", 1)[0]
    return user.split(":", 1)[0]


def to_jid(target: str) -> str:
    """Accept a bare phone number, a user jid, or a group jid."""
    t = (target or "").strip().replace("+", "").replace(" ", "")
    if "@" in t:
        return t
    if t.endswith("-group") or "-" in t:   # defensive: group ids are <num>-<ts>@g.us
        return t.rstrip("-group") + "@g.us"
    return f"{t}@s.whatsapp.net"


def parse_bridge_message(account_id: str, ev: dict) -> InboundMessage:
    """Normalize one bridge ``message`` event into an :class:`InboundMessage`."""
    chat = ev.get("chat") or ""
    is_group = bool(ev.get("group")) or chat.endswith("@g.us")
    sender = ev.get("sender") or (chat if not is_group else "")
    media = ev.get("media") or None
    atts: list[Attachment] = []
    if media and media.get("path"):
        atts.append(Attachment(path=media["path"], mime=media.get("mime") or "",
                               name=media.get("name") or "", kind=media.get("kind") or "file",
                               caption=media.get("caption") or ""))
    return InboundMessage(
        channel="whatsapp", account_id=account_id, conversation_id=chat,
        kind="group" if is_group else "dm", sender_id=jid_user(sender),
        sender_name=ev.get("senderName") or "", text=ev.get("text") or "",
        attachments=atts, reply_to=ev.get("replyTo") or None, event_id=ev.get("id") or "",
        ts=ev.get("ts") or "", mentioned=bool(ev.get("mentioned")),
        from_me=bool(ev.get("fromMe")), raw={"participant": sender, "key": ev.get("key")},
    )


@registry.register
class WhatsAppLinkedAdapter(ChannelAdapter):
    kind = "whatsapp"
    label = "WhatsApp (linked device)"
    description = ("Pair a phone number by scanning a QR code, like WhatsApp Web. Dials out — "
                   "no public URL needed. Uses an unofficial client library; use a dedicated "
                   "number and accept WhatsApp's terms.")
    config_fields = [
        {"key": "phone", "label": "Phone number (optional, for pairing by code)",
         "placeholder": "6281234567890"},
    ]
    secret_fields: list[dict] = []
    capabilities = {"media": True, "typing": True, "groups": True, "threads": False,
                    "pairing": True, "webhook": False, "markdown": "whatsapp"}
    max_text_len = 3500

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._qr: str | None = None
        self._pairing_code: str | None = None

    # ---- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        if not node_bin():
            self._set_status("error", error="Node.js not found — install Node 20+ (or set VW_NODE_BIN)")
            return
        if not bridge_installed():
            self._set_status("error", error=f"bridge not installed — run: cd {BRIDGE_DIR} && npm install")
            return
        self._stop.clear()
        self._set_status("connecting", detail="starting bridge")
        self._loop_task = asyncio.get_event_loop().create_task(
            base.run_with_backoff(f"whatsapp:{self.account.get('id')}", self._run_once, self._stop))

    async def stop(self) -> None:
        self._stop.set()
        await self._kill()
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        self._set_status("off")

    async def _kill(self) -> None:
        p = self._proc
        self._proc = None
        if p and p.returncode is None:
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
        for f in self._pending.values():
            if not f.done():
                f.set_exception(RuntimeError("bridge stopped"))
        self._pending.clear()

    async def _run_once(self) -> None:
        auth = Path(self.data_dir) / "auth"
        media = Path(self.data_dir) / "media"
        auth.mkdir(parents=True, exist_ok=True)
        media.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "NODE_NO_WARNINGS": "1"}
        self._proc = await asyncio.create_subprocess_exec(
            node_bin(), str(BRIDGE_MAIN), "--auth-dir", str(auth), "--media-dir", str(media),
            "--max-media-bytes", str(MAX_MEDIA_BYTES), "--name", "Virtual Worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(BRIDGE_DIR),
        )
        proc = self._proc
        self._reader_task = asyncio.get_event_loop().create_task(self._read_stderr(proc))
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                await self._on_event(ev)
        finally:
            if self._reader_task:
                self._reader_task.cancel()
            rc = await proc.wait() if proc.returncode is None else proc.returncode
            if not self._stop.is_set():
                self._set_status("reconnecting", detail=f"bridge exited (code {rc})")
                raise RuntimeError(f"bridge exited with code {rc}")

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            logger.debug("whatsapp bridge: %s", line.decode("utf-8", "replace").rstrip())

    # ---- events ------------------------------------------------------------- #
    async def _on_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "status":
            state = ev.get("state") or "unknown"
            if state == "qr":
                self._qr = ev.get("qr")
            elif state in ("open", "close", "logged_out"):
                self._qr = None
            if ev.get("pairingCode"):
                self._pairing_code = ev.get("pairingCode")
            me = jid_user(ev.get("me")) or self._status.get("me")
            self._set_status(state, detail=ev.get("detail") or "", me=me,
                             name=ev.get("name") or self._status.get("name"),
                             error=ev.get("error"), hasQr=bool(self._qr),
                             pairingCode=self._pairing_code)
        elif t == "message":
            try:
                msg = parse_bridge_message(self.account.get("id", ""), ev)
            except Exception:  # noqa: BLE001
                logger.debug("bad bridge message", exc_info=True)
                return
            await self.on_inbound(msg)
        elif t == "result":
            f = self._pending.pop(ev.get("id") or "", None)
            if f and not f.done():
                if ev.get("ok"):
                    f.set_result(ev.get("messageId") or "sent")
                else:
                    f.set_exception(RuntimeError(ev.get("error") or "bridge error"))
        elif t == "log":
            logger.log(logging.WARNING if ev.get("level") == "warn" else logging.DEBUG,
                       "whatsapp bridge: %s", ev.get("msg"))

    async def _command(self, cmd: dict, *, wait: bool = True, timeout: float = 60.0):
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise RuntimeError("WhatsApp bridge is not running")
        rid = uuid.uuid4().hex[:10]
        cmd = {**cmd, "id": rid}
        fut: asyncio.Future | None = None
        if wait:
            fut = asyncio.get_event_loop().create_future()
            self._pending[rid] = fut
        async with self._write_lock:
            proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    # ---- messaging ---------------------------------------------------------- #
    async def send(self, target: str, text: str, *, reply_to: str | None = None,
                   attachments: list[Attachment] | None = None) -> str:
        if self._status.get("state") != "open":
            raise RuntimeError(f"WhatsApp is not connected (state: {self._status.get('state')})")
        jid = to_jid(target)
        last = "sent"
        if text:
            last = await self._command({"type": "send", "to": jid, "text": text, "replyTo": reply_to})
        for a in attachments or []:
            payload: dict = {"type": "send", "to": jid}
            if a.kind == "image":
                payload["image"] = {"path": a.path, "caption": a.caption}
            else:
                payload["document"] = {"path": a.path, "name": a.name or os.path.basename(a.path),
                                       "mime": a.mime or "application/octet-stream"}
            last = await self._command(payload)
        return str(last)

    async def typing(self, target: str, on: bool = True) -> None:
        if self._status.get("state") != "open":
            return
        try:
            await self._command({"type": "typing", "to": to_jid(target), "on": bool(on)}, wait=False)
        except Exception:  # noqa: BLE001
            pass

    async def mark_read(self, msg: InboundMessage) -> None:
        key = (msg.raw or {}).get("key")
        if not key or self._status.get("state") != "open":
            return
        try:
            await self._command({"type": "read", "key": key}, wait=False)
        except Exception:  # noqa: BLE001
            pass

    # ---- pairing ------------------------------------------------------------ #
    async def pair(self) -> dict:
        """Return the current QR (data URL) or pairing code; request a code when
        a phone number is configured and no QR is pending."""
        phone = (self.account.get("config") or {}).get("phone") or ""
        if phone and self._status.get("state") in ("qr", "connecting") and not self._pairing_code:
            try:
                code = await self._command({"type": "pair", "phone": phone.replace("+", "")}, timeout=30)
                self._pairing_code = str(code)
                self._set_status(self._status.get("state", "qr"), pairingCode=self._pairing_code)
            except Exception as exc:  # noqa: BLE001
                logger.debug("pairing code request failed: %s", exc)
        return {"supported": True, "state": self._status.get("state"), "qr": self._qr,
                "pairingCode": self._pairing_code, "me": self._status.get("me")}

    async def logout(self) -> None:
        try:
            await self._command({"type": "logout"}, timeout=15)
        except Exception:  # noqa: BLE001
            pass
        await self._kill()
        shutil.rmtree(Path(self.data_dir) / "auth", ignore_errors=True)
        self._qr = None
        self._pairing_code = None
        self._set_status("logged_out", me=None, hasQr=False, pairingCode=None)

    def self_ids(self) -> set[str]:
        me = self._status.get("me")
        return {me, f"{me}@s.whatsapp.net"} if me else set()

    def status(self) -> dict:
        s = super().status()
        s["installed"] = bridge_installed()
        s["node"] = bool(node_bin())
        return s
