"""Agent inbox — inspect and manage flow-back notifications."""

from fastapi import APIRouter, HTTPException

from storage import inbox_store

router = APIRouter()

_VISIBLE_STATUSES = ["pending", "processing", "processed", "failed"]


@router.get("/{agent_id}/inbox")
async def list_inbox(agent_id: str, status: str | None = None):
    """List inbox items, hiding dismissed notifications by default."""
    if not status:
        statuses = ["pending", "failed"]
    elif status == "all":
        # "all" means all currently visible work/history; dismissed items are
        # intentionally omitted from the inbox view.
        statuses = _VISIBLE_STATUSES
    else:
        statuses = [status]
    return {"items": inbox_store.list_items(agent_id, statuses)}


@router.post("/{agent_id}/inbox/{item_id}/retry")
async def retry_inbox_item(agent_id: str, item_id: str):
    item = inbox_store.retry(agent_id, item_id)
    if not item:
        raise HTTPException(404, "no such inbox item")
    return item


@router.delete("/{agent_id}/inbox/{item_id}")
async def dismiss_inbox_item(agent_id: str, item_id: str):
    item = inbox_store.mark(agent_id, item_id, "dismissed")
    if not item:
        raise HTTPException(404, "no such inbox item")
    return item
