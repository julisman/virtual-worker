# Tasks — the mentor's window onto an intern's Kanban board.

"""GET/POST /api/agents/{id}/tasks, PUT/DELETE /api/agents/{id}/tasks/{taskId},
GET/POST /api/agents/{id}/tasks/{taskId}/comments.

The intern also drives this board from inside the loop via its tools; this route
is the human (mentor) side — create/assign work, drag cards across columns, read
and reply on a task's thread. Mentor-side mutations are logged as activity.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import activity_store, agent_store, task_store

logger = logging.getLogger(__name__)

router = APIRouter()


class TaskIn(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    orderKey: float | None = None
    resultSummary: str | None = None
    dueAt: str | None = None


class CommentIn(BaseModel):
    text: str
    author: str | None = "mentor"


def _require_agent(agent_id: str) -> None:
    if not agent_store.get_agent(agent_id):
        raise HTTPException(404, "no such agent")


@router.get("/{agent_id}/tasks")
async def list_tasks(agent_id: str, status: str | None = None):
    _require_agent(agent_id)
    return {"tasks": task_store.list_for(agent_id, status=status), "counts": task_store.counts(agent_id)}


@router.post("/{agent_id}/tasks")
async def create_task(agent_id: str, body: TaskIn):
    _require_agent(agent_id)
    if not (body.title or "").strip():
        raise HTTPException(400, "title is required")
    t = task_store.create(agent_id, body.model_dump(exclude_none=True), origin="mentor")
    activity_store.log(agent_id, "task_created", f"Mentor created task: {t['title']}",
                       task_id=t["id"], detail={"by": "mentor", "priority": t["priority"]})
    return t


@router.put("/{agent_id}/tasks/{task_id}")
async def update_task(agent_id: str, task_id: str, body: TaskIn):
    _require_agent(agent_id)
    before = task_store.get(agent_id, task_id)
    if not before:
        raise HTTPException(404, "no such task")
    t = task_store.update(agent_id, task_id, body.model_dump(exclude_unset=True))
    if t and body.status and body.status != before["status"]:
        activity_store.log(agent_id, "task_moved",
                           f"Mentor moved {t['title']}: {before['status']} → {body.status}",
                           task_id=task_id, detail={"from": before["status"], "to": body.status, "by": "mentor"})
    return t


@router.delete("/{agent_id}/tasks/{task_id}")
async def delete_task(agent_id: str, task_id: str):
    _require_agent(agent_id)
    if not task_store.delete(agent_id, task_id):
        raise HTTPException(404, "no such task")
    return {"deleted": task_id}


@router.get("/{agent_id}/tasks/{task_id}/comments")
async def list_comments(agent_id: str, task_id: str):
    _require_agent(agent_id)
    if not task_store.get(agent_id, task_id):
        raise HTTPException(404, "no such task")
    return {"comments": task_store.list_comments(task_id)}


@router.post("/{agent_id}/tasks/{task_id}/comments")
async def add_comment(agent_id: str, task_id: str, body: CommentIn):
    _require_agent(agent_id)
    if not task_store.get(agent_id, task_id):
        raise HTTPException(404, "no such task")
    if not body.text.strip():
        raise HTTPException(400, "text is required")
    c = task_store.add_comment(task_id, body.author or "mentor", body.text)
    return c
