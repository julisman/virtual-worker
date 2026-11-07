"""Team-mode gate: authentication, role policy and audit for every /api call.

A single dependency-free middleware in front of the whole ``/api`` surface. In
**solo mode** (the default) it does nothing at all. When team mode is on:

  1. **Authentication** — the signed session cookie is verified (constant time)
     and resolved to a user; an unauthenticated ``/api`` call gets ``401``,
     except the login/bootstrap routes and the inbound webhooks, which carry
     their own proof (HMAC signature / unguessable token).
  2. **Role policy** — mutating requests (POST/PUT/PATCH/DELETE) must meet the
     minimum role for their path (see ``required_role``): viewers are read-only,
     members operate workers, admins change installation-wide configuration and
     secrets, owners manage team mode itself. ``403`` otherwise.
  3. **Audit** — every mutating request is recorded with its principal and
     outcome in ``audit_log`` after the response, best-effort.

Routes may still add finer checks (``auth_deps.require_min_role``); this layer
is the floor, so a new route can never be accidentally open to a viewer.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import team
from storage import user_store
from storage.user_store import ROLE_RANK

logger = logging.getLogger(__name__)

# Reachable without a session even in Team mode: the login call and the guarded
# first-Owner bootstrap (which enforces its own count==0 / inviter rules).
_ALLOW = ("/api/auth/login", "/api/auth/register")
# Inbound webhooks carry their own proof (HMAC signature / unguessable token) and
# are called by machines that can't hold a session cookie.
_ALLOW_PREFIXES = ("/api/channels/webhook/", "/api/triggers/hook/")

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Minimum role for a MUTATING request, by path prefix (longest match wins).
# Anything under /api not listed defaults to "member".
_ROLE_POLICY: dict[str, str] = {
    # owner-only: the trust boundary itself
    "/api/auth/disable-team": "owner",
    # admin: installation-wide configuration, credentials, access, data lifecycle
    "/api/providers": "admin",
    "/api/channels": "admin",
    "/api/delivery-targets": "admin",
    "/api/tokens": "admin",
    "/api/users": "admin",
    "/api/auth/register": "admin",   # invites (the first-owner bootstrap is handled in the route)
    "/api/engines": "admin",
    "/api/runtime": "admin",
    "/api/hub": "admin",
    "/api/backup": "admin",
    "/api/harness": "admin",
    "/api/eval": "admin",       # switches the active model
    "/api/registries": "admin",
    "/api/browser": "admin",
    "/api/mcp": "admin",
    "/api/tools": "admin",
    "/api/debug": "admin",
    "/api/analytics": "admin",
    "/api/team": "admin",
    # member: everyday use of workers (explicit for clarity)
    "/api/chat": "member",
    "/api/sessions": "member",
    "/api/agents": "member",
    "/api/skills": "member",
    "/api/kb": "member",
    "/api/wiki": "member",
    "/api/templates": "member",
    "/api/triggers": "member",
    "/api/deliver": "member",
    "/api/feedback": "member",
    "/api/auth/logout": "viewer",
}


def required_role(method: str, path: str) -> str | None:
    """Minimum role for ``method path``; ``None`` when no role floor applies
    (reads, non-/api paths)."""
    if method.upper() not in _MUTATING or not path.startswith("/api"):
        return None
    best = None
    for prefix, role in _ROLE_POLICY.items():
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, role)
    return best[1] if best else "member"


class TeamAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Solo mode: do nothing at all (no state, no DB read, no behavior change).
        if not team.is_enabled():
            return await call_next(request)

        # Resolve the signed cookie -> user (None if absent/tampered/expired).
        raw = team.read_cookie(request.cookies.get(team.COOKIE_NAME))
        user = user_store.resolve_session(raw) if raw else None
        request.state.user = user

        path = request.url.path
        method = request.method.upper()
        exempt = path in _ALLOW or path.startswith(_ALLOW_PREFIXES)
        if path.startswith("/api") and not exempt and user is None:
            return JSONResponse({"detail": "authentication required"}, status_code=401)

        # Role floor for mutating calls (webhooks are machine calls; skip).
        if user is not None and not path.startswith(_ALLOW_PREFIXES):
            need = required_role(method, path)
            if need and ROLE_RANK.get(user.get("role"), -1) < ROLE_RANK[need]:
                self._audit(user, method, path, 403, request)
                return JSONResponse({"detail": f"requires {need} role"}, status_code=403)

        response = await call_next(request)
        if method in _MUTATING and path.startswith("/api") and not path.startswith(_ALLOW_PREFIXES):
            self._audit(user, method, path, response.status_code, request)
        return response

    @staticmethod
    def _audit(user, method, path, status, request) -> None:
        try:
            from storage import audit_store
            client = request.client.host if request.client else None
            audit_store.log(user=user, method=method, path=path, status=status, client=client)
        except Exception:  # noqa: BLE001
            logger.debug("audit write failed", exc_info=True)
