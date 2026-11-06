"""Context-window resolution for PRD-23 (token-accurate budget mode).

Single source of truth for "how big is this agent's model window, and where did
that number come from?" — used by BOTH the run path (``agent/runner.py`` threads
the resolved window into ``loop.run``) and the read-only ``contextWindowInfo``
field on the agents GET response, so the UI footer never re-implements the ladder.

Resolution order (FR-10), first hit wins:
  1. the agent's ``contextTokens`` guardrail override;
  2. the machine-wide ``settings.json`` ``modelWindows`` map;
  3. automatic per source — managed llama ctx / managed MLX config.json /
     remote provider catalog / a cached custom-upstream probe;
  4. unresolved ⇒ ``(None, None)`` (the loop falls back to chars mode).

Provenance is one of ``managed server | model config | catalog | probed |
override`` (or ``None`` when unresolved).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from gateway import providers

logger = logging.getLogger(__name__)

# The safety component of the automatic output reserve — headroom for chars/4
# under-estimating the un-measured tail (FR-7).
AUTO_RESERVE_SAFETY = 4096

# Caches so window resolution never repeats work / network on the hot path.
_mlx_window_cache: dict[str, int | None] = {}
_upstream_probe_cache: dict[str, int | None] = {}


def effective_reserve(window: int, user_reserve: int) -> int:
    """``max(userReserve, autoFloor)`` where ``autoFloor = MAX_OUTPUT_TOKENS +
    safety`` clamped to ``window // 4`` so small local windows aren't starved
    (FR-7). A configured reserve can only move compaction earlier."""
    from agent.loop import MAX_OUTPUT_TOKENS  # lazy — avoid an import cycle
    auto_floor = min(MAX_OUTPUT_TOKENS + AUTO_RESERVE_SAFETY, max(window // 4, 0))
    return max(int(user_reserve or 0), auto_floor)


def _model_windows() -> dict[str, int]:
    """The machine-wide ``settings.json`` ``modelWindows`` map ({model-id: tokens})."""
    from storage import settings
    raw = settings.get("modelWindows", {}) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if isinstance(k, str) and iv > 0:
            out[k] = iv
    return out


def _mlx_window(path: str) -> int | None:
    """``max_position_embeddings`` from an MLX model dir's config.json (cached)."""
    if path in _mlx_window_cache:
        return _mlx_window_cache[path]
    win: int | None = None
    try:
        cfg = json.loads((Path(path) / "config.json").read_text())
        n = cfg.get("max_position_embeddings")
        if isinstance(n, int) and n > 0:
            win = n
    except Exception:  # noqa: BLE001 — missing/ambiguous config ⇒ unknown
        win = None
    _mlx_window_cache[path] = win
    return win


def _probe_upstream_sync(url: str) -> int | None:
    """Cached custom-upstream window (llama.cpp /props n_ctx, else vLLM
    /v1/models max_model_len). Returns the cached value only — never probes."""
    return _upstream_probe_cache.get(url)


async def _probe_upstream(url: str) -> int | None:
    """One cached probe of a custom ``VW_UPSTREAM_URL`` (FR-14). Re-probes only
    when the URL changes (the cache key)."""
    if url in _upstream_probe_cache:
        return _upstream_probe_cache[url]
    base = url.rstrip("/")
    win: int | None = None
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            try:
                r = await client.get(f"{base}/props")
                n = ((r.json().get("default_generation_settings") or {}).get("n_ctx")
                     if r.status_code == 200 else None)
                if isinstance(n, int) and n > 0:
                    win = n
            except Exception:  # noqa: BLE001
                win = None
            if win is None:
                r = await client.get(f"{base}/v1/models")
                data = (r.json().get("data") or []) if r.status_code == 200 else []
                for m in data:
                    n = m.get("max_model_len") if isinstance(m, dict) else None
                    if isinstance(n, int) and n > 0:
                        win = n
                        break
    except Exception:  # noqa: BLE001 — upstream down ⇒ unknown, fall back to chars
        win = None
    _upstream_probe_cache[url] = win
    return win


