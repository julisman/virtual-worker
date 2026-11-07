# Providers — external LLM provider catalog + BYOK / subscription credentials.

"""GET /api/providers, PUT /api/providers/{id}.

Lets an agent run against a paid remote provider instead of the local model
(see ``gateway/providers.py``). Credentials are stored once in ``settings.json`` and
shared across agents. Secrets are **write-only**: the catalog reports only whether
a key/token is set (``hasKey``/``hasToken``), never the value itself.

Local only, unauthenticated like the rest of /api/*. Switching an agent off the
local model is the only thing that causes egress, and it is always explicit.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gateway import providers

logger = logging.getLogger(__name__)

router = APIRouter()


class ProviderCredIn(BaseModel):
    apiKey: str | None = None   # BYOK API key (empty string clears)
    token: str | None = None    # subscription access token (empty string clears)
    baseUrl: str | None = None  # optional base URL override
    apiType: str | None = None  # custom provider: chat_completions | responses
    authMode: str | None = None  # custom provider: bearer | none
    reasoningEffort: bool | None = None  # custom provider: forward reasoning_effort
    vision: dict | None = None  # custom gateway: per-model image_url input map
    thinkingFormat: dict | str | None = None  # custom provider: per-model reasoning dialect map (openai|openrouter|zai)
    replayReasoning: bool | dict | None = None  # custom provider: per-model replay map (bool = "*" row)
    caching: dict | None = None  # custom gateway per-model cache toggles
    reasoningKey: str | None = None  # custom gateway: write-only at-rest reasoning key ("" clears + purges)


@router.get("")
async def list_providers():
    return {"providers": providers.catalog()}


def _models_url(base_url: str) -> str:
    """Return the sibling ``/models`` URL for a configured API base/endpoint."""
    base = base_url.rstrip("/")
    for endpoint in ("chat/completions", "responses", "models"):
        suffix = f"/{endpoint}"
        if base.endswith(suffix):
            return f"{base[:-len(suffix)]}/models"
    return f"{base}/models"


@router.get("/{provider_id}/models")
async def discover_models(provider_id: str):
    """Discover models from a configured provider endpoint.

    This stays server-side so browser CORS never determines whether a local
    gateway can be used. It makes the same explicit, opt-in outbound request as
    the provider itself and never returns the configured credential.
    """
    defn = providers.PROVIDER_DEFS.get(provider_id)
    if not defn or not (defn.get("custom") or defn.get("modelDiscovery")):
        raise HTTPException(404, "model discovery is not available for this provider")
    rec = providers.get_record(provider_id)
    base_url = (rec.get("baseUrl") or defn.get("baseUrl") or "").strip()
    if not base_url:
        raise HTTPException(400, "configure a base URL before discovering models")
    headers: dict[str, str] = {}
    requires_key = not defn.get("custom") or rec.get("authMode") != "none"
    if requires_key:
        if not rec.get("apiKey"):
            raise HTTPException(400, "save an API key before discovering models")
        headers["Authorization"] = f"Bearer {rec['apiKey']}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(_models_url(base_url), headers=headers)
        if response.status_code // 100 != 2:
            raise HTTPException(response.status_code, response.text[:500])
        data = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"model discovery failed: {exc}") from exc

    raw_models = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw_models, list):
        raw_models = data.get("models", []) if isinstance(data, dict) else []
    # PRD-23: cache any context windows carried in the response so window
    # resolution can use them without a fresh network call on the hot path.
    providers.note_discovered_windows(provider_id, raw_models)
    models: list[str] = []
    for item in raw_models:
        model_id = item if isinstance(item, str) else (item.get("id") if isinstance(item, dict) else None)
        if isinstance(model_id, str) and model_id.strip() and model_id not in models:
            models.append(model_id)
    return {"models": models}


@router.put("/{provider_id}")
async def set_provider(provider_id: str, body: ProviderCredIn):
    if provider_id not in providers.PROVIDER_DEFS:
        raise HTTPException(404, "no such provider")
    fields = body.model_dump(exclude_unset=True)
    try:
        providers.set_record(
            provider_id,
            apiKey=fields.get("apiKey", providers._UNSET),
            token=fields.get("token", providers._UNSET),
            baseUrl=fields.get("baseUrl", providers._UNSET),
            apiType=fields.get("apiType", providers._UNSET),
            authMode=fields.get("authMode", providers._UNSET),
            reasoningEffort=fields.get("reasoningEffort", providers._UNSET),
            vision=fields.get("vision", providers._UNSET),
            thinkingFormat=fields.get("thinkingFormat", providers._UNSET),
            replayReasoning=fields.get("replayReasoning", providers._UNSET),
            caching=fields.get("caching", providers._UNSET),
            reasoningKey=fields.get("reasoningKey", providers._UNSET),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return next(p for p in providers.catalog() if p["id"] == provider_id)


# --- ChatGPT subscription: OAuth (PKCE) sign-in ----------------------------- #

@router.post("/openai/oauth/start")
async def openai_oauth_start():
    """Begin the ChatGPT sign-in. Returns the authorize URL for the browser to
    open; a one-shot loopback listener on localhost:1455 catches the redirect."""
    from gateway import openai_oauth
    try:
        url = openai_oauth.start_login()
    except openai_oauth.OAuthError as exc:
        raise HTTPException(409, str(exc))
    return {"authUrl": url}


@router.get("/openai/oauth/status")
async def openai_oauth_status():
    from gateway import openai_oauth
    return openai_oauth.login_status()


@router.post("/openai/oauth/disconnect")
async def openai_oauth_disconnect():
    from gateway import openai_oauth
    openai_oauth.disconnect()
    return {"status": "idle"}


# Claude (Anthropic) subscription is a pasted long-lived token from `claude setup-token`
# saved through the generic PUT above as the provider `token` and sent as a Bearer with
# the Claude Code beta headers (gateway/egress.py). There is no PKCE sign-in route.
# Operators should check Anthropic's terms before using a subscription token here.
