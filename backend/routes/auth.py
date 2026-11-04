"""Auth routes (PRD-07) — register/bootstrap, login, logout, me.

Prefix ``/api/auth``. These are the only ``/api`` routes reachable without a
session in Team mode (the auth chokepoint allow-lists ``/login`` and
``/register``). Login issues a signed, HttpOnly, SameSite=Strict session cookie.

Solo mode: ``GET /me`` simply reports ``teamMode: false`` and no user — the
frontend uses that to know it must NOT show a login wall.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

import team
from auth_deps import current_user
from storage import user_store
from storage.user_store import ROLE_RANK

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str | None = None  # ignored for the first (Owner) account


class LoginRequest(BaseModel):
    username: str
    password: str


def _set_session_cookie(response: Response, user_id: str) -> None:
    raw = user_store.create_session(user_id, ttl_days=team.session_days())
    response.set_cookie(
        key=team.COOKIE_NAME,
        value=team.make_cookie(raw),
        httponly=True,
        samesite="strict",
        secure=False,  # local-first: served over http on localhost
        max_age=team.session_days() * 24 * 3600,
        path="/",
    )


@router.post("/register")
async def register(req: RegisterRequest, request: Request, response: Response):
    """Create the first Owner (bootstraps Team mode) OR, once accounts exist, let
    an Owner/Admin invite a new user. The first account is always the Owner and
    is logged in immediately (so enabling Team mode can't lock the operator out).
    """
    if user_store.count() == 0:
        try:
            user = user_store.create_user(req.username, req.password, role="owner")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        team.enable()  # flip the trust boundary — auth now required from here on
        _set_session_cookie(response, user["id"])
        logger.info("team mode enabled; owner bootstrapped")
        return {"user": user, "teamMode": True}

    # Invite path — requires an authenticated Admin or Owner.
    actor = current_user(request)
    if actor is None or ROLE_RANK.get(actor["role"], -1) < ROLE_RANK["admin"]:
        raise HTTPException(403, "only an owner or admin can add users")
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


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    user = user_store.verify_password(req.username, req.password)
    if not user:
        # Same generic message + timing for unknown user vs. wrong password.
        raise HTTPException(401, "invalid username or password")
    _set_session_cookie(response, user["id"])
    return {"user": user}


@router.post("/logout")
async def logout(request: Request, response: Response):
    raw = team.read_cookie(request.cookies.get(team.COOKIE_NAME))
    if raw:
        user_store.destroy_session(raw)
    response.delete_cookie(key=team.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    """Current principal. In solo mode -> teamMode false, user null (no login
    wall). In Team mode the chokepoint guarantees a user is present here."""
    return {"teamMode": team.is_enabled(), "user": current_user(request)}


@router.post("/disable-team")
async def disable_team(request: Request, response: Response):
    """Owner-only: turn Team mode back off (return to solo localhost trust)."""
    actor = current_user(request)
    if actor is None or actor["role"] != "owner":
        raise HTTPException(403, "only an owner can disable team mode")
    team.disable()
    response.delete_cookie(key=team.COOKIE_NAME, path="/")
    logger.info("team mode disabled by owner")
    return {"teamMode": False}
