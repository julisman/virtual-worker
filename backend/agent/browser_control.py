"""Interactive browser control — a real, **visible**, **persistent** browser the
agent can drive step by step (open a page, look at what's clickable, click, type).

This is the counterpart to ``tools.browser_fetch``. Where ``browser_fetch`` opens
a **headless**, throwaway context just to read rendered text, the control browser:

  * is **headed** (visible) by default — so you can watch it and, crucially, **log
    in manually** to sites that need auth;
  * uses a **persistent profile** on disk (``config.BROWSER_PROFILE``) — so those
    logins (cookies, sessions) **stick across restarts**;
  * is **long-lived** — one context, one tab, reused across tool calls so a skill
    can carry out a multi-step flow on the same page the user sees.

Element targeting is **ref-based**: ``browser_snapshot`` tags every visible
interactive element with a sequential ``data-vw-ref`` and returns a numbered list
(``[1] button "Sign in"``). The model then ``browser_click(ref=1)`` /
``browser_type(ref=2, text=…)``. Refs are re-assigned on every snapshot, so the
open/click/type tools return a fresh snapshot after they act.

While the browser is open, console output and network traffic are captured
passively into bounded ring buffers; ``browser_console_messages`` and
``browser_network_requests`` read them back (method/status/type/URL only — no
headers or bodies).

Attach mode: set ``VW_BROWSER_CDP_URL`` (e.g. ``http://127.0.0.1:9222``) and the
tools drive **your own running Chrome** over CDP instead of launching a separate
one. Agent tabs open alongside yours in the same window; ``browser_tabs`` /
``browser_tab_select`` list and switch tabs (including yours, when you ask the
agent to work in one); the agent never steals a user tab on its own — with no
working tab it opens a new one. Virtual Worker never closes an attached browser: shutdown
and the "close" route just disconnect. Chrome must be started with
``--remote-debugging-port`` and a dedicated ``--user-data-dir`` (Chrome 136+
ignores the flag on the default profile). If nothing is listening at the CDP
URL, Virtual Worker **falls back to launching its own browser** and prefixes the next
``browser_open`` result with a one-time warning note (the two browsers have
separate profiles/logins, so the agent must know which one it's in). Screen
recording is a launch-time Playwright option, so it's unavailable whenever
attach is configured. Tabs Virtual Worker creates are marked with
``window.name = "vw-agent"``; when the Virtual Worker Companion extension is installed in
the attached Chrome, it gathers them into a colored "🦦 Virtual Worker" tab group
(``chrome.tabGroups`` is extension-only — CDP cannot group tabs).

Local-first: these tools are **opt-in per agent** (selected by name, like any
built-in) and driven explicitly by the user's skill — so, unlike ``browser_fetch``,
the control browser is **not** gated by the SSRF private-host guard (you'll want to
log into internal/intranet tools). Nothing here reaches the network unless a tool
is invoked with a URL.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections import deque
from datetime import datetime
from pathlib import Path

import config
from agent.fs_tools import FsError, _resolve_root, _safe

logger = __import__("logging").getLogger(__name__)

# Mask navigator.webdriver — same minimal stealth browser_fetch uses; harmless and
# keeps some sites from bouncing an automated-looking session mid-login.
_STEALTH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

_NAV_MS = 30_000            # navigation timeout
_SETTLE_MS = 3_000          # best-effort wait for JS to settle after nav/click
_ACT_MS = 10_000            # click/type action timeout
# CDP attach to a local Chrome completes in ~2s even with dozens of tabs; the
# Playwright default (180s) let ONE frozen tab hold every browser tool call —
# and the whole agent run — hostage for 3 minutes per call. Fail fast to the
# launched-browser fallback instead.
_ATTACH_MS = 10_000
_MAX_REFS = 200             # cap snapshot size
_MAX_READ = 6000            # default browser_read char cap

_pw = None                  # the Playwright driver
_ctx = None                 # the shared persistent BrowserContext
_cdp = None                 # the connected Browser handle in attach mode (CDP)
_current_page = None        # the tab the agent is working in (survives tool calls)
_lock = asyncio.Lock()
_record_dir: Path | None = None   # when set, contexts launch with video recording on
_REC_SIZE = {"width": 1280, "height": 720}

# DevTools-style capture: console messages + completed network requests, recorded
# passively while the control browser is open. Ring buffers survive the context
# bounces that recording start/stop cause (they're module state, not per-context).
# Network entries deliberately carry NO headers or bodies — method/status/type/URL
# is enough to debug an XHR behind a UI without teeing session tokens into the
# model context.
_MAX_LOG = 400              # ring-buffer cap (each of console / network)
_MAX_LIST = 50              # default entries returned by the read tools
_MAX_TEXT = 500             # per-console-message text cap
_MAX_URL = 300              # per-URL cap in network entries
_console_log: deque = deque(maxlen=_MAX_LOG)
_net_log: deque = deque(maxlen=_MAX_LOG)


def _needs_browser() -> str:
    from agent import tools
    return tools._NEEDS_BROWSER


def _available() -> bool:
    from agent import tools
    return tools.browser_available()


def current_page_url() -> str:
    """The agent's current working-tab URL, or "" when no live tab is tracked.

    Read-only and side-effect-free: it never opens a context or a page (so a
    status probe can't launch a browser). Any Playwright hiccup (closed page,
    driver gone) collapses to "" rather than raising."""
    try:
        page = _current_page
        if page is None or page.is_closed():
            return ""
        return page.url or ""
    except Exception:  # noqa: BLE001 — a status read must never raise/launch
        return ""


def _clean(text: str, max_chars: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text or "")).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…(+{len(text) - max_chars} chars truncated)"
    return text


def _add_console(level: str, text: str, location: str = "") -> None:
    _console_log.append({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "level": (level or "log"),
        "text": (text or "")[:_MAX_TEXT],
        "location": location or "",
    })


def _add_net(method: str, url: str, status, rtype: str, failure: str = "") -> None:
    _net_log.append({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "method": (method or "GET").upper(),
        "url": (url or "")[:_MAX_URL],
        "status": status,
        "type": rtype or "",
        "failure": failure or "",
    })


def _wire_page(page) -> None:
    """Attach console capture to one page (network is captured context-wide)."""
    def _on_console(msg):
        try:
            loc = msg.location or {}
            where = ""
            if loc.get("url"):
                where = f"{loc['url'][:_MAX_URL]}:{loc.get('lineNumber', 0)}"
            _add_console(msg.type, msg.text, where)
        except Exception:                # noqa: BLE001 — capture must never break the page
            pass

    def _on_pageerror(err):
        try:
            _add_console("error", f"Uncaught {err}")
        except Exception:                # noqa: BLE001
            pass

    try:
        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
    except Exception:                    # noqa: BLE001
        pass


def _wire_context(ctx) -> None:
    """Attach passive console/network capture to a freshly launched context."""
    def _on_response(resp):
        try:
            req = resp.request
            _add_net(req.method, resp.url, resp.status, req.resource_type)
        except Exception:                # noqa: BLE001
            pass

    def _on_requestfailed(req):
        try:
            _add_net(req.method, req.url, None, req.resource_type,
                     req.failure or "failed")
        except Exception:                # noqa: BLE001
            pass

    try:
        ctx.on("response", _on_response)
        ctx.on("requestfailed", _on_requestfailed)
        ctx.on("page", _wire_page)
        for p in ctx.pages:
            _wire_page(p)
    except Exception:                    # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Persistent headed context — lazy, shared, self-relaunching.
# --------------------------------------------------------------------------- #

# A fixed launch window size — combined with no_viewport=True below, Playwright's
# "viewport" (what page.screenshot() sees) tracks this window's actual content
# area, so pages render at full desktop width instead of the small default
# window, regardless of the host's physical display.
_WINDOW_SIZE = "1920,1080"


def _launch_kwargs() -> dict:
    """Launch options for the persistent context (recording-aware)."""
    launch_kw = dict(
        user_data_dir=str(config.browser_profile()),
        headless=config.control_browser_headless(),
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled",
              f"--window-size={_WINDOW_SIZE}", "--window-position=0,0"],
        user_agent=_UA, locale="en-US", no_viewport=True,
    )
    channel = config.browser_channel()
    if channel:                         # drive system Chrome/Edge instead of bundled Chromium
        launch_kw["channel"] = channel
    if _record_dir is not None:         # video recording is a launch-time option
        launch_kw["record_video_dir"] = str(_record_dir)
        launch_kw["record_video_size"] = dict(_REC_SIZE)
    return launch_kw


_attach_note = ""           # set when attach fell back to launching; shown once


def _pop_attach_note() -> str:
    """Return (and clear) the pending fallback note, prefixed for a tool result."""
    global _attach_note
    note, _attach_note = _attach_note, ""
    return f"⚠️ {note}\n\n" if note else ""


def _attached() -> bool:
    """Whether the live context is the user's own Chrome (CDP attach)."""
    return _cdp is not None


