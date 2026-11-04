# OpenAI Responses API surface — lets Codex CLI use the local model.

"""POST /v1/responses.

Codex only speaks the Responses API. Configure ~/.codex/config.toml with
base_url="http://localhost:8888/v1" and wire_api="responses".
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway import translate_responses as tr
from gateway.auth import check_key
from gateway.models import resolve_model
from gateway.sse import sse_event
from gateway.upstream import UpstreamError, chat_completion, chat_completion_stream

logger = logging.getLogger(__name__)

router = APIRouter()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": "invalid_request_error"}})


@router.post("/responses")
async def responses(request: Request):
    if not check_key(request):
        return _error(401, "invalid or missing API key")
    try:
        body = await request.json()
    except Exception:
        return _error(400, "request body is not valid JSON")

    requested_model = body.get("model")
    model = await resolve_model(requested_model)
    payload = tr.responses_to_openai(body, model)

    if body.get("stream"):
        async def gen():
            try:
                upstream = chat_completion_stream(payload)
                async for event_bytes in tr.stream_responses(upstream, requested_model or model):
                    yield event_bytes
            except UpstreamError as exc:
                logger.warning("responses stream upstream error: %s", exc)
                yield sse_event({"type": "error", "message": exc.body[:500]}, event="error")

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        resp = await chat_completion(payload)
    except UpstreamError as exc:
        return _error(exc.status_code if exc.status_code != 502 else 502, exc.body[:500])
    return JSONResponse(tr.openai_to_responses(resp, requested_model or model))
