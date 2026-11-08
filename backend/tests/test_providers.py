"""External providers — catalog/credentials, per-agent target resolution, and the
egress translators (OpenAI-compatible + Anthropic), all hermetic (no network)."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from gateway import egress, providers
from gateway.upstream import UpstreamError


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


# --- credentials + catalog (secrets are write-only) ------------------------- #

def test_catalog_masks_secrets(home):
    providers.set_record("deepseek", apiKey="sk-secret-123")
    cat = {p["id"]: p for p in providers.catalog()}
    assert cat["deepseek"]["hasKey"] is True
    assert cat["deepseek"]["hasToken"] is False
    # The secret value is never present in the catalog payload.
    assert "sk-secret-123" not in repr(cat)
    assert cat["deepseek"]["models"][0] == "deepseek-chat"


def test_anthropic_subscription_offered_as_pasted_token(home):
    # Claude subscription (claude setup-token) is enabled, but as a pasted token —
    # so the catalog advertises subscription without an OAuth sign-in button.
    cat = {p["id"]: p for p in providers.catalog()}
    assert cat["anthropic"]["subscription"] is True
    assert cat["anthropic"]["oauthLogin"] is False


def test_openai_subscription_catalog_defaults_to_luna(home):
    cat = {p["id"]: p for p in providers.catalog()}
    assert cat["openai"]["subscriptionModels"] == [
        "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.5",
        "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark",
    ]


def test_custom_provider_catalog_exposes_protocol_and_auth_without_secrets(home):
    providers.set_record("openai-compatible", apiKey="local-secret",
                         baseUrl="http://127.0.0.1:9000/v1",
                         apiType="responses", authMode="bearer")
    custom = {p["id"]: p for p in providers.catalog()}["openai-compatible"]
    assert custom["custom"] is True and custom["subscription"] is False
    # The agent editor gates every discovery affordance on this flag — without
    # it the "Refresh models" button is a silent no-op.
    assert custom["modelDiscovery"] is True
    assert custom["apiTypes"] == ["chat_completions", "responses"]
    assert custom["apiType"] == "responses" and custom["authMode"] == "bearer"
    assert custom["baseUrlOverride"] == "http://127.0.0.1:9000/v1"
    assert custom["hasKey"] is True and "local-secret" not in repr(custom)


def test_custom_reasoning_effort_toggle_defaults_off_and_round_trips(home):
    # Default-off: "OpenAI-compatible" doesn't guarantee the reasoning extension.
    assert providers.supports_reasoning_effort("openai-compatible") is False
    custom = {p["id"]: p for p in providers.catalog()}["openai-compatible"]
    assert custom["reasoningEffort"] is False
    # Operator opts a known-good gateway in; catalog + capability reflect it.
    providers.set_record("openai-compatible", reasoningEffort=True)
    assert providers.supports_reasoning_effort("openai-compatible") is True
    custom = {p["id"]: p for p in providers.catalog()}["openai-compatible"]
    assert custom["reasoningEffort"] is True
    # Clearing returns to the default-off.
    providers.set_record("openai-compatible", reasoningEffort=False)
    assert providers.supports_reasoning_effort("openai-compatible") is False
    assert "reasoningEffort" not in providers.get_record("openai-compatible")


def test_built_in_providers_forward_reasoning_effort_by_default(home):
    # Built-in remotes advertise the capability (unchanged forwarding behavior).
    assert providers.supports_reasoning_effort("openai") is True
    assert providers.supports_reasoning_effort("anthropic") is True
    assert providers.supports_reasoning_effort("opencode-go") is True


def test_reasoning_effort_toggle_is_custom_only(home):
    with pytest.raises(ValueError, match="only configurable"):
        providers.set_record("openai", reasoningEffort=True)


def test_supports_vision_built_in_per_model(home):
    # openai: gpt-4o/gpt-5 families see images; the o-series text ids do not.
    assert providers.supports_vision("openai", "gpt-4o") is True
    assert providers.supports_vision("openai", "gpt-5.4") is True
    assert providers.supports_vision("openai", "o4-mini") is False
    # anthropic: every current Claude model is vision-capable.
    assert providers.supports_vision("anthropic", "claude-opus-4-8") is True
    # deepseek declares no vision map → default off; local is always off.
    assert providers.supports_vision("deepseek", "deepseek-chat") is False
    assert providers.supports_vision("local", "anything") is False


def test_custom_vision_map_defaults_off_and_round_trips_per_model(home):
    # Default-off for every model, and the catalog echoes an empty map.
    assert providers.supports_vision("openai-compatible", "gpt-4o-local") is False
    custom = {p["id"]: p for p in providers.catalog()}["openai-compatible"]
    assert custom["vision"] == {}
    # Opt one model in; a different model stays off (genuinely per-model).
    providers.set_record("openai-compatible", vision={"gpt-4o-local": True})
    assert providers.supports_vision("openai-compatible", "gpt-4o-local") is True
    assert providers.supports_vision("openai-compatible", "text-only-model") is False
    # The "*" default row applies to any un-listed model.
    providers.set_record("openai-compatible", vision={"*": True})
    assert providers.supports_vision("openai-compatible", "text-only-model") is True
    custom = {p["id"]: p for p in providers.catalog()}["openai-compatible"]
    assert custom["vision"].get("gpt-4o-local") is True and custom["vision"].get("*") is True
    # A false value stores an explicit off — it beats the "*" row.
    providers.set_record("openai-compatible", vision={"gpt-4o-local": False})
    assert providers.supports_vision("openai-compatible", "gpt-4o-local") is False
    # null removes the row, back to inheriting "*".
    providers.set_record("openai-compatible", vision={"gpt-4o-local": None})
    assert providers.supports_vision("openai-compatible", "gpt-4o-local") is True  # via "*"
    providers.set_record("openai-compatible", vision={"*": False})
    assert providers.supports_vision("openai-compatible", "gpt-4o-local") is False


def test_opencode_go_supports_per_model_vision(home):
    providers.set_record("opencode-go", vision={"qwen3.8-max": True})
    assert providers.supports_vision("opencode-go", "qwen3.8-max") is True
    assert providers.supports_vision("opencode-go", "deepseek-v4-flash") is False


def test_vision_map_is_gateway_only(home):
    with pytest.raises(ValueError, match="custom gateways"):
        providers.set_record("openai", vision={"*": True})


def test_resolve_target_stamps_reasoning_effort_capability(home):
    providers.set_record("deepseek", apiKey="k")
    _, target = _resolve({"modelSource": "byok", "provider": "deepseek", "model": "deepseek-chat"})
    assert target["reasoning_effort"] is True
    providers.set_record("openai-compatible", baseUrl="http://127.0.0.1:9000/v1", authMode="none")
    _, target = _resolve({"modelSource": "byok", "provider": "openai-compatible", "model": "m"})
    assert target["reasoning_effort"] is False


def test_opencode_go_is_a_byok_provider_with_model_discovery(home):
    providers.set_record("opencode-go", apiKey="go-secret")
    go = {p["id"]: p for p in providers.catalog()}["opencode-go"]
    assert go["byok"] is True and go["subscription"] is False
    assert go["modelDiscovery"] is True
    assert go["credentialLabel"] == "Go API key"
    assert go["hasKey"] is True and "go-secret" not in repr(go)


def test_set_record_clear_and_baseurl(home):
    providers.set_record("deepseek", apiKey="k", baseUrl="https://proxy/v1")
    assert providers.get_record("deepseek")["baseUrl"] == "https://proxy/v1"
    providers.set_record("deepseek", apiKey="")  # empty clears just the key
    rec = providers.get_record("deepseek")
    assert "apiKey" not in rec and rec["baseUrl"] == "https://proxy/v1"


def test_set_record_unknown_provider_rejected(home):
    with pytest.raises(ValueError):
        providers.set_record("nope", apiKey="x")
    with pytest.raises(ValueError, match="only configurable"):
        providers.set_record("deepseek", authMode="none")


# --- resolve_target --------------------------------------------------------- #

def _resolve(agent, requested=None):
    return asyncio.run(providers.resolve_target(agent, requested))


def test_resolve_local_default(home, monkeypatch):
    async def fake_active(_):
        return "active-local"
    monkeypatch.setattr(providers, "resolve_model", fake_active)
    model, target = _resolve({"modelSource": "local", "model": None})
    assert model == "active-local" and target is None


def test_resolve_byok_deepseek(home):
    providers.set_record("deepseek", apiKey="sk-ds")
    model, target = _resolve({"modelSource": "byok", "provider": "deepseek",
                              "model": "deepseek-chat"})
    assert model == "deepseek-chat"
    assert target["kind"] == "openai"
    assert target["base_url"] == "https://api.deepseek.com/v1"
    assert target["api_key"] == "sk-ds"
    assert target["auth_mode"] == "apikey"


def test_resolve_byok_falls_back_to_default_model(home):
    providers.set_record("deepseek", apiKey="sk-ds")
    model, _ = _resolve({"modelSource": "byok", "provider": "deepseek", "model": None})
    assert model == "deepseek-chat"  # first catalog model


def test_resolve_subscription_uses_token(home):
    providers.set_record("anthropic", token="oauth-tok")
    model, target = _resolve({"modelSource": "subscription", "provider": "anthropic",
                              "model": "claude-sonnet-4-6"})
    assert target["kind"] == "anthropic"
    assert target["auth_mode"] == "subscription"
    assert target["api_key"] == "oauth-tok"


@pytest.mark.parametrize(("api_type", "kind"), [
    ("chat_completions", "openai"),
    ("responses", "responses"),
])
def test_resolve_custom_provider_supports_no_auth(home, api_type, kind):
    providers.set_record("openai-compatible", baseUrl="http://localhost:9000/v1",
                         apiType=api_type, authMode="none")
    model, target = _resolve({"modelSource": "byok", "provider": "openai-compatible",
                              "model": "local-gateway-model"})
    assert model == "local-gateway-model"
    expected = {
        "provider": "openai-compatible", "kind": kind,
        "base_url": "http://localhost:9000/v1", "api_key": None,
        "auth_mode": "none", "reasoning_effort": False,
        "thinking_format": None, "replay_reasoning": False,
        "stateless_reasoning_replay": False,
        "prompt_cache_key": False, "cache_control": False,
    }
    # The responses path also stamps the plaintext-reasoning gate (no key set).
    if kind == "responses":
        expected["retain_plaintext_reasoning"] = False
        expected["reasoning_enc_key"] = None
    assert target == expected


def test_stateless_reasoning_replay_scoped_to_custom_responses(home):
    # replayReasoning enables Responses sidecar replay only for the custom gateway
    # on the responses protocol; a chat_completions-kind target must stay off
    # (egress would load sidecars that _openai_body drops).
    providers.set_record("openai-compatible", baseUrl="http://localhost:9000/v1",
                         apiType="responses", authMode="none", replayReasoning=True)
    _m, t = _resolve({"modelSource": "byok", "provider": "openai-compatible",
                      "model": "local-gateway-model"})
    assert t["stateless_reasoning_replay"] is True
    # Same record, chat_completions protocol -> off (kind gate).
    providers.set_record("openai-compatible", apiType="chat_completions")
    _m, t2 = _resolve({"modelSource": "byok", "provider": "openai-compatible",
                       "model": "local-gateway-model"})
    assert t2["stateless_reasoning_replay"] is False
    # Toggle off -> off even on responses.
    providers.set_record("openai-compatible", apiType="responses", replayReasoning=False)
    _m, t3 = _resolve({"modelSource": "byok", "provider": "openai-compatible",
                       "model": "local-gateway-model"})
    assert t3["stateless_reasoning_replay"] is False


def test_resolve_custom_provider_honors_per_run_model_override(home):
    providers.set_record("openai-compatible", baseUrl="http://localhost:9000/v1",
                         apiType="chat_completions", authMode="none")
    model, _ = _resolve({"modelSource": "byok", "provider": "openai-compatible",
                         "model": "saved-default"}, "picked-for-this-chat")
    assert model == "picked-for-this-chat"


@pytest.mark.parametrize(("model", "kind"), [
    ("deepseek-v4-flash", "openai"),
    ("gpt-5.6-luna", "responses"),
    ("minimax-m3", "anthropic"),
    ("qwen3.8-max", "anthropic"),
])
def test_resolve_opencode_go_routes_each_model_protocol(home, model, kind):
    providers.set_record("opencode-go", apiKey="go-key")
    resolved, target = _resolve({"modelSource": "byok", "provider": "opencode-go",
                                 "model": "deepseek-v4-flash"}, model)
    assert resolved == model
    assert target["kind"] == kind
    assert target["base_url"] == "https://opencode.ai/zen/go/v1"
    assert target["api_key"] == "go-key" and target["auth_mode"] == "apikey"


def test_resolve_unknown_provider_is_local(home, monkeypatch):
    async def fake_active(_):
        return "active-local"
    monkeypatch.setattr(providers, "resolve_model", fake_active)
    model, target = _resolve({"modelSource": "byok", "provider": "bogus"})
    assert target is None and model == "active-local"


# --- egress: missing credential surfaces a clear error ---------------------- #

def test_stream_without_key_errors():
    async def run():
        target = {"provider": "deepseek", "kind": "openai", "base_url": "x",
                  "api_key": None, "auth_mode": "apikey"}
        async for _ in egress.stream({"model": "m", "messages": []}, target):
            pass
    with pytest.raises(UpstreamError) as e:
        asyncio.run(run())
    assert e.value.status_code == 400 and "API key" in e.value.body


# --- egress: transient connect blips are retried before streaming ----------- #

def _collect(agen):
    async def run():
        out = []
        async for c in agen:
            out.append(c)
        return out
    return asyncio.run(run())


async def _no_sleep(*_a, **_k):
    return None


def test_connect_retry_reconnects_before_first_chunk(monkeypatch):
    monkeypatch.setattr(egress.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def make():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("All connection attempts failed")
        yield {"choices": [{"delta": {"content": "hi"}}]}

    out = _collect(egress._with_connect_retry(make, "anthropic"))
    assert calls["n"] == 2                       # first attempt failed, retried
    assert out[0]["choices"][0]["delta"]["content"] == "hi"


def test_connect_retry_gives_up_after_max(monkeypatch):
    monkeypatch.setattr(egress.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def make():
        calls["n"] += 1
        raise httpx.ConnectError("nope")
        yield  # pragma: no cover — makes this an async generator

    with pytest.raises(UpstreamError) as e:
        _collect(egress._with_connect_retry(make, "deepseek"))
    assert e.value.status_code == 502 and "deepseek unreachable" in e.value.body
    assert calls["n"] == egress._CONNECT_RETRIES + 1   # bounded attempts


def test_connect_retry_does_not_retry_after_a_chunk(monkeypatch):
    monkeypatch.setattr(egress.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def make():
        calls["n"] += 1
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise httpx.ReadError("mid-stream drop")

    got = []
    with pytest.raises(UpstreamError):
        async def run():
            async for c in egress._with_connect_retry(make, "anthropic"):
                got.append(c)
        asyncio.run(run())
    assert calls["n"] == 1 and len(got) == 1     # streamed once, NOT restarted


# --- egress: Anthropic request translation ---------------------------------- #

def test_to_anthropic_request_shapes_messages_tools_system():
    payload = {
        "model": "claude-sonnet-4-6",
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "search", "arguments": '{"q":"x"}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "result text"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "search", "description": "find",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}],
    }
    req = egress._to_anthropic_request(payload, {"auth_mode": "apikey"})
    assert req["system"] == "be terse"
    assert req["temperature"] == 0.3
    assert req["stream"] is True and req["max_tokens"] > 0
    # assistant tool call -> tool_use block; tool role -> tool_result on a user msg
    assert req["messages"][1]["content"][1]["type"] == "tool_use"
    assert req["messages"][1]["content"][1]["input"] == {"q": "x"}
    assert req["messages"][2]["content"][0]["type"] == "tool_result"
    assert req["tools"][0]["name"] == "search"
    assert "input_schema" in req["tools"][0]


def test_anthropic_request_sends_only_one_of_temperature_top_p():
    # Anthropic rejects both together; temperature wins when both are present.
    req = egress._to_anthropic_request(
        {"model": "claude-sonnet-4-6", "messages": [], "temperature": 0.4, "top_p": 0.9}, {})
    assert req["temperature"] == 0.4 and "top_p" not in req
    # top_p is used only when temperature is absent.
    req2 = egress._to_anthropic_request({"model": "claude-sonnet-4-6", "messages": [], "top_p": 0.9}, {})
    assert req2["top_p"] == 0.9 and "temperature" not in req2


def test_anthropic_omits_sampling_for_models_that_reject_it():
    # Opus 4.7/4.8 and the Claude 5 family removed temperature/top_p — sending any 400s.
    for model in ("claude-opus-4-8", "claude-opus-4-7", "claude-fable-5", "claude-fable-5-1",
                  "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        req = egress._to_anthropic_request(
            {"model": model, "messages": [], "temperature": 0.7, "top_p": 0.95}, {})
        assert "temperature" not in req and "top_p" not in req, model
    # Older models still get one.
    assert egress._to_anthropic_request(
        {"model": "claude-opus-4-6", "messages": [], "temperature": 0.7}, {})["temperature"] == 0.7


def test_anthropic_headers_byok_vs_subscription():
    byok = egress._anthropic_headers({"auth_mode": "apikey", "api_key": "k"})
    assert byok["x-api-key"] == "k" and "Authorization" not in byok
    sub = egress._anthropic_headers({"auth_mode": "subscription", "api_key": "tok"})
    assert sub["Authorization"] == "Bearer tok" and "anthropic-beta" in sub


def test_subscription_system_is_array_with_exact_claude_code_first_block():
    # Anthropic's OAuth (subscription) path validates that the FIRST system block is
    # exactly the Claude Code preamble as its own array element — a concatenated
    # string fails and returns a misleading rate_limit_error. See egress._to_anthropic_request.
    req = egress._to_anthropic_request(
        {"model": "m", "messages": [{"role": "system", "content": "be terse"}]},
        {"auth_mode": "subscription"})
    assert isinstance(req["system"], list)
    assert req["system"][0] == {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
    assert req["system"][1] == {"type": "text", "text": "be terse"}


def test_subscription_system_is_lone_prefix_block_when_no_other_system():
    req = egress._to_anthropic_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        {"auth_mode": "subscription"})
    assert req["system"] == [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]


def test_byok_system_stays_a_plain_string():
    req = egress._to_anthropic_request(
        {"model": "m", "messages": [{"role": "system", "content": "be terse"}]},
        {"auth_mode": "apikey"})
    assert req["system"] == "be terse"


# --- egress: fake-httpx round trips ----------------------------------------- #

class _FakeResp:
    def __init__(self, lines, status=200):
        self.status_code = status
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b"upstream error body"


class _FakeClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    def stream(self, method, url, json=None, headers=None):
        _FakeClient.captured = {"method": method, "url": url, "json": json, "headers": headers}
        return _FakeClient.response

    async def aclose(self):
        pass


def _drain(agen):
    async def run():
        return [c async for c in agen]
    return asyncio.run(run())


def test_openai_egress_strips_top_k_and_sets_auth(monkeypatch):
    _FakeClient.response = _FakeResp([
        'data: {"choices":[{"delta":{"content":"hello"}}]}',
        "data: [DONE]",
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "deepseek", "kind": "openai",
              "base_url": "https://api.deepseek.com/v1", "api_key": "sk-ds",
              "auth_mode": "apikey"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}],
               "temperature": 0.7, "top_k": 64, "top_p": 0.95}
    chunks = _drain(egress.stream(payload, target))
    sent = _FakeClient.captured
    assert sent["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-ds"
    assert "top_k" not in sent["json"]       # not a valid OpenAI param
    assert sent["json"]["top_p"] == 0.95     # valid params preserved
    assert sent["json"]["stream"] is True
    assert chunks[0]["choices"][0]["delta"]["content"] == "hello"


def test_custom_chat_completions_omits_authorization_in_no_auth_mode(monkeypatch):
    _FakeClient.response = _FakeResp([
        'data: {"choices":[{"delta":{"content":"local"}}]}',
        "data: [DONE]",
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "openai-compatible", "kind": "openai",
              "base_url": "http://127.0.0.1:9000/v1", "api_key": None,
              "auth_mode": "none"}
    chunks = _drain(egress.stream({"model": "m", "messages": []}, target))
    assert _FakeClient.captured["url"] == "http://127.0.0.1:9000/v1/chat/completions"
    assert "Authorization" not in _FakeClient.captured["headers"]
    assert chunks[0]["choices"][0]["delta"]["content"] == "local"


def test_no_auth_remains_forbidden_for_builtin_providers():
    async def run():
        target = {"provider": "deepseek", "kind": "openai",
                  "base_url": "https://api.deepseek.com/v1", "api_key": None,
                  "auth_mode": "none"}
        async for _ in egress.stream({"model": "m", "messages": []}, target):
            pass
    with pytest.raises(UpstreamError) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 400 and "API key" in exc.value.body


def test_openai_egress_raises_on_http_error(monkeypatch):
    _FakeClient.response = _FakeResp([], status=401)
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "deepseek", "kind": "openai", "base_url": "https://x/v1",
              "api_key": "bad", "auth_mode": "apikey"}
    with pytest.raises(UpstreamError) as e:
        _drain(egress.stream({"model": "m", "messages": []}, target))
    assert e.value.status_code == 401


def test_anthropic_egress_translates_sse_to_openai_chunks(monkeypatch):
    _FakeClient.response = _FakeResp([
        'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu1","name":"search"}}',
        'data: {"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{\\"q\\":1}"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}',
        'data: {"type":"message_stop"}',
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "anthropic", "kind": "anthropic",
              "base_url": "https://api.anthropic.com", "api_key": "sk-ant",
              "auth_mode": "apikey"}
    chunks = _drain(egress.stream({"model": "claude-sonnet-4-6",
                                   "messages": [{"role": "user", "content": "hi"}]}, target))
    assert _FakeClient.captured["url"] == "https://api.anthropic.com/v1/messages"
    assert _FakeClient.captured["headers"]["x-api-key"] == "sk-ant"
    texts = [c["choices"][0]["delta"].get("content") for c in chunks
             if c["choices"][0]["delta"].get("content")]
    assert "Hi" in texts
    # tool_use surfaced as an OpenAI tool_calls delta with a name, then args fragment
    tool_starts = [c for c in chunks
                   if (c["choices"][0]["delta"].get("tool_calls") or [{}])[0].get("function", {}).get("name")]
    assert tool_starts and tool_starts[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "search"
    finals = [c for c in chunks if c["choices"][0].get("finish_reason")]
    assert finals[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert finals[-1]["usage"]["prompt_tokens"] == 11


def test_opencode_go_anthropic_model_uses_versioned_messages_endpoint(monkeypatch):
    _FakeClient.response = _FakeResp([
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}',
        'data: {"type":"message_stop"}',
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "opencode-go", "kind": "anthropic",
              "base_url": "https://opencode.ai/zen/go/v1", "api_key": "go-key",
              "auth_mode": "apikey"}
    _drain(egress.stream({"model": "minimax-m3", "messages": []}, target))
    assert _FakeClient.captured["url"] == "https://opencode.ai/zen/go/v1/messages"
    assert _FakeClient.captured["headers"]["x-api-key"] == "go-key"


# --- egress: Codex (ChatGPT subscription) request + SSE --------------------- #

def test_to_codex_request_shapes_input_and_tools():
    payload = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "search", "arguments": '{"q":"x"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "found"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "search", "description": "find", "parameters": {"type": "object"}}}],
    }
    req = egress._to_codex_request(payload)
    assert req["instructions"] == "be terse"
    assert req["stream"] is True and req["store"] is False
    # The Codex backend gates the model on a genuine-client shape: reasoning +
    # encrypted-reasoning include are required (else "model not supported").
    assert req["reasoning"]["summary"] == "auto" and "effort" in req["reasoning"]
    assert req["include"] == ["reasoning.encrypted_content"]
    assert req["input"][0] == {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": "hi"}]}
    # Native Responses tool items (NOT text-flattened) so multi-turn tool use works:
    # assistant tool call -> function_call; tool result -> function_call_output, paired by call_id.
    fc = req["input"][1]
    assert fc["type"] == "function_call" and fc["call_id"] == "c1" and fc["name"] == "search"
    assert fc["arguments"] == '{"q":"x"}'
    fco = req["input"][2]
    assert fco["type"] == "function_call_output" and fco["call_id"] == "c1" and fco["output"] == "found"
    # flat Responses tool shape (name at top level, not nested under "function")
    assert req["tools"][0]["name"] == "search" and req["tools"][0]["type"] == "function"
    assert req["tool_choice"] == "auto"


def test_to_generic_responses_request_omits_codex_only_fields():
    req = egress._to_responses_request({
        "model": "local-model", "temperature": 0.2, "top_p": 0.9,
        "max_tokens": 321,
        "messages": [{"role": "system", "content": "be terse"},
                     {"role": "user", "content": "hi"}],
    })
    assert req["instructions"] == "be terse" and req["stream"] is True
    assert req["max_output_tokens"] == 321 and req["temperature"] == 0.2
    assert "reasoning" not in req and "include" not in req and "store" not in req


def test_responses_stream_surfaces_reasoning_and_incomplete(monkeypatch):
    # A reasoning model can spend the whole output budget thinking: the gateway
    # streams response.reasoning_text.delta events and terminates with
    # response.incomplete (reason max_output_tokens). Both were dropped, making
    # the turn look like a dead stream (the chat channel "(no response)" incident).
    _FakeClient.response = _FakeResp([
        'data: {"type":"response.reasoning_text.delta","delta":"pondering"}',
        'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"},"usage":{"input_tokens":50,"output_tokens":40}}}',
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "openai-compatible", "kind": "responses",
              "base_url": "http://127.0.0.1:9000/v1", "api_key": None,
              "auth_mode": "none"}
    chunks = _drain(egress.stream(
        {"model": "m", "messages": [{"role": "user", "content": "hello"}]}, target))
    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == "pondering"
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"
    assert chunks[-1]["usage"]["completion_tokens"] == 40


def test_custom_responses_egress_omits_auth_and_translates_sse(monkeypatch):
    _FakeClient.response = _FakeResp([
        'data: {"type":"response.output_text.delta","delta":"Hi"}',
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":1}}}',
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "openai-compatible", "kind": "responses",
              "base_url": "http://127.0.0.1:9000/v1", "api_key": None,
              "auth_mode": "none"}
    chunks = _drain(egress.stream(
        {"model": "m", "messages": [{"role": "user", "content": "hello"}]}, target))
    assert _FakeClient.captured["url"] == "http://127.0.0.1:9000/v1/responses"
    assert "Authorization" not in _FakeClient.captured["headers"]
    assert "originator" not in _FakeClient.captured["headers"]
    assert "reasoning" not in _FakeClient.captured["json"]
    assert chunks[0]["choices"][0]["delta"]["content"] == "Hi"
    assert chunks[-1]["usage"]["total_tokens"] == 3


def test_custom_responses_bearer_header():
    headers = egress._responses_headers({"api_key": "sk-local"})
    assert headers["Authorization"] == "Bearer sk-local"


def test_codex_headers_carry_account_and_originator():
    h = egress._codex_headers({"api_key": "sk-live", "account_id": "acc_7"})
    assert h["Authorization"] == "Bearer sk-live"
    assert h["chatgpt-account-id"] == "acc_7"
    assert h["originator"] == "codex_cli_rs"
    assert h["accept"] == "text/event-stream"


def test_codex_egress_translates_responses_sse(monkeypatch):
    _FakeClient.response = _FakeResp([
        'data: {"type":"response.created"}',
        'data: {"type":"response.output_text.delta","delta":"Hel"}',
        'data: {"type":"response.output_text.delta","delta":"lo"}',
        'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"fc1","name":"search","arguments":"{\\"q\\":1}"}}',
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":4}}}',
    ])
    monkeypatch.setattr(egress.httpx, "AsyncClient", _FakeClient)
    target = {"provider": "openai", "kind": "codex",
              "base_url": "https://chatgpt.com/backend-api/codex/responses",
              "api_key": "sk-live", "account_id": "acc_7", "auth_mode": "subscription"}
    chunks = _drain(egress.stream({"model": "gpt-4o",
                                   "messages": [{"role": "user", "content": "hi"}]}, target))
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "Hello"
    names = [(c["choices"][0]["delta"].get("tool_calls") or [{}])[0].get("function", {}).get("name")
             for c in chunks]
    assert "search" in names
    finals = [c for c in chunks if c["choices"][0].get("finish_reason")]
    assert finals[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert finals[-1]["usage"]["prompt_tokens"] == 12


def test_codex_egress_without_login_errors():
    async def run():
        target = {"provider": "openai", "kind": "codex", "base_url": egress and "x",
                  "api_key": None, "account_id": None, "auth_mode": "subscription"}
        async for _ in egress.stream({"model": "gpt-4o", "messages": []}, target):
            pass
    with pytest.raises(UpstreamError) as e:
        asyncio.run(run())
    assert e.value.status_code == 400 and "ChatGPT" in e.value.body


# --- route surface ---------------------------------------------------------- #

@pytest.fixture()
def client(home):
    import app
    return TestClient(app.app)


def test_providers_route_get_and_put(client):
    listed = client.get("/api/providers").json()["providers"]
    ids = {p["id"] for p in listed}
    assert {"deepseek", "openai", "anthropic", "opencode-go", "openai-compatible"} <= ids

    r = client.put("/api/providers/deepseek", json={"apiKey": "sk-route"})
    assert r.status_code == 200 and r.json()["hasKey"] is True
    # The key itself is never returned by the API.
    assert "sk-route" not in r.text
    assert providers.get_record("deepseek")["apiKey"] == "sk-route"

    custom = client.put("/api/providers/openai-compatible", json={
        "baseUrl": "http://127.0.0.1:9000/v1",
        "apiType": "responses", "authMode": "none", "apiKey": "",
    })
    assert custom.status_code == 200
    assert custom.json()["apiType"] == "responses" and custom.json()["authMode"] == "none"
    assert custom.json()["hasKey"] is False


def test_custom_provider_model_discovery(client, monkeypatch):
    providers.set_record("openai-compatible", baseUrl="http://127.0.0.1:9000/v1",
                         authMode="none")

    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gateway-a"}, {"id": "gateway-b"}, {"id": "gateway-a"}]}

    class HttpClient:
        seen: dict = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            self.seen = {"url": url, "headers": headers}
            return Response()

    monkeypatch.setattr("routes.providers.httpx.AsyncClient", lambda **_kwargs: HttpClient())
    response = client.get("/api/providers/openai-compatible/models")
    assert response.status_code == 200
    assert response.json()["models"] == ["gateway-a", "gateway-b"]


def test_custom_provider_model_discovery_rejects_builtin_provider(client):
    assert client.get("/api/providers/deepseek/models").status_code == 404


def test_openai_model_discovery_uses_saved_api_key(client, monkeypatch):
    providers.set_record("openai", apiKey="sk-openai")

    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-5.4"}, {"id": "gpt-5.4-mini"}]}

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            assert url == "https://api.openai.com/v1/models"
            assert headers == {"Authorization": "Bearer sk-openai"}
            return Response()

    monkeypatch.setattr("routes.providers.httpx.AsyncClient", lambda **_kwargs: HttpClient())
    response = client.get("/api/providers/openai/models")
    assert response.status_code == 200
    assert response.json()["models"] == ["gpt-5.4", "gpt-5.4-mini"]


def test_opencode_go_model_discovery_uses_saved_key(client, monkeypatch):
    providers.set_record("opencode-go", apiKey="go-key")

    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"id": "deepseek-v4-flash"}, {"id": "gpt-5.6-luna"}]}

    class HttpClient:
        seen: dict = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            self.seen = {"url": url, "headers": headers}
            return Response()

    http = HttpClient()
    monkeypatch.setattr("routes.providers.httpx.AsyncClient", lambda **_kwargs: http)
    response = client.get("/api/providers/opencode-go/models")
    assert response.status_code == 200
    assert response.json()["models"] == ["deepseek-v4-flash", "gpt-5.6-luna"]
    assert http.seen == {
        "url": "https://opencode.ai/zen/go/v1/models",
        "headers": {"Authorization": "Bearer go-key"},
    }


def test_providers_route_rejects_unknown(client):
    assert client.put("/api/providers/bogus", json={"apiKey": "x"}).status_code == 404
