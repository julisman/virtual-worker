"""Model-name resolution.

Coding agents send vendor model ids (``claude-…``, ``gpt-…``, ``gemini-…``) that
must map to whatever single model the local upstream actually serves. Resolution
order:

  1. ``VW_MODEL_MAP[requested]``  (explicit per-id mapping)
  2. ``VW_DEFAULT_MODEL``         (one local model for everything)
  3. first id from the upstream ``/v1/models`` (cached)
  4. the requested name unchanged       (llama.cpp ignores it anyway)
"""

from __future__ import annotations

import logging

import httpx

import config
from config import UPSTREAM_URL

logger = logging.getLogger(__name__)

_first_model_cache: str | None = None


def invalidate_cache() -> None:
    """Drop the cached first-upstream-model id (call when the active model
    changes, so resolve_model() reflects the newly loaded model)."""
    global _first_model_cache
    _first_model_cache = None


async def _first_upstream_model() -> str | None:
    global _first_model_cache
    if _first_model_cache is not None:
        return _first_model_cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{UPSTREAM_URL.rstrip('/')}/v1/models")
        data = resp.json()
        first = (data.get("data") or [{}])[0].get("id")
        if first:
            _first_model_cache = first
        return first
    except Exception as exc:  # upstream down — fall through to passthrough
        logger.debug("could not fetch upstream models: %s", exc)
        return None


async def resolve_model(requested: str | None) -> str:
    mapping = getattr(config, "MODEL_MAP", {}) or {}
    if requested and requested in mapping:
        return mapping[requested]
    default = getattr(config, "DEFAULT_MODEL", None)
    if default:
        return default
    # The model the runtime actually loaded (reliable). MLX's /v1/models lists
    # several entries, so never trust its first id — use the active path.
    try:
        from runtime import llama_manager
        active = llama_manager._state.get("path")
        if active:
            return active
    except Exception:
        pass
    first = await _first_upstream_model()
    if first:
        return first
    return requested or ""
