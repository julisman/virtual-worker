"""User management routes (PRD-07) — list / invite / change-role / remove.

Prefix ``/api/users``. Server-side role gates (the UI is only a convenience):
  * **list / invite** — Admin or Owner
  * **change role / remove** — Owner only (and you can't demote/delete yourself,
    nor remove the last Owner — that would orphan the studio)

Deferred to a later milestone (documented in the PR): per-user grants
(``run_finetune`` / ``use_shell``), per-resource ``owner_id``/``visibility``
filtering, and the append-only audit log. M1 ships the auth chokepoint + the
user-management surface that the trust boundary depends on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth_deps import require_min_role
from storage import user_store

router = APIRouter()


class InviteRequest(BaseModel):
    username: str
    password: str
    role: str = "member"


class RoleRequest(BaseModel):
    role: str


def _owners() -> int:
    return sum(1 for u in user_store.list_users() if u["role"] == "owner")


@router.get("")
async def list_users(_actor=Depends(require_min_role("admin"))):
    return {"users": user_store.list_users()}


@router.post("")
async def invite_user(req: InviteRequest, actor=Depends(require_min_role("admin"))):
    role = req.role or "member"
    if role not in user_store.ROLES:
        raise HTTPException(400, f"invalid role: {role}")
    if role == "owner" and actor["role"] != "owner":
        raise HTTPException(403, "only an owner can create another owner")
    try:
        user = user_store.create_user(req.username, req.password, role=role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user": user}


@router.patch("/{user_id}")
async def change_role(user_id: str, req: RoleRequest,
                      actor=Depends(require_min_role("owner"))):
    if req.role not in user_store.ROLES:
        raise HTTPException(400, f"invalid role: {req.role}")
    target = user_store.get_user(user_id)
    if not target:
        raise HTTPException(404, "no such user")
    # Don't let the last owner demote themselves into a lockout.
    if target["role"] == "owner" and req.role != "owner" and _owners() <= 1:
        raise HTTPException(400, "cannot demote the last owner")
    updated = user_store.update_role(user_id, req.role)
    return {"user": updated}


@router.delete("/{user_id}")
async def remove_user(user_id: str, request: Request,
                      actor=Depends(require_min_role("owner"))):
    target = user_store.get_user(user_id)
    if not target:
        raise HTTPException(404, "no such user")
    if target["id"] == actor["id"]:
        raise HTTPException(400, "you cannot remove your own account")
    if target["role"] == "owner" and _owners() <= 1:
        raise HTTPException(400, "cannot remove the last owner")
    user_store.delete_user(user_id)
    return {"deleted": user_id}
