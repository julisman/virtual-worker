"""External LLM providers — the catalog + credentials + per-agent target resolution.

Virtual Worker is local-first: by default every agent runs against the machine's active
model (the managed llama.cpp / MLX server). This module adds the *opt-in* escape
hatch — letting an individual agent run against a paid, remote provider instead:

  - **BYOK** (bring your own key): a static API key you paste in. DeepSeek is the
    first-class target (OpenAI-compatible), plus OpenAI and Anthropic.
  - **Subscription**: a provider access token (e.g. a Claude / ChatGPT login
    token) used as a bearer credential.

Provider *credentials* live once in the app settings file (``settings.json`` under the
``providers`` key) so they're reused across agents and never duplicated into the
agents table. Each *agent* only stores which source + provider it wants
(``modelSource`` + ``provider`` + ``model``); :func:`resolve_target` turns that
into a concrete egress target (base URL + auth) at call time.

Any network egress here is explicit and opt-in — an agent only leaves the machine
when it is deliberately switched off "local". See ``gateway/egress.py`` for the
actual HTTP clients/translators.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.models import resolve_model
from storage import settings

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "providers"

# Static provider catalog. ``kind`` selects the egress client in egress.py:
#   "openai"    -> OpenAI-compatible /chat/completions (DeepSeek, OpenAI)
#   "anthropic" -> Anthropic /v1/messages (translated to/from the OpenAI shape)
#   "responses" -> OpenAI-compatible /responses (translated to/from chat shape)
# ``byok``/``subscription`` gate which auth modes the UI offers. ``models`` is
# the API-key suggestion list; subscription providers may expose their own
# maintained catalog. Free-text is always allowed.
PROVIDER_DEFS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "kind": "openai",
        "baseUrl": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "byok": True,
        "subscription": False,
        # PRD-23 context windows (tokens). DeepSeek doesn't report windows in its
        # API, so they're static: both current models are 128k (131072).
        "windows": {"default": 131072},
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "kind": "openai",
        "baseUrl": "https://api.openai.com/v1",
        # BYOK (API) suggestions. An API key can replace these with its account's
        # live /v1/models result.
        "models": ["gpt-5.4", "gpt-5.4-codex", "gpt-5.1-codex", "gpt-4o", "gpt-4o-mini", "o4-mini"],
        # ChatGPT subscription is a separate Codex product surface, not the
        # OpenAI API. Keep its maintained catalog distinct (mirrors Pi v0.84.2)
        # and default new/unset subscription agents to the fast Luna tier.
        "subscriptionModels": [
            "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.5",
            "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark",
        ],
        "byok": True,
        "subscription": True,
        # PRD-23 context windows (tokens). OpenAI/ChatGPT don't report windows via
        # the API, so they're static published values: the gpt-5 family ships large
        # windows, the 4o family 128k, o4-mini 200k. Subscription ids (gpt-5.6-*)
        # fall under the gpt-5 prefix. Whoever adds a model id adds its window here.
        "windows": {
            "exact": {"gpt-4o": 128000, "gpt-4o-mini": 128000, "o4-mini": 200000},
            "prefixes": [("gpt-5", 272000), ("gpt-4", 128000)],
            "default": 128000,
        },
        # Vision (multimodal image input) capability — same {exact, prefixes,
        # default} shape as ``windows`` (see :func:`supports_vision`). The gpt-4o
        # and gpt-5 families accept image_url content; the o-series text-reasoning
        # ids default off (conservative — a false negative degrades to a download
        # + note, never an error).
        "vision": {
            "prefixes": [("gpt-5", True), ("gpt-4o", True), ("gpt-4", True)],
            "default": False,
        },
        # API-key users can enumerate the models their project can actually use.
        # ChatGPT subscription OAuth uses the distinct Codex backend, which does
        # not expose a documented equivalent endpoint.
        "modelDiscovery": True,
        "reasoningEffort": True,
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "baseUrl": "https://api.anthropic.com",
        "models": ["claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001"],
        # PRD-23 context windows (tokens). Anthropic doesn't report windows via the
        # API; the family default is 200k, with per-id exceptions when they differ.
        "windows": {"default": 200000, "exact": {}},
        # Every current Claude model accepts image blocks (see :func:`supports_vision`).
        "vision": {"default": True},
        "byok": True,
        # Subscription = a pasted long-lived OAuth token from `claude setup-token`
        # (sk-ant-oat01-…), sent as Bearer + the Claude-Code beta headers (egress.py).
        # NOT a PKCE sign-in, so anthropic stays out of OAUTH_SUBSCRIPTION → the UI
        # shows a paste-token field. Using a Claude subscription token in a third-party
        # app is restricted by Anthropic's terms; enabling it is the operator's call.
        "subscription": True,
    },
    "opencode-go": {
        # Go is purchased as a subscription, but OpenCode issues an API key for
        # it.  It therefore belongs in the BYOK credential flow rather than the
        # OAuth/token subscription flow.
        "label": "OpenCode Go (subscription API key)",
        "kind": "openai",
        "baseUrl": "https://opencode.ai/zen/go/v1",
        "models": [
            "deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2", "glm-5.1",
            "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "gpt-5.6-luna",
            "minimax-m3", "minimax-m2.7", "qwen3.8-max", "qwen3.7-max",
        ],
        "byok": True,
        "subscription": False,
        # PRD-23: OpenCode Go windows come from genuine detection (its own /models
        # response, cached by the discovery route — see note_discovered_windows).
        # Static family prefixes sit underneath as a fallback for un-probed ids.
        "windows": {
            "prefixes": [("deepseek-", 131072), ("glm-", 131072), ("kimi-", 262144),
                         ("qwen3", 262144), ("minimax-", 1000000), ("gpt-5", 272000)],
        },
        # The available Go catalog changes independently of Virtual Worker releases.
        "modelDiscovery": True,
        "credentialLabel": "Go API key",
        "credentialHint": "Issued by OpenCode after you subscribe to Go. Stored locally, shared across agents, and never shown again.",
    },
    "openai-compatible": {
        "label": "OpenAI-compatible (custom)",
        # ``kind`` is selected from the stored apiType in resolve_target().
        "kind": "openai",
        "baseUrl": "",
        "models": [],
        "byok": True,
        "subscription": False,
        "custom": True,
        # Custom gateways expose /models like any OpenAI-compatible server; the
        # frontend gates every discovery affordance on this flag, so it must
        # match the /models route's own eligibility (custom OR modelDiscovery).
        "modelDiscovery": True,
        # "OpenAI-compatible" only guarantees the base protocol.  It does not
        # imply support for OpenAI's reasoning extension, so this is the DEFAULT:
        # off. The operator opts a specific gateway in via the per-record
        # `reasoningEffort` toggle (set_record / supports_reasoning_effort),
        # which overrides this default when truthy.
        "reasoningEffort": False,
        "apiTypes": ["chat_completions", "responses"],
        "defaultApiType": "chat_completions",
        "authModes": ["bearer", "none"],
        "defaultAuthMode": "bearer",
    },
}


# OpenCode Go uses several wire protocols behind one API key.  Keep this
# routing beside the provider definition so the selected model, rather than the
# agent's previous model, decides which egress adapter receives each turn.
_OPENCODE_GO_RESPONSES_MODELS = {"gpt-5.6-luna"}
_OPENCODE_GO_ANTHROPIC_PREFIXES = ("minimax-", "qwen3.8-", "qwen3.7-", "qwen3.6-")


def _opencode_go_kind(model: str) -> str:
    model = model.lower()
    if model in _OPENCODE_GO_RESPONSES_MODELS:
        return "responses"
    if model.startswith(_OPENCODE_GO_ANTHROPIC_PREFIXES):
        return "anthropic"
    return "openai"


def is_provider(provider: str | None) -> bool:
    return bool(provider) and provider in PROVIDER_DEFS


# --------------------------------------------------------------------------- #
# Context windows (PRD-23)
# --------------------------------------------------------------------------- #
# Windows genuinely discovered from a provider's own /models response (OpenCode
# Go, openai-compatible custom) are cached here by the discovery route so window
# resolution never adds a network call on the hot path. Static catalog windows
# live in each PROVIDER_DEFS["windows"] entry.
_discovered_windows: dict[tuple[str, str], int] = {}
_cache_seen: dict[tuple[str, str], dict[str, int]] = {}


def note_discovered_windows(provider_id: str, raw_models: list) -> None:
    """Record any per-model context windows carried in a /models discovery
    response (``context_length`` / ``max_model_len`` / ``context_window``).
    Best-effort — called from the model-discovery route, never on the hot path."""
    for item in raw_models or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        w = item.get("context_length") or item.get("max_model_len") or item.get("context_window")
        if isinstance(mid, str) and isinstance(w, int) and w > 0:
            _discovered_windows[(provider_id, mid)] = w


def provider_window(provider_id: str, model: str) -> tuple[int | None, str | None]:
    """Resolve a remote model's context window (tokens) and its provenance.

    First hit wins: a window discovered from the provider's own /models response
    (source ``"probed"``), then the static catalog — exact id map → family-prefix
    rules → provider default (source ``"catalog"``). ``(None, None)`` when unknown.
    """
    disc = _discovered_windows.get((provider_id, model))
    if isinstance(disc, int) and disc > 0:
        return disc, "probed"
    windows = (PROVIDER_DEFS.get(provider_id) or {}).get("windows") or {}
    exact = (windows.get("exact") or {}).get(model)
    if isinstance(exact, int) and exact > 0:
        return exact, "catalog"
    for prefix, w in (windows.get("prefixes") or []):
        if model and model.startswith(prefix) and isinstance(w, int) and w > 0:
            return w, "catalog"
    dflt = windows.get("default")
    if isinstance(dflt, int) and dflt > 0:
        return dflt, "catalog"
    return None, None


def note_cache_usage(provider_id: str, model: str, cache_read: int,
                     cache_write: int) -> None:
    """Keep the last normalized cache counters for the provider catalog."""
    _cache_seen[(provider_id, model)] = {
        "cacheRead": int(cache_read or 0),
        "cacheWrite": int(cache_write or 0),
    }


# --------------------------------------------------------------------------- #
# Credentials (stored in settings.json, never echoed back to the client verbatim)
# --------------------------------------------------------------------------- #

# Provider-compat reasoning dialects the operator may declare per model (§ PRD-25).
# ``openai`` is the standard OpenAI-compatible spelling. An absent row remains
# unset for backwards-compatible storage and is interpreted as this default by
# the egress layer.
_THINKING_FORMATS = ("openai", "openrouter", "zai")


def _normalize_thinking_map(value: Any) -> dict[str, str]:
    """Coerce a stored ``thinkingFormat`` value to the per-model map form
    (``{model_id: fmt, "*": default}``). A legacy scalar string is treated as
    the ``"*"`` default row (migrated forward on the next ``set_record`` write,
    mirroring ``_normalize_caching``). Unknown/blank values are dropped."""
    if isinstance(value, str):
        v = value.strip()
        return {"*": v} if v in _THINKING_FORMATS else {}
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for mid, fmt in value.items():
            if not isinstance(mid, str):
                continue
            f = fmt.strip() if isinstance(fmt, str) else fmt
            if f in _THINKING_FORMATS:
                out[mid] = f
        return out
    return {}


def _normalize_caching(value: Any) -> bool:
    """Coerce a caching-map value to a boolean. The map's values are booleans
    now (``{"model": true}``), but an earlier build wrote a two-flag dict shape
    (``{"promptCacheKey": true}``) that real config files still carry — treat
    such a dict as enabled only when ANY of its flags is truthy. Anything else
    is falsy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return any(bool(v) for v in value.values())
    return False


