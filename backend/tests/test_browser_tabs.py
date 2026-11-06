"""Multi-tab control + CDP attach mode (VW_BROWSER_CDP_URL).

Everything is tested against fake page/context stubs — no Playwright launch.
The tab tools only need .pages / .new_page() / page.title()/url/is_closed(),
and the snapshot block only needs page.evaluate()/title()/url.
"""

import asyncio

import pytest

import config
from agent import browser_control as bc


class FakePage:
    def __init__(self, url="https://example.test/", title="Example"):
        self.url = url
        self._title = title
        self._closed = False
        self.fronted = False
        self.evals = []                    # scripts run via evaluate()

    def is_closed(self):
        return self._closed

    async def title(self):
        return self._title

    async def bring_to_front(self):
        self.fronted = True

    async def evaluate(self, script, *_a):
        self.evals.append(script)
        return ['[1] button "ok"']

    async def goto(self, url, **_kw):
        self.url = url

    async def wait_for_load_state(self, *_a, **_kw):
        pass


class FakeCtx:
    def __init__(self, pages=None):
        self.pages = list(pages or [])

    async def new_page(self):
        p = FakePage(url="about:blank", title="")
        self.pages.append(p)
        return p


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(bc, "_current_page", None)
    monkeypatch.setattr(bc, "_cdp", None)
    monkeypatch.setattr(bc, "_attach_note", "")
    monkeypatch.setattr(bc, "_available", lambda: True)
    monkeypatch.setattr(config, "BROWSER_CDP_URL", "", raising=False)
    yield


def use_ctx(monkeypatch, ctx):
    async def fake_get_context():
        return ctx
    monkeypatch.setattr(bc, "_get_context", fake_get_context)


# ------------------------------------------------------------ active page ----

def test_active_page_launched_reuses_last_tab(monkeypatch):
    a, b = FakePage(title="A"), FakePage(title="B")
    ctx = FakeCtx([a, b])
    page = run(bc._active_page(ctx))
    assert page is b
    assert bc._current_page is b


def test_active_page_attach_never_steals_user_tab(monkeypatch):
    monkeypatch.setattr(bc, "_cdp", object())   # actually attached to user's Chrome
    user_tab = FakePage(title="User's tab")
    ctx = FakeCtx([user_tab])
    page = run(bc._active_page(ctx))
    assert page is not user_tab            # a NEW tab was opened instead
    assert page in ctx.pages and len(ctx.pages) == 2


def test_active_page_tracks_across_calls_and_survives_close(monkeypatch):
    a, b = FakePage(title="A"), FakePage(title="B")
    ctx = FakeCtx([a, b])
    first = run(bc._active_page(ctx))
    assert first is b
    b._closed = True                       # tracked tab dies → falls back
    second = run(bc._active_page(ctx))
    assert second is a


# ------------------------------------------------------------------ tools ----

def test_browser_open_new_tab(monkeypatch):
    a = FakePage(title="A")
    ctx = FakeCtx([a])
    use_ctx(monkeypatch, ctx)
    bc._current_page = a
    out = run(bc.browser_open("https://new.test/", new_tab=True))
    assert "Opened the page in a new tab." in out
    assert len(ctx.pages) == 2
    assert bc._current_page is ctx.pages[-1]
    assert bc._current_page.url == "https://new.test/"
    assert a.url == "https://example.test/"    # untouched


def test_agent_created_tabs_carry_group_marker(monkeypatch):
    """Tabs Virtual Worker opens set window.name so the Recorder extension can group them."""
    ctx = FakeCtx([FakePage()])
    use_ctx(monkeypatch, ctx)
    run(bc.browser_open("https://new.test/", new_tab=True))
    new_tab = ctx.pages[-1]
    assert any(bc._AGENT_TAB_MARK in s for s in new_tab.evals)

    monkeypatch.setattr(bc, "_cdp", object())  # attach mode: fresh tab, marked too
    bc._current_page = None
    attach_ctx = FakeCtx([FakePage()])
    page = run(bc._active_page(attach_ctx))
    assert any(bc._AGENT_TAB_MARK in s for s in page.evals)


def test_browser_tabs_lists_and_marks_current(monkeypatch):
    a = FakePage(url="https://a.test/", title="Site A")
    b = FakePage(url="https://b.test/", title="Site B")
    ctx = FakeCtx([a, b])
    use_ctx(monkeypatch, ctx)
    bc._current_page = b
    out = run(bc.browser_tabs())
    assert "Open tabs (2):" in out
    assert "[1] Site A — https://a.test/" in out
    assert "[2] Site B — https://b.test/   ← current" in out


def test_browser_tab_select_switches_and_fronts(monkeypatch):
    a = FakePage(url="https://a.test/", title="Site A")
    b = FakePage(url="https://b.test/", title="Site B")
    ctx = FakeCtx([a, b])
    use_ctx(monkeypatch, ctx)
    bc._current_page = b
    out = run(bc.browser_tab_select(1))
    assert "Switched to tab [1]." in out
    assert bc._current_page is a
    assert a.fronted


