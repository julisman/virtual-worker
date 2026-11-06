"""Vision helper — hermetic tests (no real model / network)."""

import asyncio
import base64

import pytest

import config
from agent import vision_helper
from gateway import egress, providers
from storage import agent_store, settings


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Isolate settings.json + stub resolve_target so no real model is touched."""
    monkeypatch.setattr(config, "APP_HOME", tmp_path)

    async def _resolve(agent, requested_model=None):
        return ("vision-model", None)

    monkeypatch.setattr(providers, "resolve_target", _resolve)


def _stream_yielding(*chunks):
    async def _stream(payload, target):
        _stream.last_payload = payload
        for c in chunks:
            yield c
    _stream.last_payload = None
    return _stream


def _content_chunk(text):
    return {"choices": [{"delta": {"content": text}}]}


# ─── resolve_helper_agent_id ─────────────────────────────────────────────────

def test_resolve_override_wins(monkeypatch):
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: {"id": aid})
    assert vision_helper.resolve_helper_agent_id("caller", "helper-x") == "helper-x"


def test_resolve_global_when_enabled(monkeypatch):
    settings.set("visionHelper", {"enabled": True, "agentId": "global-helper"})
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: {"id": aid})
    assert vision_helper.resolve_helper_agent_id("caller", None) == "global-helper"


def test_resolve_none_when_disabled(monkeypatch):
    settings.set("visionHelper", {"enabled": False, "agentId": "global-helper"})
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: {"id": aid})
    assert vision_helper.resolve_helper_agent_id("caller", None) is None


def test_resolve_none_when_blank(monkeypatch):
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: {"id": aid})
    assert vision_helper.resolve_helper_agent_id("caller", "   ") is None
    assert vision_helper.resolve_helper_agent_id("caller", None) is None


def test_resolve_none_on_self_loop(monkeypatch):
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: {"id": aid})
    assert vision_helper.resolve_helper_agent_id("me", "me") is None


def test_resolve_none_when_agent_missing(monkeypatch):
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: None)
    assert vision_helper.resolve_helper_agent_id("caller", "ghost") is None


# ─── describe_images ─────────────────────────────────────────────────────────

def test_describe_returns_none_when_no_helper(monkeypatch):
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: None)
    out = asyncio.run(vision_helper.describe_images(
        "caller", None, [{"data_url": "d", "name": "a.png"}]))
    assert out is None


def test_describe_success_uses_persona_as_system(monkeypatch):
    agent = {"id": "helper", "persona": "You are an eagle-eyed reader."}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)
    stream = _stream_yielding(_content_chunk("Hello "), _content_chunk("world"))
    monkeypatch.setattr(egress, "stream", stream)

    out = asyncio.run(vision_helper.describe_images(
        "caller", "helper", [{"data_url": "d", "name": "a.png"}]))

    assert out == [{"name": "a.png", "text": "Hello world",
                    "error": None, "model": "vision-model"}]
    sys_msg = stream.last_payload["messages"][0]
    assert sys_msg["content"] == "You are an eagle-eyed reader."


def test_describe_uses_default_system_when_persona_blank(monkeypatch):
    agent = {"id": "helper", "persona": "   "}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)
    stream = _stream_yielding(_content_chunk("text"))
    monkeypatch.setattr(egress, "stream", stream)

    out = asyncio.run(vision_helper.describe_images(
        "caller", "helper", [{"data_url": "d", "name": "a.png"}]))

    assert out[0]["text"] == "text"
    assert stream.last_payload["messages"][0]["content"] == \
        vision_helper.DEFAULT_VISION_SYSTEM


def test_describe_empty_output_is_error(monkeypatch):
    agent = {"id": "helper", "persona": ""}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)
    monkeypatch.setattr(egress, "stream", _stream_yielding(_content_chunk("   ")))

    out = asyncio.run(vision_helper.describe_images(
        "caller", "helper", [{"data_url": "d", "name": "a.png"}]))

    assert out[0]["text"] is None
    assert out[0]["error"] == "vision model returned no text"


def test_describe_truncates_long_output(monkeypatch):
    agent = {"id": "helper", "persona": ""}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)
    big = "x" * (vision_helper.VISION_MAX_OUTPUT_CHARS + 500)
    monkeypatch.setattr(egress, "stream", _stream_yielding(_content_chunk(big)))

    out = asyncio.run(vision_helper.describe_images(
        "caller", "helper", [{"data_url": "d", "name": "a.png"}]))

    text = out[0]["text"]
    assert text.endswith("…(truncated)")
    assert len(text) == vision_helper.VISION_MAX_OUTPUT_CHARS + len("…(truncated)")


def test_describe_timeout(monkeypatch):
    agent = {"id": "helper", "persona": ""}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)
    monkeypatch.setattr(vision_helper, "VISION_TIMEOUT_SECS", 0.05)

    async def _slow(payload, target):
        await asyncio.sleep(5)
        yield _content_chunk("never")

    monkeypatch.setattr(egress, "stream", _slow)

    out = asyncio.run(vision_helper.describe_images(
        "caller", "helper", [{"data_url": "d", "name": "a.png"}]))

    assert out[0]["text"] is None
    assert out[0]["error"] == "vision model timed out"


def test_describe_caps_to_max_images(monkeypatch):
    agent = {"id": "helper", "persona": ""}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)
    monkeypatch.setattr(egress, "stream", _stream_yielding(_content_chunk("ok")))

    images = [{"data_url": "d", "name": f"{i}.png"} for i in range(6)]
    out = asyncio.run(vision_helper.describe_images("caller", "helper", images))

    assert len(out) == vision_helper.VISION_MAX_IMAGES


def test_describe_threads_reasoning_effort_for_remote_reasoning_target(monkeypatch):
    # Regression: a remote reasoning model (e.g. gpt-5.x on the Responses endpoint)
    # rejects `temperature`; the agent loop steers it via reasoning_effort instead.
    # The helper must thread its configured effort through so egress drops temperature.
    agent = {"id": "helper", "persona": "", "reasoningEffort": "medium"}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)

    async def _resolve(a, requested_model=None):
        return ("vision-model", {"provider": "openai-compatible", "reasoning_effort": True})
    monkeypatch.setattr(providers, "resolve_target", _resolve)
    stream = _stream_yielding(_content_chunk("ok"))
    monkeypatch.setattr(egress, "stream", stream)

    asyncio.run(vision_helper.describe_images("caller", "helper",
                                              [{"data_url": "d", "name": "a.png"}]))
    assert stream.last_payload["reasoning_effort"] == "medium"


def test_describe_omits_reasoning_effort_for_local_target(monkeypatch):
    # Local upstream (target is None): never send reasoning_effort even if configured.
    agent = {"id": "helper", "persona": "", "reasoningEffort": "medium"}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)
    stream = _stream_yielding(_content_chunk("ok"))
    monkeypatch.setattr(egress, "stream", stream)

    asyncio.run(vision_helper.describe_images("caller", "helper",
                                              [{"data_url": "d", "name": "a.png"}]))
    assert "reasoning_effort" not in stream.last_payload


def test_describe_per_image_failure_isolation(monkeypatch):
    agent = {"id": "helper", "persona": ""}
    monkeypatch.setattr(agent_store, "get_agent", lambda aid: agent)

    async def _stream(payload, target):
        # The failing image carries name "boom.png".
        parts = payload["messages"][1]["content"]
        url = parts[1]["image_url"]["url"]
        if url == "bad":
            raise RuntimeError("upstream exploded")
            yield  # pragma: no cover
        yield _content_chunk("fine")

    monkeypatch.setattr(egress, "stream", _stream)

    images = [
        {"data_url": "good", "name": "ok.png"},
        {"data_url": "bad", "name": "boom.png"},
        {"data_url": "good", "name": "ok2.png"},
    ]
    out = asyncio.run(vision_helper.describe_images("caller", "helper", images))

    assert out[0]["text"] == "fine" and out[0]["error"] is None
    assert out[1]["text"] is None
    assert "upstream exploded" in out[1]["error"]
    assert out[2]["text"] == "fine" and out[2]["error"] is None


# ─── describe_message_images / will_describe ─────────────────────────────────

def _data_url(payload=b"\x89PNG\r\n\x1a\n fake", mime="image/png"):
    b64 = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _img_block(url):
    return {"type": "image_url", "image_url": {"url": url}}


def _user_msg(*blocks):
    return {"role": "user", "content": list(blocks)}


@pytest.fixture(autouse=True)
def _clear_cache():
    vision_helper._DESCRIBE_CACHE.clear()
    yield
    vision_helper._DESCRIBE_CACHE.clear()


def _blind_caller(monkeypatch):
    """Caller model is blind; helper 'helper' is vision-capable by default."""
    monkeypatch.setattr(providers, "supports_vision",
                        lambda provider, model: model == "vision-model")


def test_message_caller_vision_capable_keeps_valid_replaces_invalid(monkeypatch):
    # Caller can see everything.
    monkeypatch.setattr(providers, "supports_vision", lambda p, m: True)

    async def _boom(*a, **k):
        raise AssertionError("describe_images must not be called for a seeing caller")
    monkeypatch.setattr(vision_helper, "describe_images", _boom)

    good = _img_block(_data_url())
    bad = _img_block("data:image/png;base64,not-base64!!")
    messages = [_user_msg(good, bad)]

    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller"}, "gpt-4o", {"provider": "openai"}, messages))

    blocks = out[0]["content"]
    assert blocks[0]["type"] == "image_url"          # valid kept as-is
    assert blocks[1]["type"] == "text"               # invalid replaced
    assert "could not be read" in blocks[1]["text"]


def test_message_blind_caller_helper_describes(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})
    stream = _stream_yielding(_content_chunk("a red chart"))
    monkeypatch.setattr(egress, "stream", stream)

    messages = [_user_msg(_img_block(_data_url()))]
    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller", "visionHelperAgentId": "helper"},
        "blind-model", None, messages))

    note = out[0]["content"][0]
    assert note["type"] == "text"
    assert "<<<vision" in note["text"] and ">>>" in note["text"]
    assert "a red chart" in note["text"]
    assert stream.last_payload is not None            # egress called once


def test_message_blind_caller_no_helper_gets_no_vision_note(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(vision_helper, "resolve_helper_agent_id",
                        lambda cid, override: None)

    async def _boom(*a, **k):
        raise AssertionError("no describe without a helper")
    monkeypatch.setattr(vision_helper, "describe_images", _boom)

    messages = [_user_msg(_img_block(_data_url()))]
    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller"}, "blind-model", None, messages))

    assert out[0]["content"][0]["text"] == vision_helper.NO_VISION_NOTE


def test_message_blind_caller_helper_not_vision_capable_hard_skips(monkeypatch):
    # Caller blind AND helper's own model is blind → NO_VISION_NOTE, no egress.
    monkeypatch.setattr(providers, "supports_vision", lambda p, m: False)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})

    async def _boom(payload, target):
        raise AssertionError("egress must not be called for a blind helper")
        yield  # pragma: no cover
    monkeypatch.setattr(egress, "stream", _boom)

    messages = [_user_msg(_img_block(_data_url()))]
    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller", "visionHelperAgentId": "helper"},
        "blind-model", None, messages))

    assert out[0]["content"][0]["text"] == vision_helper.NO_VISION_NOTE


def test_message_dedup_same_image_one_describe_call(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})

    calls = {"n": 0}

    async def _describe(caller, override, images):
        calls["n"] += 1
        return [{"name": "", "text": "same-desc", "error": None,
                 "model": "vision-model"} for _ in images]
    monkeypatch.setattr(vision_helper, "describe_images", _describe)

    url = _data_url()
    messages = [_user_msg(_img_block(url), _img_block(url))]
    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller", "visionHelperAgentId": "helper"},
        "blind-model", None, messages))

    assert calls["n"] == 1
    blocks = out[0]["content"]
    assert "same-desc" in blocks[0]["text"] and "same-desc" in blocks[1]["text"]


def test_message_cache_hit_across_calls(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})

    calls = {"n": 0}

    async def _describe(caller, override, images):
        calls["n"] += 1
        return [{"name": "", "text": "cached-desc", "error": None,
                 "model": "vision-model"} for _ in images]
    monkeypatch.setattr(vision_helper, "describe_images", _describe)

    url = _data_url()
    agent = {"id": "caller", "visionHelperAgentId": "helper"}

    asyncio.run(vision_helper.describe_message_images(
        agent, "blind-model", None, [_user_msg(_img_block(url))]))
    asyncio.run(vision_helper.describe_message_images(
        agent, "blind-model", None, [_user_msg(_img_block(url))]))

    assert calls["n"] == 1  # second call served from cache


def test_message_overflow_extra_uniques_get_overflow_note(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})

    async def _describe(caller, override, images):
        return [{"name": "", "text": f"desc{i}", "error": None,
                 "model": "vision-model"} for i, _ in enumerate(images)]
    monkeypatch.setattr(vision_helper, "describe_images", _describe)

    n = vision_helper.VISION_MAX_IMAGES + 2
    blocks = [_img_block(_data_url(payload=f"unique-{i}".encode())) for i in range(n)]
    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller", "visionHelperAgentId": "helper"},
        "blind-model", None, [_user_msg(*blocks)]))

    texts = [b["text"] for b in out[0]["content"]]
    overflow = [t for t in texts if t == vision_helper.OVERFLOW_NOTE]
    assert len(overflow) == 2


def test_message_oversized_dataurl_is_invalid_not_sent(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})

    async def _boom(*a, **k):
        raise AssertionError("oversized image must not reach the model")
    monkeypatch.setattr(vision_helper, "describe_images", _boom)

    huge = "A" * (vision_helper.VISION_MAX_DATAURL_CHARS + 10)
    url = f"data:image/png;base64,{huge}"
    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller", "visionHelperAgentId": "helper"},
        "blind-model", None, [_user_msg(_img_block(url))]))

    note = out[0]["content"][0]["text"]
    assert "could not be read" in note and "too-large" in note


def test_message_copy_on_write_does_not_mutate_input(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})
    monkeypatch.setattr(egress, "stream",
                        _stream_yielding(_content_chunk("desc")))

    block = _img_block(_data_url())
    content = [block]
    msg = {"role": "user", "content": content}
    messages = [msg]

    out = asyncio.run(vision_helper.describe_message_images(
        {"id": "caller", "visionHelperAgentId": "helper"},
        "blind-model", None, messages))

    # Input untouched.
    assert messages[0] is msg
    assert msg["content"] is content
    assert content[0] is block
    assert block["type"] == "image_url"
    # Output is a fresh structure with the note.
    assert out is not messages
    assert out[0]["content"][0]["type"] == "text"


def test_will_describe_true_only_for_blind_with_capable_helper(monkeypatch):
    _blind_caller(monkeypatch)
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda aid: {"id": aid, "persona": ""})

    agent = {"id": "caller", "visionHelperAgentId": "helper"}
    messages = [_user_msg(_img_block(_data_url()))]

    # Blind caller + resolvable vision-capable helper → True.
    assert asyncio.run(vision_helper.will_describe(
        agent, "blind-model", None, messages)) is True

    # No image → False.
    assert asyncio.run(vision_helper.will_describe(
        agent, "blind-model", None, [_user_msg({"type": "text", "text": "hi"})])) is False

    # Caller can see → False.
    monkeypatch.setattr(providers, "supports_vision", lambda p, m: True)
    assert asyncio.run(vision_helper.will_describe(
        agent, "vision-model", {"provider": "openai"}, messages)) is False

    # Blind caller but helper not vision-capable → False.
    monkeypatch.setattr(providers, "supports_vision", lambda p, m: False)
    assert asyncio.run(vision_helper.will_describe(
        agent, "blind-model", None, messages)) is False
