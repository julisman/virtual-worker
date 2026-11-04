"""Gemini <-> OpenAI translation (pure)."""

import asyncio
import json

import gateway.translate_gemini as tr


async def _drain(agen):
    return [c async for c in agen]


async def _agen(items):
    for it in items:
        yield it


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #

def test_request_system_and_contents():
    body = {
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 100, "topP": 0.9},
    }
    out = tr.gemini_to_openai(body, "local")
    assert out["messages"][0] == {"role": "system", "content": "be brief"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["temperature"] == 0.5 and out["max_tokens"] == 100 and out["top_p"] == 0.9


def test_request_inline_image():
    body = {"contents": [{"role": "user", "parts": [
        {"text": "what"}, {"inlineData": {"mimeType": "image/jpeg", "data": "QQ=="}}]}]}
    out = tr.gemini_to_openai(body, "local")
    parts = out["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,QQ==")


def test_request_function_call_and_response_correlation():
    body = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "JKT"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "get_weather", "response": {"t": 32}}}]},
    ]}
    out = tr.gemini_to_openai(body, "local")
    asst, tool = out["messages"][0], out["messages"][1]
    assert asst["tool_calls"][0]["function"]["name"] == "get_weather"
    synth_id = asst["tool_calls"][0]["id"]
    # the tool response must carry the SAME synthesized id (correlation by name)
    assert tool == {"role": "tool", "tool_call_id": synth_id, "content": json.dumps({"t": 32})}


def test_request_tools_and_mode():
    body = {"contents": [], "tools": [{"functionDeclarations": [
        {"name": "f", "description": "d", "parameters": {"type": "object"}}]}],
        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}}
    out = tr.gemini_to_openai(body, "local")
    assert out["tools"][0]["function"]["name"] == "f"
    assert out["tool_choice"] == "required"


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #

def test_response_text_and_finish_map():
    resp = {"choices": [{"message": {"content": "hello"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    out = tr.openai_to_gemini(resp, "gemini-x")
    cand = out["candidates"][0]
    assert cand["content"]["parts"] == [{"text": "hello"}]
    assert cand["finishReason"] == "MAX_TOKENS"
    assert out["usageMetadata"] == {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}


def test_response_function_call():
    resp = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "f", "arguments": '{"x":1}'}}]}, "finish_reason": "tool_calls"}]}
    out = tr.openai_to_gemini(resp, "gemini-x")
    part = out["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"] == {"name": "f", "args": {"x": 1}}


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

def test_stream_text_live_then_final():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
    ]
    out = asyncio.run(_drain(tr.gemini_stream_chunks(_agen(chunks), "gemini-x")))
    # two live text chunks + one final chunk
    assert out[0]["candidates"][0]["content"]["parts"] == [{"text": "Hel"}]
    assert out[1]["candidates"][0]["content"]["parts"] == [{"text": "lo"}]
    final = out[-1]
    assert final["candidates"][0]["finishReason"] == "STOP"
    assert final["usageMetadata"]["candidatesTokenCount"] == 2


def test_stream_function_call_flushed_last():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "f", "arguments": '{"a":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    out = asyncio.run(_drain(tr.gemini_stream_chunks(_agen(chunks), "gemini-x")))
    final = out[-1]
    part = final["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"] == {"name": "f", "args": {"a": 1}}