def _local_window_sync() -> tuple[int | None, str | None]:
    """Local-upstream window from cached state only (no network) — for the GET
    path. Managed llama ctx, managed MLX config.json, or a cached probe."""
    import config
    from runtime import llama_manager
    st = llama_manager._state
    if config.MANAGED_LLAMA:
        backend = st.get("backend")
        if backend == "llamacpp" and st.get("ctx"):
            return int(st["ctx"]), "managed server"
        if backend in ("mlx", "mlx-vlm") and st.get("path"):
            w = _mlx_window(st["path"])
            if w:
                return w, "model config"
        return None, None
    w = _probe_upstream_sync(config.UPSTREAM_URL)
    if w:
        return w, "probed"
    return None, None


def resolve_window_sync(*, model: str | None, provider_id: str | None,
                        guardrail_context_tokens: int = 0) -> tuple[int | None, str | None]:
    """The window ladder WITHOUT any network (caches + catalog only). Used by the
    agents GET route so ``contextWindowInfo`` is fast and side-effect-free."""
    if guardrail_context_tokens and int(guardrail_context_tokens) > 0:
        return int(guardrail_context_tokens), "override"
    w = _model_windows().get(model or "")
    if isinstance(w, int) and w > 0:
        return w, "settings map"
    if provider_id:
        return providers.provider_window(provider_id, model or "")
    return _local_window_sync()


async def resolve_window(*, model: str | None, provider_id: str | None,
                         guardrail_context_tokens: int = 0,
                         allow_probe: bool = False) -> tuple[int | None, str | None]:
    """The full window ladder (FR-10). Same as :func:`resolve_window_sync` but may
    run a single cached custom-upstream probe (``allow_probe``) on the run path."""
    win, src = resolve_window_sync(
        model=model, provider_id=provider_id,
        guardrail_context_tokens=guardrail_context_tokens)
    if win:
        return win, src
    # Only the custom-upstream case is left un-probed by the sync ladder.
    if allow_probe and not provider_id:
        import config
        if not config.MANAGED_LLAMA:
            w = await _probe_upstream(config.UPSTREAM_URL)
            if w:
                return w, "probed"
    return None, None


def _agent_target_lite(agent: dict) -> tuple[str | None, str | None]:
    """The ``(model, provider_id)`` an agent would run against, WITHOUT resolving a
    live target (no network / OAuth). Mirrors ``resolve_target``'s model pick so
    the GET route's window matches what the run path uses."""
    a = agent or {}
    source = a.get("modelSource") or "local"
    provider = (a.get("provider") or "").strip()
    if source == "local" or not providers.is_provider(provider):
        return a.get("model") or None, None
    defn = providers.PROVIDER_DEFS[provider]
    default_models = (defn.get("subscriptionModels") if source == "subscription"
                      else defn.get("models")) or []
    model = a.get("model") or (default_models[0] if default_models else "")
    return model, provider


def context_window_info(agent: dict) -> dict:
    """The read-only ``contextWindowInfo`` field on an agent GET response (FR-18 /
    §9): ``{window, source, effectiveReserve, compactsAt}``. Best-effort and fast
    — cached probe results only, never a network call on the GET path."""
    g = (agent or {}).get("guardrails") or {}
    model, provider_id = _agent_target_lite(agent)
    window, source = resolve_window_sync(
        model=model, provider_id=provider_id,
        guardrail_context_tokens=g.get("contextTokens") or 0)
    if not window:
        return {"window": None, "source": None, "effectiveReserve": None, "compactsAt": None}
    eff = effective_reserve(window, g.get("reserveTokens") or 0)
    return {"window": window, "source": source,
            "effectiveReserve": eff, "compactsAt": window - eff}
