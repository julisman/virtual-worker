# Gemini API surface — lets Gemini SDK / Antigravity clients use the local model.

"""POST /v1beta/models/{model}:generateContent and :streamGenerateContent.

The path segment carries both model and action, e.g.
``/v1beta/models/gemini-2.0-flash:generateContent``. Streaming uses SSE when
``?alt=sse`` (what the google-genai SDK sends); otherwise a JSON array is
returned. Point a client here with base_url=http://localhost:8888.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway import translate_gemini as tr
from gateway.auth import check_key
from gateway.models import resolve_model
from gateway.sse import sse_event
from gateway.upstream import UpstreamError, chat_completion, chat_completion_stream

logger = logging.getLogger(__name__)

router = APIRouter()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": status, "message": message, "status": "ERROR"}})


@router.post("/models/{name}")
async def generate(name: str, request: Request):
    if not check_key(request):
        return _error(401, "invalid or missing API key")

    # name = "gemini-2.0-flash:generateContent" | "...:streamGenerateContent"
    model_name, _, action = name.rpartition(":")
    if not action:  # no colon -> treat whole thing as model, default action
        model_name, action = name, "generateContent"

    try:
        body = await request.json()
    except Exception:
        return _error(400, "request body is not valid JSON")

    model = await resolve_model(model_name)
    payload = tr.gemini_to_openai(body, model)
    streaming = action == "streamGenerateContent"
    sse = request.query_params.get("alt") == "sse"

    if streaming:
        async def chunk_dicts():
            upstream = chat_completion_stream(payload)
            async for c in tr.gemini_stream_chunks(upstream, model_name):
                yield c

        if sse:
            async def gen():
                try:
                    async for c in chunk_dicts():
                        yield sse_event(c)
                except UpstreamError as exc:
                    logger.warning("gemini stream upstream error: %s", exc)
                    yield sse_event({"error": {"code": 502, "message": exc.body[:500]}})
            return StreamingResponse(gen(), media_type="text/event-stream")

        # Non-SSE streaming: collect into a JSON array (best-effort).
        try:
            collected = [c async for c in chunk_dicts()]
        except UpstreamError as exc:
            return _error(exc.status_code if exc.status_code != 502 else 502, exc.body[:500])
        return JSONResponse(collected)

    try:
        resp = await chat_completion(payload)
    except UpstreamError as exc:
        return _error(exc.status_code if exc.status_code != 502 else 502, exc.body[:500])
    return JSONResponse(tr.openai_to_gemini(resp, model_name))
