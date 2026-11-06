# MCP servers — connect agents to external tool providers (local-first).

"""GET/POST /api/mcp, GET/PUT/DELETE /api/mcp/{id}, POST /api/mcp/{id}/tools.

CRUD for MCP server configs + a 'test' endpoint that connects and lists the
server's tools. The ``mcp`` SDK is optional; ``GET /api/mcp`` reports whether
it's available so the UI can guide the user.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent import mcp_client
from storage import mcp_store

logger = logging.getLogger(__name__)

router = APIRouter()


class ServerIn(BaseModel):
    name: str | None = None
    transport: str | None = None  # 'stdio' | 'http'
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    allowedTools: list[str] | None = None  # empty/absent = expose every tool
    enabled: bool | None = None


@router.get("")
async def list_servers():
    return {"servers": mcp_store.list_servers(), "available": mcp_client.mcp_available()}


@router.post("")
async def create_server(body: ServerIn):
    return mcp_store.create_server(body.model_dump(exclude_none=True))


@router.get("/{server_id}")
async def get_server(server_id: str):
    s = mcp_store.get_server(server_id)
    if not s:
        raise HTTPException(404, "no such server")
    return s


@router.put("/{server_id}")
async def update_server(server_id: str, body: ServerIn):
    s = mcp_store.update_server(server_id, body.model_dump(exclude_unset=True))
    if not s:
        raise HTTPException(404, "no such server")
    return s


@router.delete("/{server_id}")
async def delete_server(server_id: str):
    if not mcp_store.delete_server(server_id):
        raise HTTPException(404, "no such server")
    return {"deleted": server_id}


@router.post("/{server_id}/tools")
async def test_server(server_id: str):
    """Connect to the server and list its tools (verifies the config works)."""
    cfg = mcp_store.get_server(server_id)
    if not cfg:
        raise HTTPException(404, "no such server")
    if not mcp_client.mcp_available():
        raise HTTPException(400, "the 'mcp' Python SDK is not installed")
    try:
        tools = await mcp_client.list_server_tools(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP test connection failed for server %s", server_id)
        raise HTTPException(400, f"connection failed: {exc}") from exc
    return {"tools": tools}
