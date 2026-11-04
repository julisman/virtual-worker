"""Chat channels API — accounts, pairing, bindings, pairing requests, webhooks.

Prefix ``/api/channels``. Secrets are accepted on create/patch and never
returned; responses carry ``hasX`` booleans instead. Toggling ``enabled`` or
changing config/secrets reloads the adapter. The webhook endpoints are the only
inbound surface and exist solely for transports that need one (Cloud API).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from channels.base import registry
from channels.manager import manager
from storage import agent_store, chat_channel_store as store

logger = logging.getLogger(__name__)
router = APIRouter()


def _catalog() -> list[dict]:
    out = []
    for cls in registry.kinds():
        item = {"kind": cls.kind, "label": cls.label, "description": cls.description,
                "configFields": cls.config_fields, "secretFields": cls.secret_fields,
                "capabilities": cls.capabilities}
        if cls.kind == "whatsapp":
            from channels.whatsapp import linked
            item["installed"] = linked.bridge_installed()
            item["node"] = bool(linked.node_bin())
            item["installHint"] = f"cd {linked.BRIDGE_DIR} && npm install"
        out.append(item)
    return out


def _view(acct: dict) -> dict:
    cls = registry.get(acct["kind"])
    keys = [f["key"] for f in (cls.secret_fields if cls else [])]
    return {**acct, "status": manager.status(acct["id"]),
            "secrets": store.secret_flags(acct["id"], keys),
            "bindings": store.list_bindings(acct["id"]),
            "pairingRequests": store.list_pairing_requests(acct["id"])}


@router.get("")
async def list_all():
    return {"kinds": _catalog(), "accounts": [_view(a) for a in store.list_accounts()]}


class AccountIn(BaseModel):
    kind: str
    name: str | None = None
    enabled: bool = False
    defaultAgentId: str | None = None
    config: dict | None = None
    secrets: dict | None = None


class AccountPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    defaultAgentId: str | None = None
    config: dict | None = None
    secrets: dict | None = None


@router.post("/accounts")
async def create_account(body: AccountIn):
    if registry.get(body.kind) is None:
        raise HTTPException(400, f"unknown channel kind {body.kind!r}")
    if body.defaultAgentId and not agent_store.get_agent(body.defaultAgentId):
        raise HTTPException(400, "defaultAgentId does not exist")
    acct = store.create_account(body.kind, body.name or registry.get(body.kind).label,
                                enabled=body.enabled, default_agent_id=body.defaultAgentId,
                                config=body.config)
    if body.secrets:
        store.set_secrets(acct["id"], body.secrets)
    if acct["enabled"]:
        await manager.reload_account(acct["id"])
    return _view(store.get_account(acct["id"]))


@router.get("/accounts/{account_id}")
async def get_account(account_id: str):
    acct = store.get_account(account_id)
    if not acct:
        raise HTTPException(404, "no such account")
    return _view(acct)


@router.patch("/accounts/{account_id}")
async def patch_account(account_id: str, body: AccountPatch):
    acct = store.get_account(account_id)
    if not acct:
        raise HTTPException(404, "no such account")
    if body.defaultAgentId and not agent_store.get_agent(body.defaultAgentId):
        raise HTTPException(400, "defaultAgentId does not exist")
    fields: dict = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.defaultAgentId is not None:
        fields["default_agent_id"] = body.defaultAgentId or None
    if body.config is not None:
        fields["config"] = body.config
    if body.secrets:
        store.set_secrets(account_id, body.secrets)
    if fields:
        store.update_account(account_id, **fields)
    # Reload only when something the adapter cares about changed.
    if body.enabled is not None or body.secrets or (body.config is not None):
        await manager.reload_account(account_id)
    return _view(store.get_account(account_id))


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, purge: bool = False):
    await manager.stop_account(account_id)
    if not store.delete_account(account_id):
        raise HTTPException(404, "no such account")
    if purge:
        import shutil
        import config
        shutil.rmtree(config.APP_HOME / "channels" / account_id, ignore_errors=True)
    return {"ok": True}


@router.get("/accounts/{account_id}/status")
async def status(account_id: str):
    if not store.get_account(account_id):
        raise HTTPException(404, "no such account")
    return manager.status(account_id)


@router.post("/accounts/{account_id}/pair")
async def pair(account_id: str):
    """Ensure the adapter runs and return the current pairing payload (QR / code)."""
    acct = store.get_account(account_id)
    if not acct:
        raise HTTPException(404, "no such account")
    if not acct["enabled"]:
        store.update_account(account_id, enabled=True)
    adapter = manager.adapter(account_id)
    if adapter is None:
        await manager.start_account(account_id)
        adapter = manager.adapter(account_id)
    if adapter is None:
        return {"supported": False, "status": manager.status(account_id)}
    out = await adapter.pair()
    return {**out, "status": adapter.status()}


@router.get("/accounts/{account_id}/qr")
async def qr(account_id: str):
    adapter = manager.adapter(account_id)
    if adapter is None:
        return {"state": manager.status(account_id).get("state", "off"), "qr": None}
    out = await adapter.pair() if adapter.capabilities.get("pairing") else {"supported": False}
    return {**out, "status": adapter.status()}


@router.post("/accounts/{account_id}/logout")
async def logout(account_id: str):
    adapter = manager.adapter(account_id)
    if adapter is None:
        raise HTTPException(409, "account is not running")
    await adapter.logout()
    return {"ok": True, "status": adapter.status()}


class TestSend(BaseModel):
    to: str
    text: str = "Hello from Virtual Worker 👋"


@router.post("/accounts/{account_id}/test")
async def test_send(account_id: str, body: TestSend):
    if not store.get_account(account_id):
        raise HTTPException(404, "no such account")
    rec = await manager.send(account_id, body.to, body.text, source="test")
    return rec


# ---- bindings ------------------------------------------------------------- #

class BindingIn(BaseModel):
    conversationId: str
    agentId: str
    title: str | None = None


@router.get("/accounts/{account_id}/bindings")
async def list_bindings(account_id: str):
    return {"bindings": store.list_bindings(account_id)}


@router.post("/accounts/{account_id}/bindings")
async def upsert_binding(account_id: str, body: BindingIn):
    if not store.get_account(account_id):
        raise HTTPException(404, "no such account")
    if not agent_store.get_agent(body.agentId):
        raise HTTPException(400, "agentId does not exist")
    try:
        return store.upsert_binding(account_id, body.conversationId, body.agentId, body.title)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/accounts/{account_id}/bindings/{binding_id}")
async def delete_binding(account_id: str, binding_id: str):
    if not store.delete_binding(binding_id):
        raise HTTPException(404, "no such binding")
    return {"ok": True}


# ---- pairing requests ----------------------------------------------------- #

@router.get("/accounts/{account_id}/pairing-requests")
async def pairing_requests(account_id: str, status: str | None = "pending"):
    return {"requests": store.list_pairing_requests(account_id, status or None)}


@router.post("/accounts/{account_id}/pairing-requests/{request_id}/{decision}")
async def resolve_pairing(account_id: str, request_id: str, decision: str):
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    req = store.resolve_pairing(request_id, approve=decision == "approve")
    if not req:
        raise HTTPException(404, "no such request")
    if decision == "approve":
        acct = store.get_account(account_id)
        adapter = manager.adapter(account_id)
        if acct and adapter:
            try:
                await manager.send(account_id, req["senderId"],
                                   "You're approved — say hi!", source="pairing")
            except Exception:  # noqa: BLE001
                pass
    return req


# ---- webhooks (Cloud API only) -------------------------------------------- #

def _webhook_adapter(kind: str, account_id: str):
    adapter = manager.adapter(account_id)
    if adapter is None or adapter.kind != kind or not adapter.capabilities.get("webhook"):
        raise HTTPException(404, "no such webhook")
    return adapter


@router.get("/webhook/{kind}/{account_id}")
async def webhook_verify(kind: str, account_id: str, request: Request):
    adapter = _webhook_adapter(kind, account_id)
    q = request.query_params
    challenge = adapter.verify_challenge(q.get("hub.mode"), q.get("hub.verify_token"),
                                         q.get("hub.challenge"))
    if challenge is None:
        raise HTTPException(403, "verification failed")
    return PlainTextResponse(challenge)


@router.post("/webhook/{kind}/{account_id}")
async def webhook_receive(kind: str, account_id: str, request: Request):
    adapter = _webhook_adapter(kind, account_id)
    body = await request.body()
    if not adapter.check_signature(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(403, "bad signature")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "invalid JSON")
    n = await adapter.handle_webhook(payload)
    return {"ok": True, "messages": n}