async def _get_context():
    """Return a live shared BrowserContext — attached to the user's own Chrome
    when ``config.BROWSER_CDP_URL`` is set and reachable, else a Virtual Worker-launched
    persistent one (attach failure falls back to launching, with a note)."""
    global _pw, _ctx, _cdp, _attach_note
    from playwright.async_api import async_playwright
    async with _lock:
        if _ctx is not None:
            try:
                _ = _ctx.pages           # cheap probe; the close handler nulls _ctx
                return _ctx
            except Exception:            # noqa: BLE001 — context died; relaunch
                _ctx = None
        if _pw is None:
            _pw = await async_playwright().start()

        cdp_url = config.browser_cdp_url()
        if cdp_url:                      # attach to the user's running Chrome
            try:
                _cdp = await _pw.chromium.connect_over_cdp(cdp_url, timeout=_ATTACH_MS)
            except Exception as exc:     # noqa: BLE001 — fall back to launching
                _cdp = None
                logger.warning("CDP attach to %s failed (%s) — falling back to "
                               "the Virtual Worker-launched browser", cdp_url, exc)
                _attach_note = (
                    f"couldn't attach to your Chrome at {cdp_url} "
                    f"({exc}) — using the Virtual Worker-launched browser instead (separate "
                    "profile, so your Chrome's logins aren't here). To attach, "
                    "start Chrome with --remote-debugging-port=9222 and a "
                    "dedicated --user-data-dir, then close this browser.")
            else:
                _ctx = _cdp.contexts[0] if _cdp.contexts else await _cdp.new_context()

                def _gone(*_a):          # Chrome quit / connection dropped
                    global _ctx, _cdp, _current_page
                    _ctx = _cdp = _current_page = None
                try:
                    _cdp.on("disconnected", _gone)
                except Exception:        # noqa: BLE001
                    pass
                _wire_context(_ctx)
                return _ctx

        config.ensure_dir(config.browser_profile())
        _ctx = await _pw.chromium.launch_persistent_context(**_launch_kwargs())
        try:
            await _ctx.add_init_script(_STEALTH)
        except Exception:                # noqa: BLE001
            pass
        _wire_context(_ctx)

        def _closed(*_a):                # user closed the window → drop the handles
            global _ctx, _current_page
            _ctx = _current_page = None
        try:
            _ctx.on("close", _closed)
        except Exception:                # noqa: BLE001
            pass
        return _ctx


# Tabs Virtual Worker creates carry this marker in window.name: it survives navigation
# within the tab, and the Virtual Worker Companion extension's content script sees it and
# moves the tab into a "🦦 Virtual Worker" tab group (chrome.tabGroups is extension-only —
# CDP can't group tabs itself). Purely cosmetic if the extension isn't there.
_AGENT_TAB_MARK = "vw-agent"


async def _mark_agent_tab(page) -> None:
    try:
        await page.evaluate(f"window.name = '{_AGENT_TAB_MARK}'")
    except Exception:                    # noqa: BLE001 — cosmetic, never fatal
        pass


