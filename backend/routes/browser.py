# Browser control — one-click "open a window so I can log in" for the shared,
# persistent, visible browser the agents drive.

"""POST /api/browser/open, POST /api/browser/close, GET /api/browser/status.

The interactive browser (browser_open / _click / _type tools) uses a persistent
profile so logins stick. This human-facing route lets the user pop the window open
and sign in **once** — without having to chat an agent — before running any skill
that drives the browser. It calls the very same ``agent.browser_control`` functions
the tools use, so the session it establishes is the one the agent inherits.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
from agent import browser_control
from storage import settings

logger = logging.getLogger(__name__)

router = APIRouter()


class OpenIn(BaseModel):
    url: str = "about:blank"


class ClickSelectorIn(BaseModel):
    selector: str = ""


# Persisted defaults for the ``browser`` settings object — mirror the resolver
# defaults in config.py (a stored value of None means "use the built-in").
_BROWSER_DEFAULTS = {
    "controlMode": "visible",
    "focusPolicy": "allow",
    "channel": None,
    "cdpUrl": "",
    "fetchHeadless": True,
    "profileDir": None,
}


class BrowserSettingsIn(BaseModel):
    controlMode: str = "visible"
    focusPolicy: str = "allow"
    channel: str | None = None
    cdpUrl: str = ""
    fetchHeadless: bool = True
    profileDir: str | None = None


@router.post("/open")
async def open_browser(body: OpenIn):
    """Open (or focus) the visible browser at a URL so the user can log in."""
    url = (body.url or "").strip() or "about:blank"
    if url == "about:blank":
        # Just launch the window; nothing to navigate to yet.
        if not browser_control._available():
            return {"ok": False, "message": browser_control._needs_browser()}
        try:
            ctx = await browser_control._get_context()
            await browser_control._active_page(ctx)
            note = browser_control._pop_attach_note()
            return {"ok": True,
                    "message": note + "Browser is open — log in, then run your skill."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"could not open browser: {exc}"}
    result = await browser_control.browser_open(url)
    ok = not result.startswith("browser_open failed") and "needs Playwright" not in result
    return {"ok": ok, "message": result}


@router.post("/click_selector")
async def click_selector(body: ClickSelectorIn):
    """Click an element by CSS selector (or XPath) in the agent's browser — the
    same ``browser_click_selector`` tool the agents use, exposed to a CLI sidecar
    so it can drive the persistent browser. An empty selector is rejected without
    touching the browser."""
    selector = (body.selector or "").strip()
    if not selector:
        return {"ok": False,
                "message": "click_selector needs a 'selector' — a CSS selector, or an "
                           "XPath starting with // ."}
    result = await browser_control.browser_click_selector(selector)
    # Mirror /open's heuristic: a failure result starts with the tool's own error
    # prefix ("browser_click_selector…"/"No page open…") or asks for Playwright.
    ok = (not result.startswith("browser_click_selector")
          and not result.startswith("No page open")
          and "needs Playwright" not in result)
    return {"ok": ok, "message": result}


@router.post("/close")
async def close_browser():
    """Close the Virtual Worker-launched browser; in attach mode this only disconnects —
    the user's own Chrome stays open."""
    await browser_control.close_control_browser()
    return {"ok": True}


@router.post("/relaunch")
async def relaunch_browser():
    """Tear down the control browser context so launch-time settings (mode,
    channel, CDP URL, profile) apply on the next tool call / open — no backend
    restart needed. In attach mode this only disconnects (the user's Chrome
    stays open); the next browser_open re-attaches / relaunches with the current
    settings."""
    await browser_control.close_control_browser()
    return {"ok": True,
            "message": "Agent browser closed — it relaunches with the current "
                       "settings on the next browser action."}


def _env_overrides() -> dict[str, bool]:
    """Which browser settings are currently pinned by an env var (presence check
    — a value equal to the default still counts as an override)."""
    import os
    return {key: (env in os.environ) for key, env in config.BROWSER_ENV_VARS.items()}


@router.get("/settings")
async def get_settings():
    """The persisted Settings → Browser values, plus which fields an env var is
    currently overriding (so the UI can flag them as read-only)."""
    stored = settings.get("browser") or {}
    if not isinstance(stored, dict):
        stored = {}
    values = {**_BROWSER_DEFAULTS, **{k: stored[k] for k in _BROWSER_DEFAULTS if k in stored}}
    return {**values, "envOverrides": _env_overrides()}


@router.put("/settings")
async def put_settings(body: BrowserSettingsIn):
    """Persist the Settings → Browser values. focusPolicy applies live (read per
    call); the launch-time fields take effect on the next relaunch/open."""
    if body.controlMode not in ("visible", "headless"):
        raise HTTPException(400, "controlMode must be 'visible' or 'headless'")
    if body.focusPolicy not in ("allow", "never"):
        raise HTTPException(400, "focusPolicy must be 'allow' or 'never'")
    if body.channel not in (None, "", "chrome", "msedge"):
        raise HTTPException(400, "channel must be null, '', 'chrome' or 'msedge'")
    stored = {
        "controlMode": body.controlMode,
        "focusPolicy": body.focusPolicy,
        "channel": body.channel,
        "cdpUrl": (body.cdpUrl or "").strip(),
        "fetchHeadless": bool(body.fetchHeadless),
        "profileDir": (body.profileDir or None),
    }
    settings.set("browser", stored)
    return {**stored, "envOverrides": _env_overrides()}


@router.get("/cookies")
async def cookies(url: str = ""):
    """Return the control browser's cookies for a URL/origin (login session).

    Local-first credential handoff: a signing sidecar reads
    the freshly logged-in session from here instead of having the cookies pasted in
    by hand. Includes ``httpOnly`` cookies (page JS can't). Requires an already-open
    browser — returns an empty set otherwise (nothing is logged in yet), and never
    launches one just to read cookies.

    ``url`` should be the target origin (e.g. ``http://portal.example.com``); it
    scopes the result to cookies that origin would receive. Returns both a
    ``cookies`` name→value map (ready for a Cookie header) and the raw ``details``."""
    raw = await browser_control.get_cookies((url or "").strip())
    return {"ok": True,
            "count": len(raw),
            "cookies": {c["name"]: c["value"] for c in raw},
            "details": raw}


@router.get("/status")
async def status():
    return {"available": browser_control._available(),
            "open": browser_control._ctx is not None,
            # the agent's current working-tab URL ("" when no tab/browser is open);
            # read-only — never launches a browser as a side effect.
            "pageUrl": browser_control.current_page_url(),
            "headless": config.control_browser_headless(),
            "focusPolicy": config.browser_focus_policy(),
            # attached = actually connected to the user's Chrome right now;
            # attachConfigured = a CDP URL is resolved (attach is attempted
            # first, with fallback to launching).
            "attached": browser_control._attached(),
            "attachConfigured": bool(config.browser_cdp_url())}
