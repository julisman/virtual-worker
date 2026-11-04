# Gateway info — powers the frontend Connect page.

"""GET /api/gateway/info -> what the gateway exposes + live upstream status.

Returns the protocol endpoints, the model a request resolves to (so the Connect
page can prefill ANTHROPIC_MODEL etc.), whether a gateway key is required, and
whether the upstream model server is currently reachable.
"""

import logging

import httpx
from fastapi import APIRouter

import config
from config import UPSTREAM_URL
from gateway.auth import gateway_key_required
from gateway.models import resolve_model
from runtime import llama_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/info")
async def gateway_info():
    upstream_ok = False
    upstream_models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(f"{UPSTREAM_URL.rstrip('/')}/v1/models")
        upstream_ok = resp.status_code == 200
        if upstream_ok:
            upstream_models = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
    except Exception:
        upstream_ok = False

    # Prefer the runtime's actual ACTIVE model (friendly name) for the snippets;
    # /v1/models / resolve_model are unreliable for MLX (lists several).
    active_name = None
    try:
        st = await llama_manager.status()
        if st.get("healthy"):
            active_name = st.get("modelName")
    except Exception:
        active_name = None
    resolved = active_name or (await resolve_model(None))

    return {
        "upstreamUrl": UPSTREAM_URL,
        "upstreamReachable": upstream_ok or bool(active_name),
        "upstreamModels": upstream_models,
        "resolvedModel": resolved or None,
        "activeModel": active_name,
        "modelMap": getattr(config, "MODEL_MAP", {}) or {},
        "defaultModel": getattr(config, "DEFAULT_MODEL", None),
        "gatewayKeyRequired": gateway_key_required(),
        "endpoints": {
            "anthropic": "/v1/messages",
            "openaiChat": "/v1/chat/completions",
            "openaiResponses": "/v1/responses",
            "gemini": "/v1beta/models/{model}:generateContent",
        },
    }