async def _new_agent_tab(ctx):
    """Open a new tab and mark it as Virtual Worker's (for extension tab-grouping)."""
    page = await ctx.new_page()
    await _mark_agent_tab(page)
    return page


async def _active_page(ctx):
    """The agent's working tab, tracked across tool calls. When the tracked tab
    is gone: launched mode falls back to the window's last open tab (usually the
    only one); attach mode opens a NEW tab — it never hijacks one of the user's
    own tabs (switching to a user tab is explicit, via browser_tab_select)."""
    global _current_page
    cur = _current_page
    if cur is not None:
        try:
            # Must belong to THIS context: a relaunch/reconnect invalidates old
            # page handles even when is_closed() still answers.
            if not cur.is_closed() and cur in ctx.pages:
                return cur
        except Exception:                # noqa: BLE001 — stale handle
            pass
    if _attached():
        _current_page = await _new_agent_tab(ctx)
    else:
        pages = [p for p in ctx.pages if not p.is_closed()]
        _current_page = pages[-1] if pages else await _new_agent_tab(ctx)
    return _current_page


def _same_address(page_url: str, target_url: str) -> bool:
    """Whether an open tab is already on the address we're about to open, so it can
    be reused instead of navigating (which would refresh it) or opening a duplicate.

    Match rule: same host[:port], and — when the target has a real path — the same
    path+query (trailing slash and #fragment ignored). A target that is just an
    origin (``http://host`` / ``http://host/``) matches any tab on that host, so
    'open the admin console' reuses an existing tab of that site whatever page it's on."""
    from urllib.parse import urlparse
    try:
        p, t = urlparse(page_url or ""), urlparse(target_url or "")
    except Exception:                        # noqa: BLE001
        return False
    if not p.hostname or (p.hostname, p.port) != (t.hostname, t.port):
        return False
    t_path = (t.path or "").rstrip("/")
    if not t_path:                           # origin-only target → any tab on this host
        return True
    return (p.path or "").rstrip("/") == t_path and (p.query or "") == (t.query or "")


def _find_same_address_tab(ctx, url: str):
    """An open, non-closed tab already on ``url`` (see _same_address), or None."""
    for p in ctx.pages:
        try:
            if not p.is_closed() and _same_address(p.url, url):
                return p
        except Exception:                    # noqa: BLE001 — stale handle
            continue
    return None


def _find_blank_tab(ctx):
    """An open blank tab we can safely navigate (no user content to clobber)."""
    for p in ctx.pages:
        try:
            if not p.is_closed() and (not p.url or p.url == "about:blank"):
                return p
        except Exception:                    # noqa: BLE001
            continue
    return None


async def _settle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
    except Exception:                    # noqa: BLE001 — not fatal
        pass


async def _teardown_context() -> str:
    """Close the shared context (if any) and return the last open page's URL.

    Recording is a launch-time option, so start/stop must bounce the context;
    the returned URL lets the caller restore where the agent was."""
    global _ctx
    async with _lock:
        ctx, _ctx = _ctx, None
    if ctx is None:
        return ""
    url = ""
    try:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if pages and pages[-1].url and pages[-1].url != "about:blank":
            url = pages[-1].url
    except Exception:                    # noqa: BLE001
        pass
    try:
        await ctx.close()
    except Exception:                    # noqa: BLE001
        pass
    return url


async def get_cookies(url: str = "") -> list[dict]:
    """Return the control browser's cookies, optionally scoped to a URL/origin.

    Reads the live persistent context's real cookie jar via Playwright — so it
    includes ``httpOnly`` session cookies that page JS (``document.cookie``) can
    never see. ``url`` filters to cookies that would be sent to that URL (pass a
    full ``http(s)://host`` origin); empty returns every cookie in the context.

    Never launches a browser just to read cookies: if no context is open there is
    nothing logged in yet, so it returns ``[]``. Each cookie is a dict with
    ``name``/``value``/``domain``/``path``/``httpOnly``/``secure``/``expires``."""
    if _ctx is None:
        return []
    try:
        return await _ctx.cookies([url] if url else None)
    except Exception:                    # noqa: BLE001
        logger.debug("get_cookies failed", exc_info=True)
        return []


async def close_control_browser() -> None:
    """Tear down the control browser (call on app shutdown). In attach mode this
    only disconnects — the user's Chrome and all its tabs stay open."""
    global _pw, _ctx, _cdp, _current_page
    async with _lock:
        c, b, p = _ctx, _cdp, _pw
        _ctx = _cdp = _pw = _current_page = None
    if b is not None:
        try:                             # closing a connect_over_cdp handle only
            await b.close()              # drops the connection; Chrome keeps running
        except Exception:                # noqa: BLE001
            pass
    elif c is not None:
        try:
            await c.close()
        except Exception:                # noqa: BLE001
            pass
    if p is not None:
        try:
            await p.stop()
        except Exception:                # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Snapshot — tag visible interactive elements with refs, return a numbered list.
# --------------------------------------------------------------------------- #

_SNAPSHOT_JS = """(max) => {
  const q = 'a,button,input,textarea,select,[role=button],[role=link],[role=tab],'
    + '[role=menuitem],[role=checkbox],[role=radio],[contenteditable=true],summary,[onclick]';
  // Clear refs from earlier snapshots first: an element that was tagged while
  // visible but is skipped this pass (now hidden) would otherwise keep its old
  // number, colliding with whichever element gets that number this pass.
  for (const el of Array.from(document.querySelectorAll('[data-vw-ref]'))) {
    el.removeAttribute('data-vw-ref');
  }
  const out = [];
  let n = 0;
  for (const el of Array.from(document.querySelectorAll(q))) {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden'
        || st.display === 'none' || el.disabled) continue;
    n += 1;
    el.setAttribute('data-vw-ref', String(n));
    let role = el.getAttribute('role') || el.tagName.toLowerCase();
    if (el.tagName === 'INPUT') role = 'input:' + (el.getAttribute('type') || 'text');
    const name = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || el.value || el.innerText || el.getAttribute('name')
      || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    out.push('[' + n + '] ' + role + (name ? ' "' + name + '"' : ''));
    if (n >= max) break;
  }
  return out;
}"""


