"""Hermetic acceptance coverage for prompt caching phases 1 and 2."""

import asyncio

import pytest

import config
from gateway import egress, providers
from storage import agent_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    providers._cache_seen.clear()
    return tmp_path


def _resolve(agent):
    return asyncio.run(providers.resolve_target(agent))


def test_caching_enabled_precedence_and_custom_storage(home):
    providers.set_record("opencode-go", caching={"*": True})
    assert providers.caching_enabled("opencode-go", "kimi-k3") is True
    assert providers.caching_enabled("opencode-go", "minimax-m3") is True
    providers.set_record("openai-compatible", caching={"model-a": True})
    assert providers.caching_enabled("openai-compatible", "model-a") is True
    assert providers.caching_enabled("openai-compatible", "model-b") is False
    assert providers.get_record("openai-compatible")["caching"] == {"model-a": True}
    providers.set_record("openai-compatible", caching={"model-a": False})
    # False is an explicit off (kept), not a delete — tri-state rows.
    assert providers.get_record("openai-compatible")["caching"] == {"model-a": False}
    assert providers.caching_enabled("openai-compatible", "model-a") is False
    providers.set_record("openai-compatible", caching={"model-a": None})
    assert "caching" not in providers.get_record("openai-compatible")
    assert providers.caching_enabled("anthropic", "claude") is False


def test_legacy_caching_dict_shape_is_normalized_on_read_and_save(home):
    # Real config files still carry the earlier two-flag dict shape. It must be
    # read as a boolean (any truthy flag = enabled) rather than "any non-empty
    # dict is truthy", and a save rewrites the stored map forward to booleans.
    from storage import settings
    settings.set("providers", {"openai-compatible": {"caching": {
        "*": {"promptCacheKey": True},
        "m": {"promptCacheKey": False},
    }}})
    assert providers.caching_enabled("openai-compatible", "anything") is True
    # Legacy all-false dict on an exact row now reads as an explicit off
    # (tri-state), not as "no row → inherit the default".
    assert providers.caching_enabled("openai-compatible", "m") is False

    settings.set("providers", {"openai-compatible": {"caching": {
        "m": {"promptCacheKey": False, "cacheControl": False},
    }}})
    # Legacy all-false dict reads as an explicit off row.
    assert providers.caching_enabled("openai-compatible", "m") is False

    # A save migrates the legacy shape to clean booleans (False rows kept).
    settings.set("providers", {"openai-compatible": {"caching": {
        "keep": {"promptCacheKey": True},
        "off": {"promptCacheKey": False},
    }}})
    providers.set_record("openai-compatible", caching={"model-a": True})
    assert providers.get_record("openai-compatible")["caching"] == {
        "keep": True, "off": False, "model-a": True}


def test_caching_enabled_kind_split_on_resolve(home):
    providers.set_record("opencode-go", apiKey="k", caching={"*": True})
    _, target = _resolve({"modelSource": "byok", "provider": "opencode-go",
                          "model": "kimi-k3"})
    assert target["prompt_cache_key"] is True and target["cache_control"] is False
    _, target = _resolve({"modelSource": "byok", "provider": "opencode-go",
                          "model": "minimax-m3"})
    assert target["prompt_cache_key"] is False and target["cache_control"] is True


@pytest.mark.parametrize("provider", ["openai", "anthropic", "deepseek"])
def test_official_providers_reject_caching(home, provider):
    with pytest.raises(ValueError):
        providers.set_record(provider, caching={"*": True})


def test_resolve_target_stamps_provider_flags(home):
    providers.set_record("openai", apiKey="k")
    assert _resolve({"modelSource": "byok", "provider": "openai", "model": "gpt-5"})[1][
        "prompt_cache_key"] is True
    providers.set_record("anthropic", apiKey="k")
    _, target = _resolve({"modelSource": "byok", "provider": "anthropic", "model": "claude"})
    assert target["prompt_cache_key"] is False and target["cache_control"] is True
    providers.set_record("deepseek", apiKey="k")
    _, target = _resolve({"modelSource": "byok", "provider": "deepseek", "model": "deepseek-chat"})
    assert target["prompt_cache_key"] is False and target["cache_control"] is False