def _normalize_bool_map(value: Any) -> dict[str, bool]:
    """Coerce a stored per-model tri-state capability value to the map form
    (``{model_id: bool, \"*\": default}``). A legacy scalar ``True`` is treated
    as the ``\"*\"`` default row. Explicit ``False`` rows are kept — they mean
    \"off even when the default is on\" (readers resolve exact-row-first)."""
    if isinstance(value, dict):
        out: dict[str, bool] = {}
        for mid, val in value.items():
            if not isinstance(mid, str):
                continue
            if isinstance(val, bool):
                out[mid] = val
            elif isinstance(val, dict):
                # Legacy two-flag dict shape ({"promptCacheKey": true, …}) —
                # any truthy flag reads as enabled, else an explicit off.
                out[mid] = _normalize_caching(val)
        return out
    if _normalize_caching(value):
        return {"*": True}
    return {}


def _all_records() -> dict[str, dict]:
    rec = settings.get(_SETTINGS_KEY, {}) or {}
    return rec if isinstance(rec, dict) else {}


def get_record(provider: str) -> dict:
    """Stored provider credentials and custom endpoint configuration."""
    r = _all_records().get(provider) or {}
    return r if isinstance(r, dict) else {}


_UNSET = object()


def set_record(provider: str, *, apiKey: Any = _UNSET, token: Any = _UNSET,
               baseUrl: Any = _UNSET, apiType: Any = _UNSET,
               authMode: Any = _UNSET, reasoningEffort: Any = _UNSET,
               thinkingFormat: Any = _UNSET, replayReasoning: Any = _UNSET,
               caching: Any = _UNSET, vision: Any = _UNSET,
               reasoningKey: Any = _UNSET) -> None:
    """Merge credential fields for a provider. Passing an empty string clears
    that field; fields left unset are untouched."""
    if provider not in PROVIDER_DEFS:
        raise ValueError(f"unknown provider: {provider}")
    custom = bool(PROVIDER_DEFS[provider].get("custom"))
    caching_gateway = provider in ("openai-compatible", "opencode-go")
    if not custom and (apiType is not _UNSET or authMode is not _UNSET
                       or reasoningEffort is not _UNSET
                       or thinkingFormat is not _UNSET or replayReasoning is not _UNSET
                       or reasoningKey is not _UNSET
                       or (caching is not _UNSET and not caching_gateway)
                       or (vision is not _UNSET and not caching_gateway)):
        raise ValueError("apiType, authMode, reasoningEffort, thinkingFormat, "
                         "replayReasoning, reasoningKey, caching, and vision are only "
                         "configurable for custom gateways")
    if caching is not _UNSET and caching is not None and not isinstance(caching, dict):
        raise ValueError("caching must be a model map")
    if vision is not _UNSET and vision is not None and not isinstance(vision, dict):
        raise ValueError("vision must be a model map")
    if (replayReasoning is not _UNSET and replayReasoning is not None
            and not isinstance(replayReasoning, (bool, dict))):
        raise ValueError("replayReasoning must be a boolean or a model map")
    if apiType is not _UNSET and apiType not in (None, "", "chat_completions", "responses"):
        raise ValueError("apiType must be chat_completions or responses")
    if authMode is not _UNSET and authMode not in (None, "", "bearer", "none"):
        raise ValueError("authMode must be bearer or none")
    # thinkingFormat is a scalar dialect string (legacy) OR a per-model map
    # ({model_id: fmt, "*": default}); either way every declared format is
    # validated up front so a bad value never reaches the record.
    if thinkingFormat is not _UNSET and thinkingFormat not in (None, ""):
        if isinstance(thinkingFormat, str):
            if thinkingFormat.strip() not in _THINKING_FORMATS:
                raise ValueError("thinkingFormat must be openai, openrouter, or zai")
        elif isinstance(thinkingFormat, dict):
            for fmt in thinkingFormat.values():
                if fmt in (None, ""):
                    continue  # a blank row value clears that model's row
                if not isinstance(fmt, str) or fmt.strip() not in _THINKING_FORMATS:
                    raise ValueError("thinkingFormat must be openai, openrouter, or zai")
        else:
            raise ValueError("thinkingFormat must be a model map or format string")
    records = _all_records()
    rec = dict(records.get(provider) or {})
    for key, val in (("apiKey", apiKey), ("token", token), ("baseUrl", baseUrl),
                     ("apiType", apiType), ("authMode", authMode)):
        if val is _UNSET:
            continue
        val = (val or "").strip() if isinstance(val, str) else val
        if val:
            rec[key] = val
        else:
            rec.pop(key, None)
    # reasoningEffort is a stored boolean capability toggle (not a credential):
    # a truthy value opts the custom gateway into OpenAI's reasoning extension;
    # anything falsy clears it back to the default-off.
    if reasoningEffort is not _UNSET:
        if reasoningEffort:
            rec["reasoningEffort"] = True
        else:
            rec.pop("reasoningEffort", None)
    # vision is a per-model tri-state map ({model_id: bool, "*" = default
    # row}): True opts the model into multimodal image_url input, False is an
    # explicit off (beats the "*" row), None/absent removes the row (inherit).
    if vision is not _UNSET:
        stored = _normalize_bool_map(rec.get("vision"))
        for model_id, enabled in (vision or {}).items():
            if not isinstance(model_id, str):
                continue
            if enabled is None:
                stored.pop(model_id, None)
            elif isinstance(enabled, bool):
                stored[model_id] = enabled
        if stored:
            rec["vision"] = stored
        else:
            rec.pop("vision", None)
    # thinkingFormat is a per-model dialect map ({model_id: "openai"|"openrouter"|"zai",
    # "*" = default row}). None/"" clears it entirely; a scalar string is the
    # legacy shape and lands on the "*" row. A dict merges: a valid format sets
    # that model's row, a blank ("" / null) value removes it — and any legacy
    # scalar already on the record is migrated to map form on this write.
    if thinkingFormat is not _UNSET:
        if thinkingFormat is None or thinkingFormat == "":
            rec.pop("thinkingFormat", None)
        else:
            stored = _normalize_thinking_map(rec.get("thinkingFormat"))
            incoming = ({"*": thinkingFormat.strip()} if isinstance(thinkingFormat, str)
                        else thinkingFormat)
            for mid, fmt in incoming.items():
                if not isinstance(mid, str):
                    continue
                f = fmt.strip() if isinstance(fmt, str) else fmt
                if f in _THINKING_FORMATS:
                    stored[mid] = f
                else:
                    stored.pop(mid, None)
            if stored:
                rec["thinkingFormat"] = stored
            else:
                rec.pop("thinkingFormat", None)
    # replayReasoning is a per-model tri-state map ({model_id: bool, "*" =
    # default row}, same shape as vision/caching): True opts the model into
    # replaying captured reasoning on tool-call messages, False is an explicit
    # off (beats the "*" row), None/absent removes the row (inherit). A legacy
    # scalar bool lands on the "*" row.
    if replayReasoning is not _UNSET:
        stored = _normalize_bool_map(rec.get("replayReasoning"))
        incoming = ({"*": replayReasoning} if isinstance(replayReasoning, bool)
                    else (replayReasoning or {}))
        for model_id, enabled in incoming.items():
            if not isinstance(model_id, str):
                continue
            if enabled is None:
                stored.pop(model_id, None)
            elif isinstance(enabled, bool):
                stored[model_id] = enabled
        if stored:
            rec["replayReasoning"] = stored
        else:
            rec.pop("replayReasoning", None)
    # reasoningKey is a write-only secret (like apiKey): providing it is the
    # consent to store plaintext chain-of-thought (DeepSeek/Kimi), encrypted at
    # rest under a scrypt-derived key + per-provider random salt. Omitting it
    # preserves; an explicit "" clears the key AND purges the encrypted plaintext
    # sidecars; a new/changed value regenerates the salt and purges stale rows.
    if reasoningKey is not _UNSET:
        import os as _os
        from storage import reasoning_store
        new_val = reasoningKey.strip() if isinstance(reasoningKey, str) else reasoningKey
        if not new_val:
            if rec.pop("reasoningKey", None) is not None:
                rec.pop("reasoningSalt", None)
                reasoning_store.purge_encrypted_plaintext()
        else:
            if not isinstance(new_val, str) or len(new_val) < 8:
                raise ValueError("reasoningKey must be at least 8 characters")
            if new_val != rec.get("reasoningKey"):
                rec["reasoningKey"] = new_val
                rec["reasoningSalt"] = _os.urandom(16).hex()
                reasoning_store.purge_encrypted_plaintext()
    # caching is a per-model tri-state map ({model_id: bool, "*" = default
    # row}): True opts that model into prompt caching — the wire mechanism
    # (prompt_cache_key vs cache_control) is chosen by the model's protocol in
    # resolve_target, so the human never picks between provider dialects.
    # False is an explicit off (beats the "*" row); None/absent removes the row.
    if caching is not _UNSET:
        # Normalize any legacy per-flag dict values already on the record to
        # clean booleans, so a save migrates the stored map forward.
        stored = _normalize_bool_map(rec.get("caching"))
        for model_id, enabled in (caching or {}).items():
            if not isinstance(model_id, str):
                continue
            if enabled is None:
                stored.pop(model_id, None)
            elif isinstance(enabled, bool):
                stored[model_id] = enabled
        if stored:
            rec["caching"] = stored
        else:
            rec.pop("caching", None)
    records[provider] = rec
    settings.set(_SETTINGS_KEY, records)