def test_browser_tab_select_out_of_range(monkeypatch):
    ctx = FakeCtx([FakePage()])
    use_ctx(monkeypatch, ctx)
    out = run(bc.browser_tab_select(7))
    assert "out of range" in out and "1 tab(s) open" in out


# ----------------------------------------------------------- attach mode -----

def test_recording_unavailable_in_attach_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BROWSER_CDP_URL", "http://127.0.0.1:9222", raising=False)
    tools = bc.make_recording_tools(str(tmp_path))
    start = run(tools["browser_record_start"]["fn"]())
    assert "attached to your own Chrome" in start
    stop = run(tools["browser_record_stop"]["fn"]())
    assert "attached to your own Chrome" in stop


def test_close_disconnects_but_keeps_attached_chrome(monkeypatch):
    class FakeBrowser:
        closed = False

        async def close(self):
            self.closed = True

    class FakeClosableCtx(FakeCtx):
        closed = False

        async def close(self):
            self.closed = True

    browser, ctx = FakeBrowser(), FakeClosableCtx()
    monkeypatch.setattr(bc, "_cdp", browser)
    monkeypatch.setattr(bc, "_ctx", ctx)
    monkeypatch.setattr(bc, "_pw", None)
    run(bc.close_control_browser())
    assert browser.closed                  # CDP handle disconnected …
    assert not ctx.closed                  # … the user's context left alone
    assert bc._cdp is None and bc._ctx is None


class LaunchableCtx(FakeCtx):
    """FakeCtx that also survives the launch path in _get_context."""

    async def add_init_script(self, *_a, **_kw):
        pass

    def on(self, *_a, **_kw):
        pass


def _fake_pw(monkeypatch, tmp_path, *, chrome_listening, attached_ctx=None,
             launched_ctx=None):
    """Install a fake Playwright driver: connect_over_cdp succeeds or refuses,
    launch_persistent_context returns launched_ctx."""
    from types import SimpleNamespace

    async def connect_over_cdp(url, timeout=None):
        assert timeout, "attach must pass an explicit timeout (a frozen tab hangs the 180s default)"
        if not chrome_listening:
            raise Exception("connect ECONNREFUSED 127.0.0.1:9222")
        return SimpleNamespace(contexts=[attached_ctx], on=lambda *_a: None)

    async def launch_persistent_context(**_kw):
        return launched_ctx

    monkeypatch.setattr(bc, "_pw", SimpleNamespace(chromium=SimpleNamespace(
        connect_over_cdp=connect_over_cdp,
        launch_persistent_context=launch_persistent_context)))
    monkeypatch.setattr(bc, "_ctx", None)
    monkeypatch.setattr(config, "BROWSER_CDP_URL", "http://127.0.0.1:9222", raising=False)
    monkeypatch.setattr(config, "BROWSER_PROFILE", tmp_path / "profile", raising=False)


def test_get_context_attaches_when_chrome_listening(monkeypatch, tmp_path):
    attached = LaunchableCtx()
    _fake_pw(monkeypatch, tmp_path, chrome_listening=True, attached_ctx=attached)
    ctx = run(bc._get_context())
    assert ctx is attached
    assert bc._attached()
    assert bc._attach_note == ""


def test_get_context_falls_back_to_launch_when_attach_fails(monkeypatch, tmp_path):
    launched = LaunchableCtx()
    _fake_pw(monkeypatch, tmp_path, chrome_listening=False, launched_ctx=launched)
    ctx = run(bc._get_context())
    assert ctx is launched                 # previous logic: Virtual Worker launches its own
    assert not bc._attached()
    assert "couldn't attach" in bc._attach_note


def test_attach_fallback_note_surfaced_once_by_browser_open(monkeypatch):
    ctx = FakeCtx([FakePage()])
    use_ctx(monkeypatch, ctx)
    monkeypatch.setattr(bc, "_attach_note", "couldn't attach to your Chrome at x — …")
    first = run(bc.browser_open("https://a.test/"))
    assert "⚠️ couldn't attach" in first
    second = run(bc.browser_open("https://b.test/"))
    assert "⚠️" not in second              # shown once, then cleared


# --------------------------------------------------------------- registry ----

def test_tools_registered():
    reg = bc.make_browser_tools()
    for name in ("browser_tabs", "browser_tab_select"):
        assert name in reg
        assert reg[name]["schema"]["function"]["name"] == name
    open_props = reg["browser_open"]["schema"]["function"]["parameters"]["properties"]
    assert "new_tab" in open_props
    from agent import tools
    assert "browser_tabs" in tools.TOOLS
    assert "browser_tab_select" in tools.TOOLS
