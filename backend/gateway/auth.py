"""Gateway auth — local-first.

By default the gateway accepts any key (it's meant to run on localhost in front
of a local model). If ``VW_GATEWAY_KEY`` is set, the presented key must
match it. The key is read from whichever header the calling SDK uses:

  - ``x-api-key``            (Anthropic)
  - ``Authorization: Bearer`` (OpenAI / Codex)
  - ``x-goog-api-key`` / ``?key=`` (Gemini)
"""

from __future__ import annotations

import config


def extract_key(request) -> str | None:
    h = request.headers
    if h.get("x-api-key"):
        return h["x-api-key"]
    auth = h.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if h.get("x-goog-api-key"):
        return h["x-goog-api-key"]
    return request.query_params.get("key")


def gateway_key_required() -> bool:
    """True if a key is required: either a static env key, or any managed token."""
    if getattr(config, "GATEWAY_KEY", None):
        return True
    try:
        from storage import token_store
        return token_store.has_active()
    except Exception:
        return False


def check_key(request) -> bool:
    """True if the request is authorized.

    Open mode (no static key, no managed tokens) -> always True, for easy local
    use. Once a key/token exists, a matching one must be presented.
    """
    if not gateway_key_required():
        return True
    key = extract_key(request)
    if not key:
        return False
    if key == getattr(config, "GATEWAY_KEY", None):
        return True
    try:
        from storage import token_store
        return token_store.verify(key)
    except Exception:
        return False