def supports_reasoning_effort(provider: str) -> bool:
    """Whether ``reasoning_effort`` should be forwarded to this provider.

    Built-in remote providers advertise the capability via their catalog def
    (defaulting on — unchanged forwarding behavior). The custom
    OpenAI-compatible provider defaults OFF: "OpenAI-compatible" only guarantees
    the base protocol, so the operator must explicitly toggle the capability on
    (stored in the provider record) once they've confirmed their gateway honors
    it. The local upstream (no provider) never forwards it — handled by callers.
    """
    defn = PROVIDER_DEFS.get(provider) or {}
    if defn.get("custom"):
        return bool(get_record(provider).get("reasoningEffort"))
    return bool(defn.get("reasoningEffort", True))


def supports_vision(provider: str, model: str) -> bool:
    """Whether this provider+model can accept multimodal image input — resolved
    PER MODEL either way.

    Built-in providers advertise it via a ``vision`` catalog map with the same
    ``{exact, prefixes, default}`` shape as ``windows`` — an exact model row wins,
    then a family-prefix rule, then the provider default (absent map ⇒ off). The
    custom gateways (openai-compatible, opencode-go) don't guarantee image_url
    content, so they default OFF and the operator opts models in via a per-model
    ``vision`` record map (``{model_id: bool, "*": default}``) — mirroring
    ``caching_enabled``. The local upstream (no provider / ``"local"``) is text-only
    and always False.
    """
    if not provider or provider == "local":
        return False
    defn = PROVIDER_DEFS.get(provider) or {}
    if not defn:
        return False
    if provider in ("openai-compatible", "opencode-go"):
        vision = _normalize_bool_map(get_record(provider).get("vision"))
        # Exact row wins — an explicit False row means off even when "*" is on.
        if model in vision:
            return vision[model]
        return bool(vision.get("*"))
    vision = defn.get("vision") or {}
    exact = (vision.get("exact") or {}).get(model)
    if isinstance(exact, bool):
        return exact
    for prefix, enabled in (vision.get("prefixes") or []):
        if model and model.startswith(prefix):
            return bool(enabled)
    return bool(vision.get("default", False))