def test_resolve_target_stamps_anthropic_ttl_only_when_opted_in(home):
    providers.set_record("anthropic", apiKey="k")
    _, target = _resolve({"modelSource": "byok", "provider": "anthropic",
                          "model": "claude", "cacheTtl": "1h"})
    assert target["cache_ttl"] == "1h"


def test_agent_cache_ttl_round_trip(home):
    agent = agent_store.create_agent({"name": "Cache bot", "cacheTtl": "1h"})
    assert agent["cacheTtl"] == "1h"
    updated = agent_store.update_agent(agent["id"], {"cacheTtl": ""})
    assert updated["cacheTtl"] == ""


def test_catalog_cache_telemetry_is_normalized_and_key_free(home):
    providers.note_cache_usage("openai-compatible", "m", 12, 3)
    item = next(p for p in providers.catalog() if p["id"] == "openai-compatible")
    assert item["cacheSeen"] == {"m": {"cacheRead": 12, "cacheWrite": 3}}
    assert "prompt_cache_key" not in repr(item)


def test_catalog_exposes_opencode_go_model_kinds(home):
    # The catalog is the single source of truth for a model's protocol kind
    # (backed by _opencode_go_kind) so the UI never re-derives it from a regex.
    providers.set_record("opencode-go", caching={
        "minimax-m3": True, "kimi-k3": True, "gpt-5.6-luna": True, "*": True})
    item = next(p for p in providers.catalog() if p["id"] == "opencode-go")
    kinds = item["modelKinds"]
    assert kinds["minimax-m3"] == "anthropic"
    assert kinds["kimi-k3"] == "openai"
    assert kinds["gpt-5.6-luna"] == "responses"
    assert "*" not in kinds
    # Official/non-multiplexed providers carry no per-model kind map.
    other = next(p for p in providers.catalog() if p["id"] == "anthropic")
    assert other["modelKinds"] == {}