async def _snapshot_lines(page) -> list[str]:
    try:
        return await page.evaluate(_SNAPSHOT_JS, _MAX_REFS)
    except Exception as exc:             # noqa: BLE001
        return [f"(snapshot failed: {exc})"]


async def _where(page) -> str:
    try:
        title = (await page.title()) or ""
    except Exception:                    # noqa: BLE001
        title = ""
    return f"{title} — {page.url}".strip(" —")


async def _snapshot_block(page) -> str:
    lines = await _snapshot_lines(page)
    body = "\n".join(lines) if lines else "(no interactive elements found)"
    return f"Page: {await _where(page)}\n\nInteractive elements:\n{body}"


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

async def browser_open(url: str = "", new_tab: bool = False) -> str:
    global _current_page
    url = (url or "").strip()
    if not url:
        return "browser_open needs a 'url'."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        if new_tab:
            _current_page = await _new_agent_tab(ctx)
            page = _current_page
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_MS)
            await _settle(page)
            note = "Opened the page in a new tab."
            return _pop_attach_note() + note + "\n\n" + await _snapshot_block(page)

        # Default: reuse a tab already on this address; otherwise open a NEW tab.
        # Never navigate an existing tab that's on a different page — that would
        # refresh/hijack it (e.g. the user's own tab). A blank tab is fair game.
        existing = _find_same_address_tab(ctx, url)
        if existing is not None:
            _current_page = existing
            if config.browser_focus_policy() != "never":
                try:
                    await existing.bring_to_front()
                except Exception:            # noqa: BLE001 — focus is best-effort
                    pass
            await _settle(existing)
            note = "Reused the tab already open on this page (no reload)."
            return _pop_attach_note() + note + "\n\n" + await _snapshot_block(existing)

        blank = _find_blank_tab(ctx)
        if blank is not None:
            _current_page = blank
            await _mark_agent_tab(blank)
            note = "Opened the page."
        else:
            _current_page = await _new_agent_tab(ctx)
            note = "Opened the page in a new tab (left your other tabs untouched)."
        page = _current_page
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_MS)
        await _settle(page)
        return _pop_attach_note() + note + "\n\n" + await _snapshot_block(page)
    except Exception as exc:             # noqa: BLE001
        return "browser_open failed: " + _launch_hint(exc)


def _launch_hint(exc: Exception) -> str:
    """Add a 'install Chrome' hint when we drive system Chrome and it's missing."""
    msg = str(exc)
    if config.browser_channel() == "chrome" and (
            "executable doesn't exist" in msg.lower() or "channel" in msg.lower()
            or "chrome" in msg.lower()):
        return (f"{msg}\n\nThis build uses your system Google Chrome — please install "
                "Chrome (https://www.google.com/chrome/) and try again.")
    return msg


async def browser_snapshot() -> str:
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        if not page.url or page.url == "about:blank":
            return "No page open yet — call browser_open(url) first."
        return await _snapshot_block(page)
    except Exception as exc:             # noqa: BLE001
        return f"browser_snapshot failed: {exc}"


def _ref_selector(ref) -> str | None:
    # ':visible' guards against a ref that matches more than one element (the
    # page mutated after the snapshot): Playwright acts on the first match, and
    # without the filter a hidden stale duplicate can shadow the visible target.
    try:
        return f'[data-vw-ref="{int(ref)}"]:visible'
    except (TypeError, ValueError):
        return None


async def browser_click(ref: int = 0) -> str:
    if not _available():
        return _needs_browser()
    sel = _ref_selector(ref)
    if sel is None:
        return "browser_click needs a numeric 'ref' from the latest browser_snapshot."
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        await page.click(sel, timeout=_ACT_MS)
        await _settle(page)
        return f"Clicked [{int(ref)}].\n\n" + await _snapshot_block(page)
    except Exception as exc:             # noqa: BLE001
        return (f"browser_click failed: {exc}\n"
                "The ref may be stale — call browser_snapshot again for fresh refs.")


_SEL_CLICK_MS = 4_000       # first (normal) click attempt before falling back


async def browser_click_selector(selector: str = "") -> str:
    """Click by CSS/XPath — for elements the snapshot can't see (no native
    input/role/name, e.g. a vxe-table checkbox rendered as a styled <span>),
    which therefore never get a ref for browser_click.

    Robust against the two ways real grids defeat a plain click:
      * the grid lives inside an iframe → all frames are searched;
      * the matched cell is covered by a clone (vxe-table fixed columns render
        a copy of the column ABOVE the body table) → a normal click fails the
        hit-target check, so fall back to force (coordinate) click, then to a
        dispatched JS click event.
    """
    selector = (selector or "").strip()
    if not selector:
        return ("browser_click_selector needs a 'selector' — a CSS selector, or an "
                "XPath starting with // .")
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        if not page.url or page.url == "about:blank":
            return "No page open yet — call browser_open(url) first."
        hits = []                        # (frame, locator, count) per frame with matches
        for frame in page.frames:
            try:
                loc = frame.locator(selector)
                n = await loc.count()
            except Exception as exc:     # noqa: BLE001 — invalid selector syntax
                return f"browser_click_selector: bad selector {selector!r}: {exc}"
            if n:
                hits.append((frame, loc, n))
        if not hits:
            return (f"browser_click_selector: no element matches {selector!r} "
                    f"(searched the page and {len(page.frames) - 1} iframe(s)). "
                    "Check the selector against browser_read / a screenshot.")
        frame, loc, _ = hits[0]
        target = loc.first
        how = ""
        try:
            await target.click(timeout=_SEL_CLICK_MS)
        except Exception:                # noqa: BLE001 — covered/unstable target
            try:
                await target.click(timeout=_SEL_CLICK_MS, force=True)
                how = (" using a forced click (the element was covered — the click "
                       "landed on whatever is rendered at its position)")
            except Exception:            # noqa: BLE001 — not even coordinates work
                await target.dispatch_event("click")
                how = (" by dispatching a JS click event (a real click was blocked; "
                       "the event fired on the matched element itself)")
        await _settle(page)
        total = sum(h[2] for h in hits)
        note = f" (matched {total} elements; clicked the first)" if total > 1 else ""
        infr = "" if frame is page.main_frame else " inside an iframe"
        return f"Clicked {selector!r}{note}{infr}{how}.\n\n" + await _snapshot_block(page)
    except Exception as exc:             # noqa: BLE001
        return (f"browser_click_selector failed: {exc}\n"
                "Try a more specific selector for the innermost clickable element "
                "(e.g. the checkbox span itself), or browser_evaluate with .click() "
                "as a last resort.")