def caching_enabled(provider: str, model: str) -> bool:
    """Whether the operator opted this custom-gateway model into prompt
    caching: exact model row (an explicit False row means off even when the
    ``"*"`` default is on), then the ``"*"`` row, else off."""
    if provider not in ("openai-compatible", "opencode-go"):
        return False
    caching = _normalize_bool_map(get_record(provider).get("caching"))
    if model in caching:
        return caching[model]
    return bool(caching.get("*"))


def thinking_format_for(provider: str, model: str) -> str | None:
    """The provider-compat reasoning dialect for a custom-gateway model: exact
    model row, then the ``"*"`` default row, else None (mirrors caching_enabled).
    A legacy stored scalar is read as the ``"*"`` row. An absent custom row is
    left unset for backwards-compatible storage and interpreted as the standard
    ``openai`` dialect by egress."""
    if not PROVIDER_DEFS.get(provider, {}).get("custom"):
        return None
    fmt_map = _normalize_thinking_map(get_record(provider).get("thinkingFormat"))
    return fmt_map.get(model) or fmt_map.get("*") or None


def replay_reasoning_for(provider: str, model: str) -> bool:
    """Whether captured reasoning is replayed on tool-call continuations for
    this custom-gateway model: exact model row (an explicit False row means
    off even when the ``"*"`` default is on), then the ``"*"`` row, else off.
    A legacy stored scalar ``True`` reads as the ``"*"`` row. Custom provider
    only."""
    if not PROVIDER_DEFS.get(provider, {}).get("custom"):
        return False
    replay = _normalize_bool_map(get_record(provider).get("replayReasoning"))
    if model in replay:
        return replay[model]
    return bool(replay.get("*"))


