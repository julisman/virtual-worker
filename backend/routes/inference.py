# OpenAI-compatible inference gateway — proxies to an upstream model server.

"""A thin OpenAI-compatible proxy.

Forwards /v1/* to ``VW_UPSTREAM_URL`` (a local llama.cpp / vLLM server, or
any provider base URL). Streaming is passed through byte-for-byte. The
ApiDebugMiddleware tees these calls into the trace viewer, so this gives us one
gateway in front of local + remote models *and* full observability.
"""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import UPSTREAM_URL
from gateway.upstream import raw_proxy as _proxy

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await _proxy(request, "/v1/chat/completions")


@router.post("/completions")
async def completions(request: Request):
    return await _proxy(request, "/v1/completions")


@router.post("/embeddings")
async def embeddings(request: Request):
    return await _proxy(request, "/v1/embeddings")


@router.get("/models")
async def models():
    url = f"{UPSTREAM_URL.rstrip('/')}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"error": {"message": str(exc)}})