async def browser_type(ref: int = 0, text: str = "", submit: bool = False) -> str:
    if not _available():
        return _needs_browser()
    sel = _ref_selector(ref)
    if sel is None:
        return "browser_type needs a numeric 'ref' from the latest browser_snapshot."
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        await page.fill(sel, text or "", timeout=_ACT_MS)
        if submit:
            await page.press(sel, "Enter", timeout=_ACT_MS)
            await _settle(page)
        return (f"Typed into [{int(ref)}]"
                + (" and pressed Enter" if submit else "") + ".\n\n"
                + await _snapshot_block(page))
    except Exception as exc:             # noqa: BLE001
        return (f"browser_type failed: {exc}\n"
                "The ref may be stale — call browser_snapshot again for fresh refs.")


async def browser_read(max_chars: int = _MAX_READ) -> str:
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        if not page.url or page.url == "about:blank":
            return "No page open yet — call browser_open(url) first."
        text = await page.inner_text("body")
        if not (text or "").strip():
            text = await page.text_content("body")
        cleaned = _clean(text or "", int(max_chars) if max_chars else _MAX_READ)
        return f"Content of {page.url}:\n\n{cleaned or '(no readable text)'}"
    except Exception as exc:             # noqa: BLE001
        return f"browser_read failed: {exc}"


async def browser_press(key: str = "") -> str:
    key = (key or "").strip()
    if not key:
        return "browser_press needs a 'key' (e.g. Enter, Escape, Tab, ArrowDown)."
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        await page.keyboard.press(key)
        await _settle(page)
        return f"Pressed {key}.\n\n" + await _snapshot_block(page)
    except Exception as exc:             # noqa: BLE001
        return f"browser_press failed: {exc}"


async def browser_back() -> str:
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        await page.go_back(wait_until="domcontentloaded", timeout=_NAV_MS)
        await _settle(page)
        return "Went back.\n\n" + await _snapshot_block(page)
    except Exception as exc:             # noqa: BLE001
        return f"browser_back failed: {exc}"


async def browser_tabs() -> str:
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        pages = [p for p in ctx.pages if not p.is_closed()]
        if not pages:
            return "No tabs open — call browser_open(url) first."
        cur = _current_page
        lines = []
        for i, p in enumerate(pages, 1):
            try:
                title = (await p.title() or "").strip()
            except Exception:            # noqa: BLE001
                title = ""
            head = f"{title} — " if title else ""
            mark = "   ← current" if p is cur else ""
            lines.append(f"[{i}] {head}{p.url}{mark}")
        return (f"Open tabs ({len(pages)}):\n" + "\n".join(lines)
                + "\n\nUse browser_tab_select(index) to switch tabs; "
                  "browser_open(url, new_tab=true) opens another tab.")
    except Exception as exc:             # noqa: BLE001
        return "browser_tabs failed: " + _launch_hint(exc)


async def browser_tab_select(index: int = 0) -> str:
    global _current_page
    if not _available():
        return _needs_browser()
    try:
        i = int(index)
    except (TypeError, ValueError):
        return "browser_tab_select needs a numeric 'index' from browser_tabs."
    try:
        ctx = await _get_context()
        pages = [p for p in ctx.pages if not p.is_closed()]
        if not 1 <= i <= len(pages):
            return (f"browser_tab_select: index {i} is out of range — "
                    f"{len(pages)} tab(s) open; call browser_tabs for the list.")
        _current_page = pages[i - 1]
        if config.browser_focus_policy() != "never":
            try:
                await _current_page.bring_to_front()
            except Exception:            # noqa: BLE001 — headless/attach quirks
                pass
        return f"Switched to tab [{i}].\n\n" + await _snapshot_block(_current_page)
    except Exception as exc:             # noqa: BLE001
        return "browser_tab_select failed: " + _launch_hint(exc)


# In-page wrapper for browser_evaluate. Indirect eval covers expressions AND
# statement lists ("let el = …; el.click()"); scripts that use `return` (or
# top-level `await`, illegal in eval) fall back to an async function body.
# The result is stringified in-page so DOM nodes / circular objects can't
# break Playwright's structured-clone serialization on the way out.
_EVAL_JS = """async (script) => {
  let value;
  try {
    value = await (0, eval)(script);
  } catch (err) {
    if (!(err instanceof SyntaxError)) throw err;
    const AsyncFn = Object.getPrototypeOf(async function () {}).constructor;
    value = await new AsyncFn(script)();
  }
  if (value === undefined) return 'undefined';
  try { return JSON.stringify(value) ?? String(value); }
  catch { return String(value); }
}"""

_MAX_EVAL = 4000            # cap on the stringified result


async def browser_evaluate(script: str = "") -> str:
    script = (script or "").strip()
    if not script:
        return "browser_evaluate needs a 'script' (JavaScript to run on the page)."
    if not _available():
        return _needs_browser()
    try:
        ctx = await _get_context()
        page = await _active_page(ctx)
        if not page.url or page.url == "about:blank":
            return "No page open yet — call browser_open(url) first."
        result = str(await page.evaluate(_EVAL_JS, script))
        await _settle(page)
        if len(result) > _MAX_EVAL:
            result = result[:_MAX_EVAL] + f"\n…(+{len(result) - _MAX_EVAL} chars truncated)"
        return f"Result: {result}\n\n" + await _snapshot_block(page)
    except Exception as exc:             # noqa: BLE001
        return f"browser_evaluate failed: {exc}"


