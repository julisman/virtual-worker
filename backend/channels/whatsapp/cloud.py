"""WhatsApp Cloud API — Transport B (PRD FR-WA2), the official webhook path.

For operators with a WhatsApp Business account **and a public HTTPS URL**. Meta
POSTs events to ``/api/channels/webhook/whatsapp_cloud/{account}``; we verify
the ``X-Hub-Signature-256`` HMAC with the app secret, answer the one-time
``hub.challenge`` with the verify token, and send replies through the Graph API.
Pure ``httpx`` — no SDK.

Secrets (write-only): ``accessToken`` (system-user token), ``appSecret``
(signature key), ``verifyToken`` (the string you type into Meta's webhook form).
Config: ``phoneNumberId`` (the business number id) and ``apiVersion``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import os
import uuid
from pathlib import Path

import httpx

from channels.base import Attachment, ChannelAdapter, InboundMessage, registry
from channels.formatting import to_whatsapp

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v22.0"
MAX_MEDIA_BYTES = int(os.environ.get("VW_WHATSAPP_MAX_MEDIA_BYTES", str(25 * 1024 * 1024)))


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256: sha256=<hex>`` over the raw body."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip())


def parse_webhook(account_id: str, payload: dict) -> list[dict]:
    """Flatten a Cloud API webhook into ``[{message, contactName, phoneNumberId}]``.
    Status updates (``statuses``) are skipped."""
    out: list[dict] = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") != "messages" or not value.get("messages"):
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                     for c in (value.get("contacts") or [])}
            pnid = (value.get("metadata") or {}).get("phone_number_id")
            for m in value["messages"]:
                out.append({"message": m, "contactName": names.get(m.get("from")) or "",
                            "phoneNumberId": pnid})
    return out


@registry.register
class WhatsAppCloudAdapter(ChannelAdapter):
    kind = "whatsapp_cloud"
    label = "WhatsApp (Cloud API)"
    description = ("The official WhatsApp Business Platform. Needs a Meta app, a business "
                   "phone number id, and a public HTTPS URL for the webhook.")
    config_fields = [
        {"key": "phoneNumberId", "label": "Phone number ID", "placeholder": "1234567890"},
        {"key": "apiVersion", "label": "Graph API version", "placeholder": DEFAULT_API_VERSION},
        {"key": "publicUrl", "label": "Public base URL of this server",
         "placeholder": "https://assistant.example.com"},
    ]
    secret_fields = [
        {"key": "accessToken", "label": "Access token"},
        {"key": "appSecret", "label": "App secret (webhook signature)"},
        {"key": "verifyToken", "label": "Webhook verify token"},
    ]
    capabilities = {"media": True, "typing": True, "groups": False, "threads": False,
                    "pairing": False, "webhook": True, "markdown": "whatsapp"}
    max_text_len = 4000

    # ---- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        cfg = self.account.get("config") or {}
        missing = [k for k in ("accessToken", "appSecret", "verifyToken") if not self.secrets.get(k)]
        if not cfg.get("phoneNumberId"):
            missing.append("phoneNumberId")
        if missing:
            self._set_status("error", error=f"missing: {', '.join(missing)}",
                             webhookPath=self.webhook_path())
            return
        self._set_status("open", detail="webhook ready", me=str(cfg.get("phoneNumberId")),
                         webhookPath=self.webhook_path())

    async def stop(self) -> None:
        self._set_status("off")

    def webhook_path(self) -> str:
        return f"/api/channels/webhook/whatsapp_cloud/{self.account.get('id')}"

    def _api(self) -> str:
        cfg = self.account.get("config") or {}
        ver = (cfg.get("apiVersion") or DEFAULT_API_VERSION).strip()
        return f"{GRAPH}/{ver}/{cfg.get('phoneNumberId')}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secrets.get('accessToken', '')}"}

    # ---- webhook ------------------------------------------------------------ #
    def verify_challenge(self, mode: str | None, token: str | None, challenge: str | None) -> str | None:
        if mode == "subscribe" and token and hmac.compare_digest(token, self.secrets.get("verifyToken", "")):
            return challenge or ""
        return None

    def check_signature(self, body: bytes, header: str | None) -> bool:
        return verify_signature(self.secrets.get("appSecret", ""), body, header)

    async def handle_webhook(self, payload: dict) -> int:
        """Normalize every message in the payload and hand it to the manager."""
        n = 0
        for item in parse_webhook(self.account.get("id", ""), payload):
            m = item["message"]
            try:
                msg = await self._to_inbound(m, item["contactName"])
            except Exception:  # noqa: BLE001
                logger.debug("cloud webhook message parse failed", exc_info=True)
                continue
            if msg is None:
                continue
            n += 1
            await self.on_inbound(msg)
        return n

    async def _to_inbound(self, m: dict, name: str) -> InboundMessage | None:
        mtype = m.get("type")
        text = ""
        atts: list[Attachment] = []
        if mtype == "text":
            text = (m.get("text") or {}).get("body") or ""
        elif mtype in ("image", "document", "audio", "video", "sticker"):
            media = m.get(mtype) or {}
            text = media.get("caption") or ""
            path = await self._download_media(media.get("id"), media.get("mime_type"),
                                              media.get("filename"))
            if path:
                atts.append(Attachment(path=path, mime=media.get("mime_type") or "",
                                       name=media.get("filename") or os.path.basename(path),
                                       kind="image" if mtype in ("image", "sticker") else mtype,
                                       caption=text))
        elif mtype == "button":
            text = (m.get("button") or {}).get("text") or ""
        elif mtype == "interactive":
            inter = m.get("interactive") or {}
            text = ((inter.get("button_reply") or inter.get("list_reply") or {}).get("title")) or ""
        else:
            return None  # reactions, locations, contacts… not handled in v1
        sender = m.get("from") or ""
        return InboundMessage(
            channel=self.kind, account_id=self.account.get("id", ""), conversation_id=sender,
            kind="dm", sender_id=sender, sender_name=name, text=text, attachments=atts,
            reply_to=(m.get("context") or {}).get("id"), event_id=m.get("id") or "",
            ts=m.get("timestamp") or "", mentioned=True, from_me=False, raw={"type": mtype},
        )

    async def _download_media(self, media_id: str | None, mime: str | None,
                              filename: str | None) -> str | None:
        if not media_id:
            return None
        cfg = self.account.get("config") or {}
        ver = (cfg.get("apiVersion") or DEFAULT_API_VERSION).strip()
        media_dir = Path(self.data_dir) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=60) as c:
            meta = await c.get(f"{GRAPH}/{ver}/{media_id}", headers=self._headers())
            meta.raise_for_status()
            url = (meta.json() or {}).get("url")
            if not url:
                return None
            async with c.stream("GET", url, headers=self._headers()) as r:
                r.raise_for_status()
                ext = (Path(filename).suffix if filename else "") or mimetypes.guess_extension(mime or "") or ""
                path = media_dir / f"{uuid.uuid4().hex[:12]}{ext}"
                size = 0
                with open(path, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            fh.close()
                            path.unlink(missing_ok=True)
                            return None
                        fh.write(chunk)
        return str(path)

    # ---- messaging ---------------------------------------------------------- #
    async def send(self, target: str, text: str, *, reply_to: str | None = None,
                   attachments: list[Attachment] | None = None) -> str:
        to = (target or "").strip().replace("+", "").split("@", 1)[0]
        if not to:
            raise RuntimeError("target phone number is required")
        last = "sent"
        async with httpx.AsyncClient(timeout=30) as c:
            if text:
                body: dict = {"messaging_product": "whatsapp", "recipient_type": "individual",
                              "to": to, "type": "text", "text": {"preview_url": False, "body": to_whatsapp(text)}}
                if reply_to:
                    body["context"] = {"message_id": reply_to}
                r = await c.post(f"{self._api()}/messages", json=body, headers=self._headers())
                if r.status_code >= 400:
                    raise RuntimeError(f"Cloud API {r.status_code}: {r.text[:300]}")
                last = ((r.json().get("messages") or [{}])[0].get("id")) or "sent"
            for a in attachments or []:
                media_id = await self._upload(c, a)
                kind = "image" if a.kind == "image" else "document"
                payload: dict = {"messaging_product": "whatsapp", "to": to, "type": kind,
                                 kind: {"id": media_id}}
                if a.caption:
                    payload[kind]["caption"] = a.caption
                if kind == "document":
                    payload[kind]["filename"] = a.name or os.path.basename(a.path)
                r = await c.post(f"{self._api()}/messages", json=payload, headers=self._headers())
                if r.status_code >= 400:
                    raise RuntimeError(f"Cloud API {r.status_code}: {r.text[:300]}")
                last = ((r.json().get("messages") or [{}])[0].get("id")) or "sent"
        return str(last)

    async def _upload(self, c: httpx.AsyncClient, a: Attachment) -> str:
        mime = a.mime or mimetypes.guess_type(a.path)[0] or "application/octet-stream"
        with open(a.path, "rb") as fh:
            r = await c.post(f"{self._api()}/media", headers=self._headers(),
                             data={"messaging_product": "whatsapp", "type": mime},
                             files={"file": (a.name or os.path.basename(a.path), fh, mime)})
        if r.status_code >= 400:
            raise RuntimeError(f"Cloud API media upload {r.status_code}: {r.text[:300]}")
        return r.json().get("id")

    async def mark_read(self, msg: InboundMessage) -> None:
        if not msg.event_id:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(f"{self._api()}/messages", headers=self._headers(),
                             json={"messaging_product": "whatsapp", "status": "read",
                                   "message_id": msg.event_id,
                                   "typing_indicator": {"type": "text"}})
        except Exception:  # noqa: BLE001
            pass

    async def typing(self, target: str, on: bool = True) -> None:
        return None  # Cloud API only exposes typing via mark_read (handled there)

    def self_ids(self) -> set[str]:
        cfg = self.account.get("config") or {}
        me = str(cfg.get("phoneNumberId") or "")
        return {me} if me else set()
