"""ChatGPT subscription sign-in: OAuth (PKCE) through the public Codex CLI client.

Lets an agent run on a ChatGPT Plus/Pro subscription instead of an API key, the
same way the open-source Codex CLI does:

  1. Login (browser PKCE): open the OpenAI authorize URL and run a one-shot
     loopback listener on ``localhost:1455`` for the redirect, which is the URI
     the public Codex client id is registered for. The code is exchanged for
     access, refresh and id tokens.
  2. Derive credentials: the id token's ``https://api.openai.com/auth`` claim
     yields the ChatGPT account id; when it carries an ``organization_id`` the id
     token is traded for an ``openai-api-key`` (token-exchange grant), otherwise
     the raw access token is used. Calls then go to the Codex Responses API
     (``egress.py``, kind ``codex``) with that bearer plus the account-id header.
  3. Refresh: only the refresh token is durable (in the ``openai`` provider
     record under ``settings.json``); the key and account id are re-derived on
     demand with a five-minute margin.

Opt-in and explicit: nothing is sent anywhere until an agent is switched to the
OpenAI subscription source. The client id and flow are the Codex CLI's public
values; whether a subscription may be used this way is governed by OpenAI's
terms for the account, which the operator accepts by signing in.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from gateway import providers

logger = logging.getLogger(__name__)

AUTH_ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = AUTH_ISSUER + "/oauth/authorize"
TOKEN_URL = AUTH_ISSUER + "/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"   # the open-source Codex CLI's public client
REDIRECT_PORT = 1455
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/auth/callback"
SCOPE = "openid profile email offline_access"
ORIGINATOR = "codex_cli_rs"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
AUTH_CLAIM = "https://api.openai.com/auth"
CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
EXPIRY_BUFFER_S = 300

PROVIDER = "openai"


class OAuthError(Exception):
    pass


# --------------------------------------------------------------------------- #
# PKCE + URL building
# --------------------------------------------------------------------------- #

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _gen_verifier() -> str:
    return _b64url(os.urandom(32))


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _gen_state() -> str:
    return _b64url(os.urandom(32))


def build_authorize_url(challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": ORIGINATOR,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# --------------------------------------------------------------------------- #
# Token endpoint (sync; called from the callback thread or via to_thread)
# --------------------------------------------------------------------------- #

def _post_token(form: dict) -> dict:
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError(f"token endpoint unreachable: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise OAuthError(f"token endpoint {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _decode_jwt(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


def _auth_claim(id_token: str) -> dict:
    claim = _decode_jwt(id_token).get(AUTH_CLAIM)
    return claim if isinstance(claim, dict) else {}


def _exchange_for_api_key(id_token: str) -> str:
    body = _post_token({
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "client_id": CLIENT_ID,
        "requested_token": "openai-api-key",
        "subject_token": id_token,
        "subject_token_type": SUBJECT_TOKEN_TYPE,
    })
    return body.get("access_token") or ""


def _store_from_tokens(tokens: dict) -> dict:
    """Derive and persist credentials from a token response. Only the refresh
    token is durable; the key and account id are a re-derivable cache."""
    access = tokens.get("access_token") or ""
    id_token = tokens.get("id_token") or ""
    refresh = tokens.get("refresh_token") or ""
    claim = _auth_claim(id_token)
    account_id = claim.get("chatgpt_account_id") or ""
    org_id = claim.get("organization_id") or ""

    api_key = access
    if id_token and org_id:
        try:
            api_key = _exchange_for_api_key(id_token) or access
        except OAuthError as exc:
            logger.debug("api-key exchange failed, using access token: %s", exc)
            api_key = access

    blob = {
        "refreshToken": refresh,
        "apiKey": api_key,
        "accountId": account_id,
        "expiresAt": time.time() + float(tokens.get("expires_in") or 3600),
    }
    providers.set_oauth(PROVIDER, blob)
    return blob


# --------------------------------------------------------------------------- #
# Credential access (refresh on demand)
# --------------------------------------------------------------------------- #

_refresh_lock = threading.Lock()


def _refresh() -> tuple[str | None, str | None]:
    # Serialize: two concurrent runs must not both spend the refresh grant.
    with _refresh_lock:
        return _refresh_locked()


def _refresh_locked() -> tuple[str | None, str | None]:
    blob = providers.get_oauth(PROVIDER)
    refresh = blob.get("refreshToken")
    if not refresh:
        return None, None
    if blob.get("apiKey") and (blob.get("expiresAt", 0) - time.time()) > EXPIRY_BUFFER_S:
        return blob["apiKey"], blob.get("accountId")
    try:
        tokens = _post_token({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh,
        })
    except OAuthError as exc:
        logger.warning("ChatGPT token refresh failed: %s", exc)
        return blob.get("apiKey"), blob.get("accountId")
    if not tokens.get("refresh_token"):
        tokens["refresh_token"] = refresh
    fresh = _store_from_tokens(tokens)
    return fresh.get("apiKey"), fresh.get("accountId")


async def active_credential() -> tuple[str | None, str | None]:
    """``(api_key, account_id)`` for the Codex call, or ``(None, None)``."""
    return await asyncio.to_thread(_refresh)


def is_connected() -> bool:
    return bool(providers.get_oauth(PROVIDER).get("refreshToken"))


def account_id() -> str:
    return providers.get_oauth(PROVIDER).get("accountId") or ""


def disconnect() -> None:
    providers.set_oauth(PROVIDER, None)
    with _lock:
        _login.update(status="idle", verifier=None, state=None, error=None, account=None)


# --------------------------------------------------------------------------- #
# Login flow: loopback callback listener on :1455
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_login: dict = {"status": "idle", "verifier": None, "state": None, "error": None, "account": None}
_server: HTTPServer | None = None

_SUCCESS_HTML = (
    b"<!doctype html><html><body style='font-family:sans-serif;text-align:center;"
    b"padding-top:4rem'><h2>Signed in to ChatGPT</h2>"
    b"<p>You can close this tab and return to Virtual Worker.</p></body></html>"
)


def _error_html(msg: str) -> bytes:
    safe = msg[:200].replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><html><body style='font-family:sans-serif;text-align:center;"
        "padding-top:4rem'><h2>Sign-in failed</h2><p>" + safe + "</p></body></html>"
    ).encode()


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _respond(self, status: int, html: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/auth/callback"):
            self._respond(404, b"not found")
            return
        q = parse_qs(parsed.query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        err = (q.get("error") or [""])[0]
        try:
            if err:
                raise OAuthError(err)
            with _lock:
                verifier, expected = _login.get("verifier"), _login.get("state")
            if not code:
                raise OAuthError("no authorization code in callback")
            if not expected or state != expected:
                raise OAuthError("state mismatch: restart the sign-in")
            tokens = _post_token({
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            })
            blob = _store_from_tokens(tokens)
            with _lock:
                _login.update(status="connected", account=blob.get("accountId"), error=None)
            self._respond(200, _SUCCESS_HTML)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChatGPT sign-in callback failed: %s", exc)
            with _lock:
                _login.update(status="error", error=str(exc))
            self._respond(400, _error_html(str(exc)))
        finally:
            threading.Thread(target=_stop_server, daemon=True).start()


def _stop_server() -> None:
    global _server
    with _lock:
        srv, _server = _server, None
    if srv:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass


def _ensure_server() -> None:
    global _server
    with _lock:
        if _server is not None:
            return
        try:
            _server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
        except OSError as exc:
            raise OAuthError(
                f"could not start the sign-in listener on port {REDIRECT_PORT}: {exc}. "
                "Close any other Codex or ChatGPT login in progress and try again.") from exc
    threading.Thread(target=_server.serve_forever, daemon=True).start()


def start_login() -> str:
    """Begin the PKCE login; return the authorize URL for the browser to open."""
    verifier = _gen_verifier()
    state = _gen_state()
    with _lock:
        _login.update(status="awaiting", verifier=verifier, state=state, error=None, account=None)
    _ensure_server()
    return build_authorize_url(_challenge(verifier), state)


def login_status() -> dict:
    with _lock:
        status, account, error = _login["status"], _login["account"], _login["error"]
    if status in ("idle", "awaiting") and is_connected():
        return {"status": "connected", "account": account_id(), "error": None}
    return {"status": status, "account": account or account_id(), "error": error}
