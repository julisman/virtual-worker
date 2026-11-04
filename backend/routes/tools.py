# Custom tools — give agents new capabilities by declaration (local-first).

"""GET/POST /api/tools, GET/PUT/DELETE /api/tools/{id}, POST /api/tools/{id}/test.

CRUD for custom HTTP tool definitions + a 'test' endpoint that invokes the tool
with sample args (honoring the SSRF guard + timeout). The lightweight, first-class
alternative to standing up an MCP server (see routes/mcp.py).
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent import custom_tools
from storage import tool_store

logger = logging.getLogger(__name__)

router = APIRouter()


class ToolIn(BaseModel):
    name: str | None = None
    description: str | None = None
    transport: str | None = None          # 'http' (script: deferred)
    parameters: dict | None = None        # JSON Schema for the args
    method: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    allowPrivate: bool | None = None
    timeout: int | None = None
    enabled: bool | None = None


class TestIn(BaseModel):
    args: dict | None = None


@router.get("")
async def list_tools():
    return {"tools": tool_store.list_tools()}


@router.post("")
async def create_tool(body: ToolIn):
    try:
        return tool_store.create_tool(body.model_dump(exclude_none=True))
    except tool_store.ToolError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{tool_id}")
async def get_tool(tool_id: str):
    t = tool_store.get_tool(tool_id)
    if not t:
        raise HTTPException(404, "no such tool")
    return t


@router.put("/{tool_id}")
async def update_tool(tool_id: str, body: ToolIn):
    try:
        t = tool_store.update_tool(tool_id, body.model_dump(exclude_unset=True))
    except tool_store.ToolError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not t:
        raise HTTPException(404, "no such tool")
    return t


@router.delete("/{tool_id}")
async def delete_tool(tool_id: str):
    if not tool_store.delete_tool(tool_id):
        raise HTTPException(404, "no such tool")
    return {"deleted": tool_id}


@router.post("/{tool_id}/test")
async def test_tool(tool_id: str, body: TestIn):
    """Run the tool with sample args and return the raw result."""
    tool = tool_store.get_tool(tool_id)
    if not tool:
        raise HTTPException(404, "no such tool")
    try:
        return await custom_tools.test_tool(tool, body.args)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"test failed: {exc}") from exc
