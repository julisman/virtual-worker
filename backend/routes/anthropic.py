# Anthropic Messages API surface — lets Claude Code use the local model.

"""POST /v1/messages and /v1/messages/count_tokens.

Translates the Anthropic wire format to the OpenAI Chat Completions shape the
upstream speaks, then translates the (streamed) reply back. Point Claude Code
here with ANTHROPIC_BASE_URL=http://localhost:8888.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway import translate_anthropic as tr
from gateway.auth import check_key
from gateway.models import resolve_model
from gateway.sse import sse_event
from gateway.upstream import UpstreamError, chat_completion, chat_completion_stream

logger = logging.getLogger(__name__)

router = APIRouter()


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"type": "error", "error": {"type": kind, "message": message}})


@router.post("/messages")
async def messages(request: Request):
    if not check_key(request):
        return _error(401, "authentication_error", "invalid or missing API key")

    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid_request_error", "request body is not valid JSON")

    requested_model = body.get("model")
    model = await resolve_model(requested_model)
    payload = tr.anthropic_to_openai(body, model)

    if body.get("stream"):
        input_tokens = tr.estimate_tokens(tr.count_tokens_source(body))

        async def gen():
            try:
                upstream = chat_completion_stream(payload)
                async for event_bytes in tr.stream_anthropic(upstream, requested_model or model, input_tokens):
                    yield event_bytes
            except UpstreamError as exc:
                logger.warning("anthropic stream upstream error: %s", exc)
                yield sse_event(
                    {"type": "error", "error": {"type": "api_error", "message": exc.body[:500]}},
                    event="error",
                )

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        resp = await chat_completion(payload)
    except UpstreamError as exc:
        return _error(exc.status_code if exc.status_code != 502 else 502, "api_error", exc.body[:500])
    return JSONResponse(tr.openai_to_anthropic(resp, requested_model or model))


@router.post("/messages/count_tokens")
async def count_tokens(request: Request):
    if not check_key(request):
        return _error(401, "authentication_error", "invalid or missing API key")
    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid_request_error", "request body is not valid JSON")
    n = tr.estimate_tokens(tr.count_tokens_source(body))
    return JSONResponse({"input_tokens": n})
