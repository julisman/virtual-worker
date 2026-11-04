"""Auth dependencies — the server-side capability checks for Team mode (PRD-07).

The UI hides what you can't do, but the boundary lives **here**: every gated route
re-derives the caller's role from the request principal and 401s/403s on its own.
``request.state.user`` is populated by ``TeamAuthMiddleware`` (None in solo mode or
when unauthenticated).
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from storage.user_store import ROLE_RANK


def current_user(request: Request) -> dict | None:
    """The authenticated user dict, or None (solo mode / unauthenticated)."""
    return getattr(request.state, "user", None)


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_min_role(min_role: str):
    """FastAPI dependency factory: require the caller's role to rank >= ``min_role``."""
    floor = ROLE_RANK[min_role]

    def _dep(request: Request) -> dict:
        user = require_user(request)
        if ROLE_RANK.get(user.get("role"), -1) < floor:
            raise HTTPException(status_code=403, detail=f"requires {min_role} role")
        return user

    return _dep