def test_anthropic_markers_and_off_byte_compatibility():
    payload = {"model": "claude-sonnet-4-6", "messages": [
        {"role": "system", "content": "stable\n\nclock"},
        {"role": "user", "content": "hello"},
    ]}
    off_a = egress._to_anthropic_request(payload, {"cache_control": False})
    off_b = egress._to_anthropic_request(payload, {})
    assert off_a == off_b and off_a["system"] == "stable\n\nclock"
    marked = egress._to_anthropic_request(payload, {"cache_control": True})
    assert marked["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked["system"][1]
    assert marked["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_subscription_markers_keep_preamble_unmarked():
    req = egress._to_anthropic_request(
        {"model": "claude", "messages": [
            {"role": "system", "content": "stable instructions\n\nclock block"},
        ]},
        {"auth_mode": "subscription", "cache_control": True},
    )
    assert isinstance(req["system"], list)
    assert req["system"][0] == {"type": "text", "text": egress._CLAUDE_CODE_PREFIX}
    assert "cache_control" not in req["system"][0]
    assert req["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert req["system"][1]["text"] == "stable instructions"
    assert req["system"][2] == {"type": "text", "text": "clock block"}


def test_prompt_cache_key_body_builders_and_codex_reasoning():
    payload = {"model": "m", "messages": [], "prompt_cache_key": "ves-key",
               "reasoning_effort": "high"}
    assert egress._openai_body(payload, {"provider": "openai", "prompt_cache_key": True})[
        "prompt_cache_key"] == "ves-key"
    assert "prompt_cache_key" not in egress._openai_body(
        payload, {"provider": "openai-compatible", "prompt_cache_key": False})
    req = egress._to_responses_request(payload, {"prompt_cache_key": True})
    assert req["prompt_cache_key"] == "ves-key" and payload["reasoning_effort"] == "high"
    assert "prompt_cache_key" not in egress._to_responses_request(
        payload, {"prompt_cache_key": False})
    codex = egress._to_codex_request(payload)
    assert codex["prompt_cache_key"] == "ves-key" and codex["store"] is False


def test_official_retention_is_target_driven_and_custom_gateways_stay_clean():
    payload = {"model": "m", "messages": [], "prompt_cache_key": "ves-key"}
    assert egress._openai_body(payload, {"provider": "openai", "prompt_cache_key": True,
                                         "prompt_cache_retention": "24h"})[
        "prompt_cache_retention"] == "24h"
    assert "prompt_cache_retention" not in egress._openai_body(
        payload, {"provider": "openai-compatible", "prompt_cache_key": True})
    assert egress._to_responses_request(
        payload, {"prompt_cache_key": True, "prompt_cache_retention": "24h"})[
        "prompt_cache_retention"] == "24h"
    assert egress._to_codex_request(
        payload, {"prompt_cache_retention": "24h"})["prompt_cache_retention"] == "24h"


def test_anthropic_extended_ttl_is_opt_in_and_target_driven():
    payload = {"model": "claude", "messages": [
        {"role": "system", "content": "stable\n\nclock"},
        {"role": "user", "content": "hello"},
    ]}
    off = egress._to_anthropic_request(payload, {"cache_control": True})
    on = egress._to_anthropic_request(payload, {"cache_control": True, "cache_ttl": "1h"})
    assert off["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert on["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert on["messages"][-1]["content"][0]["cache_control"]["ttl"] == "1h"


def test_custom_empty_cache_map_has_no_new_body_fields():
    payload = {"model": "m", "messages": [], "prompt_cache_key": "ves-key"}
    target = {"provider": "openai-compatible", "prompt_cache_key": False}
    assert "prompt_cache_key" not in egress._openai_body(payload, target)
    assert "prompt_cache_key" not in egress._to_responses_request(payload, target)


def test_anthropic_usage_is_inclusive(monkeypatch):
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"cache_read_input_tokens":7,"cache_creation_input_tokens":2}}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3,"cache_creation_input_tokens":4}}',
        'data: {"type":"message_stop"}',
    ]

    class Resp:
        status_code = 200

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def aiter_lines(self):
            for line in lines: yield line

    class Client:
        def __init__(self, **kwargs): pass
        def stream(self, *args, **kwargs): return Resp()
        async def aclose(self): pass

    monkeypatch.setattr(egress.httpx, "AsyncClient", Client)
    chunks = asyncio.run(_drain(egress._anthropic_stream(
        {"model": "claude", "messages": []}, {"api_key": "k", "auth_mode": "apikey",
         "base_url": "https://api.anthropic.com"})))
    usage = chunks[-1]["usage"]
    assert usage["prompt_tokens"] == 16 and usage["cacheRead"] == 7 and usage["cacheWrite"] == 4


def test_responses_usage_cache_write_is_nested(monkeypatch):
    # The LLM Gateway nests cache_write_tokens under input_tokens_details on the
    # Responses API (alongside cached_tokens), not at usage top-level. Egress must
    # read it there or cacheWrite silently reports 0 while cacheRead is correct.
    lines = [
        'data: {"type":"response.completed","response":{"usage":{'
        '"input_tokens":100,"output_tokens":20,'
        '"input_tokens_details":{"cached_tokens":80,"cache_write_tokens":12}}}}',
        "data: [DONE]",
    ]

    class Resp:
        status_code = 200

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def aiter_lines(self):
            for line in lines: yield line

    class Client:
        def __init__(self, **kwargs): pass
        def stream(self, *args, **kwargs): return Resp()
        async def aclose(self): pass

    monkeypatch.setattr(egress.httpx, "AsyncClient", Client)
    chunks = asyncio.run(_drain(egress._responses_request_stream(
        {"model": "m", "input": []}, "http://x/v1/responses", {}, None)))
    usage = chunks[-1]["usage"]
    assert usage["cacheRead"] == 80 and usage["cacheWrite"] == 12


async def _drain(gen):
    return [item async for item in gen]
