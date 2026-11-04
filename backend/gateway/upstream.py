"""The one place that talks to the OpenAI-compatible upstream.

Extracted from ``routes/inference.py`` so the raw `/v1/*` byte-passthrough proxy
AND the protocol adapters share a single upstream client, header policy, and
error envelope. Three entry points:

- ``raw_proxy``            — byte-for-byte proxy (what inference.py uses).
- ``chat_completion``      — non-streaming call, returns parsed JSON.
- ``chat_completion_stream`` — streaming call, yields parsed delta dicts.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config import UPSTREAM_URL

logger = logging.getLogger(__name__)

# Headers that must not be forwarded verbatim to the upstream.
_HOP_BY_HOP = {"host", "content-length", "connection", "accept-encoding"}

# Generous read timeout (resets per streamed chunk) so a wedged upstream surfaces
# an error instead of hanging the UI forever; still allows slow first tokens +
# model load. Connect is short.
_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=180.0, pool=15.0)


def _upstream_url(path: str) -> str:
    return f"{UPSTREAM_URL.rstrip('/')}{path}"


class UpstreamError(Exception):
    """Upstream returned a non-2xx (or was unreachable). Carries body for the
    adapter to wrap in its native error envelope."""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"upstream {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


# --------------------------------------------------------------------------- #
# Raw proxy (byte-for-byte) — used by routes/inference.py
# --------------------------------------------------------------------------- #

async def raw_proxy(request: Request, path: str) -> Response:
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}

    stream = False
    try:
        stream = bool(json.loads(body or b"{}").get("stream"))
    except Exception:
        pass

    url = _upstream_url(path)
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        req = client.build_request("POST", url, content=body, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("upstream proxy error: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Upstream {UPSTREAM_URL} unreachable: {exc}",
                               "type": "upstream_unavailable"}},
        )

    media_type = resp.headers.get("content-type", "application/json")
    if stream:
        async def gen():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            except httpx.HTTPError as exc:
                # Upstream dropped mid-stream (e.g. the model server crashed during
                # generation). End cleanly + emit an OpenAI-style error chunk
                # instead of letting the exception spew a traceback.
                logger.warning("upstream stream interrupted: %s", exc)
                err = {"error": {"message": f"model server interrupted the response: {exc}",
                                 "type": "upstream_interrupted"}}
                yield f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n".encode()
            finally:
                await resp.aclose()
                await client.aclose()
        return StreamingResponse(gen(), status_code=resp.status_code, media_type=media_type)

    content = await resp.aread()
    await resp.aclose()
    await client.aclose()
    return Response(content=content, status_code=resp.status_code, media_type=media_type)


# --------------------------------------------------------------------------- #
# Structured calls — used by the protocol adapters
# --------------------------------------------------------------------------- #

async def chat_completion(payload: dict) -> dict:
    """Non-streaming chat completion. Returns the parsed JSON dict."""
    body = {**payload, "stream": False}
    url = _upstream_url("/v1/chat/completions")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise UpstreamError(502, f"Upstream {UPSTREAM_URL} unreachable: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise UpstreamError(resp.status_code, resp.text)
    return resp.json()


async def chat_completion_stream(payload: dict) -> AsyncIterator[dict]:
    """Streaming chat completion. Yields each parsed SSE ``data:`` JSON dict
    (skips keep-alives, stops on ``[DONE]``). Requests usage in the final chunk."""
    body = {
        **payload,
        "stream": True,
        "stream_options": {"include_usage": True, **(payload.get("stream_options") or {})},
    }
    url = _upstream_url("/v1/chat/completions")
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        async with client.stream("POST", url, json=body) as resp:
            if resp.status_code // 100 != 2:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise UpstreamError(resp.status_code, text)
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload_str = line[len("data:"):].strip()
                if not payload_str or payload_str == "[DONE]":
                    if payload_str == "[DONE]":
                        break
                    continue
                try:
                    yield json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
    except httpx.HTTPError as exc:
        raise UpstreamError(502, f"Upstream {UPSTREAM_URL} unreachable: {exc}") from exc
    finally:
        await client.aclose()