# --------------------------------------------------------------------------- #
# DevTools read tools — report from the passive capture buffers. These don't
# launch the browser: with nothing captured yet they just say so.
# --------------------------------------------------------------------------- #

def _clamp_entries(max_entries) -> int:
    try:
        return max(1, min(int(max_entries), 200))
    except (TypeError, ValueError):
        return _MAX_LIST


async def browser_console_messages(level: str = "", max_entries: int = _MAX_LIST) -> str:
    level = (level or "").strip().lower()
    if level == "warn":                  # accept the common alias
        level = "warning"
    entries = list(_console_log)
    if not entries:
        return ("No console messages captured yet — capture runs while the control "
                "browser is open (start with browser_open).")
    if level:
        entries = [e for e in entries if e["level"] == level]
        if not entries:
            return (f"No console messages at level '{level}' "
                    f"({len(_console_log)} message(s) captured at other levels).")
    entries = entries[-_clamp_entries(max_entries):]
    lines = [f"{e['ts']} [{e['level']}] {e['text']}"
             + (f"  ({e['location']})" if e["location"] else "")
             for e in entries]
    scope = f" at level '{level}'" if level else ""
    return (f"Last {len(lines)} console message(s){scope} "
            f"(of {len(_console_log)} captured, oldest first):\n\n" + "\n".join(lines))


async def browser_network_requests(match: str = "", max_entries: int = _MAX_LIST) -> str:
    match = (match or "").strip().lower()
    entries = list(_net_log)
    if not entries:
        return ("No network requests captured yet — capture runs while the control "
                "browser is open (start with browser_open).")
    if match:
        entries = [e for e in entries
                   if match in e["url"].lower() or match == e["type"].lower()]
        if not entries:
            return (f"No captured requests match '{match}' "
                    f"({len(_net_log)} request(s) captured).")
    entries = entries[-_clamp_entries(max_entries):]
    lines = []
    for e in entries:
        status = str(e["status"]) if e["status"] is not None else f"FAILED({e['failure']})"
        lines.append(f"{e['ts']} {e['method']} {status} {e['type']} {e['url']}")
    scope = f" matching '{match}'" if match else ""
    return (f"Last {len(lines)} request(s){scope} "
            f"(of {len(_net_log)} captured, oldest first; no headers/bodies):\n\n"
            + "\n".join(lines))


# --------------------------------------------------------------------------- #
# Screenshot — a per-request tool scoped to an agent's working folder (that's
# where the PNG has to land for a note to later embed it). Unlike the tools
# above, this can't live in the static ``make_browser_tools()`` registry
# because it needs ``working_dir`` at call time — mirrors ``make_shell_tool``.
# --------------------------------------------------------------------------- #

_SHOT_DIR = "screenshots"


def make_screenshot_tool(working_dir: str | Path, agent_id: str = "") -> dict[str, dict]:
    """A ``browser_screenshot`` tool that saves PNGs under ``working_dir``.

    Returns ``{}`` (no tool) when there's no working folder — there'd be
    nowhere safe to write the file. When ``agent_id`` is known, the saved PNG
    is also served back as a ``![screenshot](...)`` markdown image (via
    ``routes.agent_files``) so it renders inline in chat, not just as a path."""
    wd = (working_dir or "").strip()
    if not wd:
        return {}
    root = _resolve_root(wd)
    root.mkdir(parents=True, exist_ok=True)

    async def browser_screenshot(path: str = "") -> str:
        if not _available():
            return _needs_browser()
        rel = (path or "").strip()
        if not rel:
            rel = f"{_SHOT_DIR}/{datetime.now():%Y%m%d-%H%M%S-%f}.png"
        if not rel.lower().endswith(".png"):
            rel += ".png"
        try:
            target = _safe(root, rel)
        except FsError as exc:
            return str(exc)
        try:
            ctx = await _get_context()
            page = await _active_page(ctx)
            if not page.url or page.url == "about:blank":
                return "No page open yet — call browser_open(url) first."
            target.parent.mkdir(parents=True, exist_ok=True)
            # full_page: capture the whole scrollable page, not just what fits in
            # the window right now — a dashboard/table taller than the window would
            # otherwise come out cropped.
            await page.screenshot(path=str(target), full_page=True)
            vp = page.viewport_size or {}
            dims = f" ({vp['width']}x{vp['height']})" if vp else ""
            rel_saved = target.relative_to(root)
            note = f"Saved screenshot to {rel_saved}{dims}"
            if agent_id:
                from urllib.parse import quote
                url = f"/api/agents/{agent_id}/files?path={quote(str(rel_saved))}"
                note += f"\n\n![screenshot]({url})"
            return note
        except Exception as exc:         # noqa: BLE001
            return f"browser_screenshot failed: {exc}"

    return {
        "browser_screenshot": {
            "fn": browser_screenshot,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_screenshot",
                "Take a screenshot of the current page and save it as a PNG file in the "
                "working folder. Returns the saved file's relative path (and viewport size) "
                "plus a `![screenshot](...)` markdown image that renders inline in chat — "
                "leave it in your reply as-is. Also usable in an Obsidian-style "
                "`![[relative/path.png]]` embed when writing notes with write_file. If "
                "'path' is omitted, a timestamped filename is generated under a "
                "`screenshots/` subfolder.",
                {"path": {"type": "string",
                           "description": "optional relative path to save to, e.g. "
                                           "'screenshots/step1.png' (default: auto-generated "
                                           "under screenshots/)"}},
                []),
        },
    }


# --------------------------------------------------------------------------- #
# Screen recording — video is a *launch-time* Playwright option, so start/stop
# bounce the persistent context (logins survive on disk; the current page is
# restored on start). Scoped to the working folder like make_screenshot_tool:
# that's where the .webm has to land.
# --------------------------------------------------------------------------- #

_REC_DIR = "recordings"