def get_oauth(provider: str) -> dict:
    """The OAuth blob (refresh token + cached key/account id) for a provider, if
    it signs in via OAuth (ChatGPT). Empty when not connected."""
    o = get_record(provider).get("oauth")
    return o if isinstance(o, dict) else {}


def set_oauth(provider: str, blob: dict | None) -> None:
    """Store (or clear, with ``None``) a provider's OAuth blob without touching its
    other credential fields."""
    records = _all_records()
    rec = dict(records.get(provider) or {})
    if blob:
        rec["oauth"] = blob
    else:
        rec.pop("oauth", None)
    records[provider] = rec
    settings.set(_SETTINGS_KEY, records)


# Providers whose subscription mode is an OAuth sign-in (not a pasted token).
# Anthropic is excluded because its subscription is a *pasted* `claude setup-token`
# (sk-ant-oat01-…), not a PKCE sign-in — so the UI shows a token field, not a button.
OAUTH_SUBSCRIPTION = {"openai"}


def catalog() -> list[dict]:
    """Public provider catalog for the UI — definitions + masked credential
    state (whether a key/token/login is set, never the secret itself)."""
    out = []
    for pid, defn in PROVIDER_DEFS.items():
        rec = get_record(pid)
        oauth = get_oauth(pid)
        # OpenCode Go multiplexes several wire protocols behind one key; expose
        # the per-model protocol kind (the single source of truth is
        # ``_opencode_go_kind``) so the UI never re-derives it from a regex.
        model_kinds: dict[str, str] = {}
        if pid == "opencode-go":
            ids = set(defn["models"])
            ids |= {mid for (p, mid) in _discovered_windows if p == pid}
            ids |= {mid for (p, mid) in _cache_seen if p == pid}
            ids |= set((rec.get("caching") or {}).keys())
            ids.discard("*")
            model_kinds = {mid: _opencode_go_kind(mid) for mid in ids}
        out.append({
            "id": pid,
            "label": defn["label"],
            "kind": defn["kind"],
            "baseUrl": defn["baseUrl"],
            "models": defn["models"],
            "subscriptionModels": defn.get("subscriptionModels") or [],
            "byok": defn["byok"],
            "subscription": defn["subscription"],
            "oauthLogin": pid in OAUTH_SUBSCRIPTION,  # subscription = sign-in, not a token
            "hasKey": bool(rec.get("apiKey")),
            "hasToken": bool(rec.get("token")),
            "hasOauth": bool(oauth.get("refreshToken")),
            "account": oauth.get("accountId") or "",
            "baseUrlOverride": rec.get("baseUrl") or "",
            "custom": bool(defn.get("custom")),
            "modelDiscovery": bool(defn.get("modelDiscovery")),
            "reasoningEffort": supports_reasoning_effort(pid),
            # Per-model image-input map for the custom gateways (built-in providers
            # declare vision in their catalog def, not per record — echoed empty here).
            "vision": (_normalize_bool_map(rec.get("vision"))
                       if pid in ("openai-compatible", "opencode-go") else {}),
            # Echo the per-model dialect map (normalized to map form; a legacy
            # stored scalar surfaces as its "*" row).
            "thinkingFormat": _normalize_thinking_map(rec.get("thinkingFormat")),
            # Echo the per-model replay map (normalized; a legacy stored
            # scalar surfaces as its "*" row).
            "replayReasoning": _normalize_bool_map(rec.get("replayReasoning")),
            # Whether a reasoning encryption key is configured (custom gateway
            # only) — the key value and its salt are NEVER exposed.
            "hasReasoningKey": (bool(rec.get("reasoningKey"))
                                if pid == "openai-compatible" else False),
            "caching": (_normalize_bool_map(rec.get("caching"))
                        if pid in ("openai-compatible", "opencode-go") else {}),
            "cacheSeen": {
                mid: dict(seen) for (p, mid), seen in _cache_seen.items() if p == pid
            },
            "modelKinds": model_kinds,
            "credentialLabel": defn.get("credentialLabel") or "API key",
            "credentialHint": defn.get("credentialHint") or "",
            "apiTypes": defn.get("apiTypes") or [],
            "apiType": rec.get("apiType") or defn.get("defaultApiType") or "",
            "authModes": defn.get("authModes") or [],
            "authMode": rec.get("authMode") or defn.get("defaultAuthMode") or "",
        })
    return out


