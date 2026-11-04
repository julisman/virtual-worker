"""Team-mode auth chokepoint (PRD-07).

A single dependency-free gate in front of every ``/api/*`` route. In **solo mode**
(``team.is_enabled() is False``) it is a transparent no-op — the request is never
touched, so default localhost behavior is byte-for-byte unchanged.

In **Team mode** it resolves the signed session cookie to a user, stashes it on
``request.state.user`` for downstream role checks, and rejects unauthenticated
``/api`` calls with ``401`` — except the small allow-list needed to log in or to
bootstrap the very first Owner.

The gateway (``/v1*``) keeps its own ``sk-vw-…`` token auth and is NOT gated
here; ``/health`` and non-``/api`` paths (static assets) pass through.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import team
from storage import user_store

# Reachable without a session even in Team mode: the login call and the guarded
# first-Owner bootstrap (which enforces its own count==0 / inviter rules).
_ALLOW = ("/api/auth/login", "/api/auth/register")
# Inbound webhooks carry their own proof (HMAC signature / unguessable token) and
# are called by machines that can't hold a session cookie.
_ALLOW_PREFIXES = ("/api/channels/webhook/", "/api/triggers/hook/")


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
        if (path.startswith("/api") and path not in _ALLOW and user is None
                and not path.startswith(_ALLOW_PREFIXES)):
            return JSONResponse({"detail": "authentication required"}, status_code=401)

        return await call_next(request)