def make_recording_tools(working_dir: str | Path) -> dict[str, dict]:
    """``browser_record_start`` / ``browser_record_stop`` saving under ``working_dir``.

    Returns ``{}`` (no tools) when there's no working folder — there'd be
    nowhere safe to write the video."""
    wd = (working_dir or "").strip()
    if not wd:
        return {}
    root = _resolve_root(wd)
    root.mkdir(parents=True, exist_ok=True)

    async def browser_record_start() -> str:
        global _record_dir
        if not _available():
            return _needs_browser()
        if config.browser_cdp_url():
            return ("Screen recording isn't available while attached to your own "
                    "Chrome (VW_BROWSER_CDP_URL is set) — video capture is a "
                    "launch-time option, so it needs a Virtual Worker-launched browser. Use "
                    "browser_screenshot instead.")
        if _record_dir is not None:
            return ("Already recording — call browser_record_stop to save the current "
                    "video before starting a new one.")
        tmp = root / _REC_DIR / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        url = await _teardown_context()      # recording needs a fresh launch
        _record_dir = tmp
        try:
            ctx = await _get_context()
            if url:                          # put the agent back where it was
                page = await _active_page(ctx)
                await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_MS)
                await _settle(page)
                return (f"Recording started — the browser relaunched and restored {url}. "
                        "Everything from now until browser_record_stop is captured.\n\n"
                        + await _snapshot_block(page))
            return ("Recording started — everything from the next browser_open until "
                    "browser_record_stop is captured as video.")
        except Exception as exc:             # noqa: BLE001
            _record_dir = None
            shutil.rmtree(tmp, ignore_errors=True)
            return "browser_record_start failed: " + _launch_hint(exc)

    async def browser_record_stop(path: str = "") -> str:
        global _record_dir
        if not _available():
            return _needs_browser()
        if config.browser_cdp_url():
            return ("Screen recording isn't available while attached to your own "
                    "Chrome (VW_BROWSER_CDP_URL is set).")
        if _record_dir is None:
            return "Not recording — call browser_record_start first."
        tmp = _record_dir
        rel = (path or "").strip()
        if not rel:
            rel = f"{_REC_DIR}/{datetime.now():%Y%m%d-%H%M%S}.webm"
        if not rel.lower().endswith(".webm"):
            rel += ".webm"
        try:
            target = _safe(root, rel)
        except FsError as exc:
            return str(exc)
        video = None
        async with _lock:
            ctx = _ctx
        if ctx is not None:
            try:
                pages = [p for p in ctx.pages if not p.is_closed()]
                video = pages[-1].video if pages else None
            except Exception:                # noqa: BLE001
                pass
        await _teardown_context()            # closing the context finalizes the file
        _record_dir = None
        src = None
        try:
            if video is not None:
                src = Path(await video.path())
        except Exception:                    # noqa: BLE001
            pass
        if src is None or not src.exists():  # e.g. the user closed the window mid-run
            webms = sorted(tmp.glob("*.webm"), key=lambda p: p.stat().st_mtime)
            src = webms[-1] if webms else None
        if src is None:
            shutil.rmtree(tmp, ignore_errors=True)
            return ("Recording stopped, but no video was captured — no page was "
                    "opened while recording.")
        extra = len(list(tmp.glob("*.webm"))) - 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        shutil.rmtree(tmp, ignore_errors=True)
        size_kb = max(1, target.stat().st_size // 1024)
        note = f" ({extra} earlier page video(s) discarded)" if extra > 0 else ""
        return (f"Recording saved to {target.relative_to(root)} ({size_kb} KB){note}. "
                "The browser was closed to finalize the video — use browser_open to "
                "continue browsing.")

    return {
        "browser_record_start": {
            "fn": browser_record_start,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_record_start",
                "Start screen-recording the browser as video. The browser relaunches "
                "(logins survive; the current page is restored) and every page interaction "
                "from here on is captured until browser_record_stop saves the .webm. "
                "Call this before walking through a UI flow you want a recording of "
                "(e.g. a test case), then run the flow with browser_open / browser_click / "
                "browser_type as usual.",
                {}, []),
        },
        "browser_record_stop": {
            "fn": browser_record_stop,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_record_stop",
                "Stop the screen recording and save it as a .webm video file in the working "
                "folder. Returns the saved file's relative path — use it in an Obsidian-style "
                "`![[recordings/name.webm]]` embed when writing notes with write_file. The "
                "browser closes to finalize the video; browser_open resumes browsing. If "
                "'path' is omitted, a timestamped filename is generated under `recordings/`.",
                {"path": {"type": "string",
                           "description": "optional relative path to save to, e.g. "
                                           "'recordings/create-conveyor.webm' (default: "
                                           "auto-generated under recordings/)"}},
                []),
        },
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def _fn_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


# The names of the interactive browser-control tools an agent can be granted.
# Used to decide whether an agent "has a browser" — e.g. the shell tool exports
# VW_AGENT_BROWSER=1 so a subprocess CLI can drive the agent's browser instead
# of the human's. Covers the eleven core control tools plus the passive DevTools
# readers and the screenshot/recording tools (all rendered by the make_* factories
# below and make_screenshot_tool / make_recording_tools).
CONTROL_TOOL_NAMES: frozenset[str] = frozenset({
    "browser_open", "browser_snapshot", "browser_click", "browser_click_selector",
    "browser_type", "browser_read", "browser_press", "browser_back",
    "browser_tabs", "browser_tab_select", "browser_evaluate",
    "browser_console_messages", "browser_network_requests",
    "browser_screenshot", "browser_record_start", "browser_record_stop",
})


