"""Team mode — the opt-in trust-boundary switch (PRD-07).

Solo mode (the default) is **off**: when ``is_enabled()`` is False the studio
behaves exactly like today — no auth, no login wall, localhost trust. Team mode is
flipped on explicitly the moment the first Owner account is created.

All state lives in ``<VW_HOME>/settings.json`` under a ``team`` block:

    "team": { "enabled": bool, "secret": "<hex>", "sessionDays": int }

The ``secret`` (generated locally, once) signs session cookies with stdlib
``hmac`` — no JWT library, no network, no external secret store. The cookie value
is ``"<rawToken>.<hmac_hex>"``; a tampered token fails the constant-time signature
check and is rejected before any DB lookup.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from storage import settings

COOKIE_NAME = "vw_session"
_KEY = "team"


def _block() -> dict:
    b = settings.get(_KEY) or {}
    return b if isinstance(b, dict) else {}


def is_enabled() -> bool:
    return bool(_block().get("enabled"))


def session_days() -> int:
    try:
        return max(1, int(_block().get("sessionDays") or 7))
    except (TypeError, ValueError):
        return 7


def _secret() -> bytes:
    """Return the app signing secret, generating + persisting it on first use."""
    b = _block()
    sec = b.get("secret")
    if not sec:
        sec = secrets.token_hex(32)
        b["secret"] = sec
        settings.set(_KEY, b)
    return sec.encode("utf-8")


def enable() -> None:
    b = _block()
    b["enabled"] = True
    settings.set(_KEY, b)
    _secret()  # ensure a signing secret exists once we start issuing cookies


def disable() -> None:
    b = _block()
    b["enabled"] = False
    settings.set(_KEY, b)


# --------------------------------------------------------------------------- #
# Cookie signing (stdlib hmac, constant-time verify)
# --------------------------------------------------------------------------- #

def _sign(raw: str) -> str:
    return hmac.new(_secret(), raw.encode("utf-8"), sha256).hexdigest()


def make_cookie(raw: str) -> str:
    """Wrap a raw session token into a signed cookie value."""
    return f"{raw}.{_sign(raw)}"


def read_cookie(value: str | None) -> str | None:
    """Verify a cookie's signature (constant-time) and return the raw token, or
    None if it's missing/malformed/tampered. Does NOT hit the DB."""
    if not value or "." not in value:
        return None
    raw, _, sig = value.rpartition(".")
    if not raw or not sig:
        return None
    if not hmac.compare_digest(sig, _sign(raw)):
        return None
    return raw


# --------------------------------------------------------------------------- #
# Login rate limiting (in-memory sliding window; per username and per client)
# --------------------------------------------------------------------------- #

import time as _time
from collections import deque as _deque


class LoginLimiter:
    """Refuse a login attempt once ``max_failures`` failed attempts for the same
    key (username or client address) happened within ``window_s`` seconds. A
    successful login clears the key. Process-local — enough for one instance."""

    def __init__(self, max_failures: int = 10, window_s: int = 15 * 60):
        self.max_failures = max_failures
        self.window_s = window_s
        self._fails: dict[str, _deque] = {}

    def _prune(self, key: str, now: float) -> _deque:
        q = self._fails.setdefault(key, _deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        now = _time.monotonic()
        return any(len(self._prune(k, now)) >= self.max_failures for k in keys if k)

    def failed(self, *keys: str) -> None:
        now = _time.monotonic()
        for k in keys:
            if k:
                self._prune(k, now).append(now)

    def succeeded(self, *keys: str) -> None:
        for k in keys:
            self._fails.pop(k, None)


login_limiter = LoginLimiter()