# --------------------------------------------------------------------------- #
# Per-agent target resolution
# --------------------------------------------------------------------------- #

async def resolve_target(agent: dict | None,
                         requested_model: str | None = None) -> tuple[str, dict | None]:
    """Resolve an agent to ``(model, target)`` for ``loop.run``.

    ``target is None`` means *use the local upstream* (the default, unchanged
    behavior). Otherwise ``target`` is a dict the egress layer routes on:
    ``{provider, kind, base_url, api_key, auth_mode}``.
    """
    agent = agent or {}
    source = agent.get("modelSource") or "local"
    provider = (agent.get("provider") or "").strip()

    # Local (default): preserve the existing resolution chain exactly.
    if source == "local" or not is_provider(provider):
        model = requested_model or agent.get("model") or await resolve_model(None)
        return model, None

    defn = PROVIDER_DEFS[provider]
    rec = get_record(provider)
    auth_mode = "subscription" if source == "subscription" else "apikey"
    kind = defn["kind"]
    if defn.get("custom"):
        api_type = rec.get("apiType") or defn["defaultApiType"]
        kind = "responses" if api_type == "responses" else "openai"
        configured_auth = rec.get("authMode") or defn["defaultAuthMode"]
        auth_mode = "none" if configured_auth == "none" else "apikey"
    # Saved models remain authoritative for ordinary built-in providers. Custom
    # gateways and OpenCode Go have discovered lists, so Chat can pick one for
    # just this run without changing the assistant's default.
    default_models = (defn.get("subscriptionModels") if source == "subscription"
                      else defn["models"])
    model = ((requested_model if (defn.get("custom") or defn.get("modelDiscovery"))
              and requested_model else None)
             or agent.get("model") or (default_models[0] if default_models else ""))

    if provider == "opencode-go":
        kind = _opencode_go_kind(model)

    if auth_mode == "subscription" and provider == "openai":
        raise ValueError("ChatGPT subscription sign-in is not available in this build — use an API key")

    cred = (rec.get("token") if auth_mode == "subscription" else rec.get("apiKey"))
    if auth_mode == "none":
        cred = None
    base_url = (rec.get("baseUrl") or defn["baseUrl"]).rstrip("/")
    target = {
        "provider": provider,
        "kind": kind,
        "base_url": base_url,
        "api_key": cred,
        "auth_mode": auth_mode,
        "reasoning_effort": supports_reasoning_effort(provider),
        # Provider-compat reasoning dialect (resolved to the scalar for THIS model,
        # so the egress interface stays scalar) + replay toggle. Harmless to read
        # from the record for any provider — only custom records ever hold these.
        "thinking_format": thinking_format_for(provider, model),
        "replay_reasoning": replay_reasoning_for(provider, model),
    }
    # Responses-path reasoning replay: rehydrate the session's stored reasoning
    # sidecars (encrypted items) into the next request's input. The loop only
    # loads sidecars when this flag is set. Scoped to the custom Responses gateway
    # with the toggle on — a chat-kind target would load sidecars that _openai_body
    # then drops, and only responses egress reinjects them.
    target["stateless_reasoning_replay"] = (
        provider == "openai-compatible" and kind == "responses"
        and replay_reasoning_for(provider, model))
    # Plaintext reasoning (DeepSeek/Kimi) is only captured + persisted when a
    # reasoning encryption key is configured — providing the key IS the consent.
    # Stamp the key-presence flag egress reads (capture filter) and derive the
    # 32-byte key the loop hands the store for at-rest encryption. Never logged.
    if provider == "openai-compatible" and kind == "responses":
        reasoning_key = rec.get("reasoningKey")
        salt_hex = rec.get("reasoningSalt")
        target["retain_plaintext_reasoning"] = bool(reasoning_key)
        if reasoning_key and salt_hex:
            from storage import reasoning_crypto
            target["reasoning_enc_key"] = reasoning_crypto.derive_key(
                reasoning_key, bytes.fromhex(salt_hex))
        else:
            target["reasoning_enc_key"] = None
    if provider in ("openai-compatible", "opencode-go"):
        # One human-facing toggle per model; the wire mechanism follows the
        # model's protocol: Anthropic-shaped models take cache_control markers,
        # OpenAI-shaped ones take the prompt_cache_key affinity hint.
        enabled = caching_enabled(provider, model)
        target["prompt_cache_key"] = enabled and kind != "anthropic"
        target["cache_control"] = enabled and kind == "anthropic"
    elif provider == "openai":
        target["prompt_cache_key"] = True
        target["prompt_cache_retention"] = "24h"
        target["cache_control"] = False
    elif provider == "anthropic":
        target["prompt_cache_key"] = False
        target["cache_control"] = True
    else:
        target["prompt_cache_key"] = False
        target["cache_control"] = False
    # Anthropic's one-hour tier is deliberately per-agent and opt-in. Custom
    # Anthropic-shaped models only receive it when their caching toggle is on.
    if (target.get("cache_control") and auth_mode == "apikey"
            and agent.get("cacheTtl") == "1h"):
        target["cache_ttl"] = "1h"
    return model, target