def make_browser_tools() -> dict[str, dict]:
    """The interactive browser-control registry slice ({name: {schema, fn}}).

    Merged into ``tools.TOOLS`` so each tool is selectable per-agent by name."""
    return {
        "browser_open": {
            "fn": browser_open,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_open",
                "Open a URL in a real, VISIBLE browser and return the page plus a numbered "
                "list of clickable elements. The browser keeps its login/session across calls "
                "and restarts (the user can log in manually in the window). Use this to start a "
                "browser task, then browser_click / browser_type by ref number. Tab handling is "
                "automatic and safe: if a tab is already open on this address it is REUSED (no "
                "reload); otherwise the page opens in a NEW tab — an existing tab showing a "
                "different page is never navigated/refreshed, so the user's own tabs are left "
                "alone (browser_tabs / browser_tab_select switch between tabs). Set new_tab=true "
                "to force a brand-new tab even when a matching one exists.",
                {"url": {"type": "string", "description": "the URL to open"},
                 "new_tab": {"type": "boolean",
                             "description": "force a brand-new tab even if a tab on this "
                                            "address already exists (default false)"}},
                ["url"]),
        },
        "browser_tabs": {
            "fn": browser_tabs,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_tabs",
                "List the open tabs in the control browser — [index] title — URL, with the "
                "agent's current tab marked. When Virtual Worker is attached to the user's own Chrome "
                "this includes their tabs too; only switch into one of those when the user "
                "asked you to work in it.",
                {}, []),
        },
        "browser_tab_select": {
            "fn": browser_tab_select,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_tab_select",
                "Switch the agent's working tab to the [index] from the latest browser_tabs "
                "list and bring it to the front. All other browser tools then act on that "
                "tab. Returns a fresh snapshot of it.",
                {"index": {"type": "integer",
                           "description": "the [n] tab index from browser_tabs"}},
                ["index"]),
        },
        "browser_snapshot": {
            "fn": browser_snapshot,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_snapshot",
                "Re-scan the current page and return a fresh numbered list of visible "
                "interactive elements (e.g. `[1] button \"Sign in\"`). Call this before "
                "clicking/typing — refs are re-assigned every snapshot.",
                {}, []),
        },
        "browser_click": {
            "fn": browser_click,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_click",
                "Click the element with the given ref number from the latest browser_snapshot. "
                "Returns a fresh snapshot of the resulting page.",
                {"ref": {"type": "integer", "description": "the [n] ref of the element to click"}},
                ["ref"]),
        },
        "browser_click_selector": {
            "fn": browser_click_selector,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_click_selector",
                "Click an element by CSS selector or XPath (starting with //). Use when the "
                "target has NO ref in the snapshot because it's a custom-rendered control "
                "with no native input/role/name — e.g. a vxe-table row checkbox cell: "
                "tr[rowid=\"row_126\"] td.col--checkbox, or the first row's "
                ".vxe-body--row:first-child td.col--checkbox. Searches iframes too, and if "
                "the element is covered (vxe fixed-column clones, overlays) it automatically "
                "falls back to a forced coordinate click, then a dispatched JS click. If "
                "several elements match, the first is clicked — make the selector specific "
                "([rowid=…], nth-child). Prefer browser_click(ref) whenever the element has "
                "a ref; returns a fresh snapshot like the other actions.",
                {"selector": {"type": "string",
                              "description": "CSS selector, or XPath if it starts with //"}},
                ["selector"]),
        },
        "browser_type": {
            "fn": browser_type,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_type",
                "Type text into the input/textarea with the given ref number. Set submit=true to "
                "press Enter after (e.g. to submit a search or login form).",
                {"ref": {"type": "integer", "description": "the [n] ref of the field"},
                 "text": {"type": "string", "description": "text to enter"},
                 "submit": {"type": "boolean", "description": "press Enter after typing (default false)"}},
                ["ref", "text"]),
        },
        "browser_read": {
            "fn": browser_read,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_read",
                "Return the rendered, readable text of the current page (what browser_fetch does, "
                "but on the live logged-in page).",
                {"max_chars": {"type": "integer", "description": "max characters to return (default 6000)"}},
                []),
        },
        "browser_press": {
            "fn": browser_press,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_press",
                "Press a keyboard key on the current page (e.g. Enter, Escape, Tab, ArrowDown, "
                "PageDown). Useful for closing dialogs or scrolling.",
                {"key": {"type": "string", "description": "key name, e.g. Enter / Escape / PageDown"}},
                ["key"]),
        },
        "browser_back": {
            "fn": browser_back,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_back", "Navigate the current tab back to the previous page.", {}, []),
        },
        "browser_evaluate": {
            "fn": browser_evaluate,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_evaluate",
                "Run JavaScript on the current page and return its result (JSON-stringified) "
                "plus a fresh snapshot. The escape hatch when browser_click can't reach a "
                "custom-rendered control — e.g. an Ant Design checkbox whose real <input> is "
                "covered by a styled wrapper: document.querySelector('.ant-checkbox-input')"
                ".click(). Target elements with any CSS selector or document.evaluate (XPath); "
                "elements from the latest snapshot keep their ref as a data-vw-ref attribute, "
                "so [data-vw-ref=\"7\"] selects element [7]. Statements are allowed "
                "('let el = …; el.click()' returns the last statement's value); use 'return' "
                "and 'await' for async flows.",
                {"script": {"type": "string",
                            "description": "JavaScript to execute in the page context"}},
                ["script"]),
        },
        "browser_console_messages": {
            "fn": browser_console_messages,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_console_messages",
                "Read recent console output from the control browser (console.log/info/"
                "warning/error plus uncaught page errors), oldest first. Capture runs "
                "passively while the browser is open — use this to debug a page that "
                "misbehaves after a click (JS errors, failed validations). Optional "
                "'level' filters by type.",
                {"level": {"type": "string",
                           "description": "only this level: log, info, warning, error "
                                          "(default: all)"},
                 "max_entries": {"type": "integer",
                                 "description": "max messages to return (default 50)"}},
                []),
        },
        "browser_network_requests": {
            "fn": browser_network_requests,
            "kind": "browser",
            "schema": _fn_schema(
                "browser_network_requests",
                "List recent HTTP requests the control browser made — method, status, "
                "resource type and URL only (headers and bodies are never captured). "
                "Use it to see the XHR/fetch calls behind a UI action, spot failed or "
                "4xx/5xx calls, or find the API endpoint a button hits. Optional "
                "'match' filters by URL substring or exact resource type "
                "(e.g. 'xhr', 'fetch', 'document', 'script').",
                {"match": {"type": "string",
                           "description": "URL substring or resource type to filter by"},
                 "max_entries": {"type": "integer",
                                 "description": "max requests to return (default 50)"}},
                []),
        },
    }
