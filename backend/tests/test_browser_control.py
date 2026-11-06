"""Interactive browser-control tools — hermetic (no real browser launched).

We mock the Playwright page/context so click/type/read dispatch is exercised
without a display or a Chromium binary. Follows the repo convention of driving
async tool functions with ``asyncio.run`` (see test_agent.py / test_fs_tools.py).
"""

import asyncio
from pathlib import Path

import pytest

from agent import browser_control as bc
from agent import tools as toolkit


# --------------------------------------------------------------------------- #
# A fake Playwright page/context that records what it was asked to do.
# --------------------------------------------------------------------------- #

class FakeVideo:
    def __init__(self, file: Path):
        self.file = file

    async def path(self):
        return str(self.file)


class FakeLocator:
    def __init__(self, page, selector, n):
        self.page, self.selector, self.n = page, selector, n

    async def count(self):
        return self.n

    @property
    def first(self):
        return self

    async def click(self, timeout=None, force=False):
        if not force and self.page.click_blocked:
            raise TimeoutError("element intercepts pointer events")
        if force and self.page.force_blocked:
            raise TimeoutError("element is not stable")
        self.page.calls.append(("locator_click", self.selector, force))

    async def dispatch_event(self, event):
        self.page.calls.append(("dispatch_event", self.selector, event))


class FakePage:
    def __init__(self, url="https://example.com", video=None):
        self.url = url
        self.calls = []
        self.screenshot_kwargs = {}
        self.video = video
        self.match_count = 1          # what locator(...).count() reports
        self.click_blocked = False    # normal locator click raises (covered element)
        self.force_blocked = False    # force click raises too

    def is_closed(self):
        return False

    async def bring_to_front(self):
        self.calls.append(("bring_to_front", self.url))

    @property
    def frames(self):
        return [self]                 # main frame only

    @property
    def main_frame(self):
        return self

    def locator(self, selector):
        return FakeLocator(self, selector, self.match_count)

    async def click(self, selector, timeout=None):
        self.calls.append(("click", selector))

    async def fill(self, selector, text, timeout=None):
        self.calls.append(("fill", selector, text))

    async def press(self, selector, key, timeout=None):
        self.calls.append(("press", selector, key))

    async def goto(self, url, **kw):
        self.url = url
        self.calls.append(("goto", url))

    async def go_back(self, **kw):
        self.calls.append(("back",))

    async def inner_text(self, sel):
        return "hello world"

    async def text_content(self, sel):
        return "hello world"

    async def title(self):
        return "Example"

    async def evaluate(self, js, arg=None):
        return ['[1] button "Sign in"', '[2] input:text "Email"']

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def screenshot(self, path=None, **kw):
        self.calls.append(("screenshot", path))
        self.screenshot_kwargs = kw
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    @property
    def viewport_size(self):
        return {"width": 1280, "height": 800}

    @property
    def keyboard(self):
        page = self

        class _KB:
            async def press(self, key):
                page.calls.append(("kbpress", key))
        return _KB()


class FakeCtx:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False
        self.cookie_urls = None

    async def new_page(self):
        p = FakePage()
        self.pages.append(p)
        return p

    async def cookies(self, urls=None):
        self.cookie_urls = urls
        return [{"name": "_token", "value": "abc", "domain": "x", "httpOnly": True},
                {"name": "_user", "value": "Example_test", "domain": "x", "httpOnly": False}]

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_ctx(monkeypatch):
    page = FakePage()
    ctx = FakeCtx(page)

    async def _get_context():
        return ctx

    monkeypatch.setattr(bc, "_available", lambda: True)
    monkeypatch.setattr(bc, "_get_context", _get_context)
    return ctx, page


# --------------------------------------------------------------------------- #
# Registry & schema shape
# --------------------------------------------------------------------------- #

def test_all_tools_registered_with_valid_schema():
    reg = bc.make_browser_tools()
    expected = {"browser_open", "browser_snapshot", "browser_click",
                "browser_type", "browser_read", "browser_press", "browser_back",
                "browser_evaluate", "browser_click_selector"}
    assert expected <= set(reg)
    for name, entry in reg.items():
        assert callable(entry["fn"])
        fn = entry["schema"]["function"]
        assert fn["name"] == name                       # schema name matches key
        assert fn["parameters"]["type"] == "object"
        props = fn["parameters"]["properties"]
        assert set(fn["parameters"].get("required", [])) <= set(props)


def test_get_cookies_reads_live_context_jar(monkeypatch):
    # Reads bc._ctx directly (httpOnly cookies live only in the real jar). With a
    # url it scopes the query; with no context open it returns [] and never opens one.
    page = FakePage()
    ctx = FakeCtx(page)
    monkeypatch.setattr(bc, "_ctx", ctx)
    got = asyncio.run(bc.get_cookies("http://portal.test:31641"))
    assert ctx.cookie_urls == ["http://portal.test:31641"]
    assert {c["name"]: c["value"] for c in got} == {"_token": "abc", "_user": "Example_test"}
    # No url → unscoped query (all cookies).
    asyncio.run(bc.get_cookies())
    assert ctx.cookie_urls is None


def test_get_cookies_returns_empty_when_no_browser(monkeypatch):
    monkeypatch.setattr(bc, "_ctx", None)
    assert asyncio.run(bc.get_cookies("http://portal.test")) == []


def test_tools_merged_into_toolkit_registry():
    for name in ("browser_open", "browser_click", "browser_type"):
        assert name in toolkit.TOOLS
        assert toolkit.TOOLS[name]["fn"] is getattr(bc, name)


# --------------------------------------------------------------------------- #
# ref → selector
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ref,expected", [(3, '[data-vw-ref="3"]:visible'),
                                          ("5", '[data-vw-ref="5"]:visible')])
def test_ref_selector_valid(ref, expected):
    assert bc._ref_selector(ref) == expected


def test_ref_selector_targets_only_visible_matches():
    # A stale hidden duplicate of a ref must never shadow the visible element
    # (Playwright acts on the first match of a non-strict selector).
    assert bc._ref_selector(28).endswith(":visible")


def test_snapshot_js_clears_stale_refs_before_tagging():
    # Every snapshot must drop refs left by earlier snapshots on now-hidden
    # elements — otherwise two elements can share the same data-vw-ref.
    assert "removeAttribute('data-vw-ref')" in bc._SNAPSHOT_JS
    assert bc._SNAPSHOT_JS.index("removeAttribute('data-vw-ref')") \
        < bc._SNAPSHOT_JS.index("setAttribute('data-vw-ref'")


@pytest.mark.parametrize("ref", ["abc", None, ""])
def test_ref_selector_rejects_non_numeric(ref):
    assert bc._ref_selector(ref) is None


# --------------------------------------------------------------------------- #
# Graceful when Playwright is missing
# --------------------------------------------------------------------------- #

def test_missing_playwright_returns_hint(monkeypatch):
    monkeypatch.setattr(bc, "_available", lambda: False)
    for coro in (bc.browser_open("https://x.com"), bc.browser_snapshot(),
                 bc.browser_click(1), bc.browser_type(1, "hi"), bc.browser_read(),
                 bc.browser_evaluate("1+1"), bc.browser_click_selector(".x")):
        out = asyncio.run(coro)
        assert "Playwright" in out


# --------------------------------------------------------------------------- #
# Dispatch: click / type / open / read hit the mocked page correctly
# --------------------------------------------------------------------------- #

def test_click_uses_ref_selector(fake_ctx):
    _, page = fake_ctx
    out = asyncio.run(bc.browser_click(3))
    assert ("click", '[data-vw-ref="3"]:visible') in page.calls
    assert "Clicked [3]" in out


def test_type_fills_and_optionally_submits(fake_ctx):
    _, page = fake_ctx
    asyncio.run(bc.browser_type(2, "me@example.com"))
    assert ("fill", '[data-vw-ref="2"]:visible', "me@example.com") in page.calls
    assert not any(c[0] == "press" for c in page.calls)

    page.calls.clear()
    asyncio.run(bc.browser_type(2, "pw", submit=True))
    assert ("press", '[data-vw-ref="2"]:visible', "Enter") in page.calls


def test_bad_ref_is_rejected_before_dispatch(fake_ctx):
    _, page = fake_ctx
    out = asyncio.run(bc.browser_click("not-a-number"))
    assert "numeric 'ref'" in out
    assert page.calls == []


def test_open_navigates_and_snapshots(fake_ctx):
    _, page = fake_ctx
    page.url = "about:blank"                             # a fresh tab to navigate
    out = asyncio.run(bc.browser_open("example.com"))    # scheme auto-added
    assert ("goto", "https://example.com") in page.calls
    assert 'button "Sign in"' in out


# --------------------------------------------------------------------------- #
# browser_open tab rule: reuse same-address tab, else new tab, never hijack
# --------------------------------------------------------------------------- #

def _open_ctx(monkeypatch, urls):
    """A FakeCtx pre-populated with tabs on the given urls; browser_open wired to it."""
    pages = [FakePage(url=u) for u in urls]
    ctx = FakeCtx(pages[0])
    ctx.pages = list(pages)

    async def _get_context():
        return ctx
    monkeypatch.setattr(bc, "_available", lambda: True)
    monkeypatch.setattr(bc, "_get_context", _get_context)
    monkeypatch.setattr(bc, "_current_page", None)
    return ctx, pages


PORTAL = "http://portal.test:31641/pages/X"


def test_same_address_matching():
    f = bc._same_address
    assert f("http://h:8/pages/X", "http://h:8/pages/X")
    assert f("http://h:8/pages/X/", "http://h:8/pages/X")     # trailing slash ignored
    assert f("http://h:8/anything", "http://h:8")            # origin-only target → any path on host
    assert not f("http://h:8/pages/X", "http://h:8/pages/Y")  # different path
    assert not f("http://other/pages/X", "http://h/pages/X")  # different host


def test_open_reuses_tab_on_same_address(monkeypatch):
    ctx, pages = _open_ctx(monkeypatch, [PORTAL])
    out = asyncio.run(bc.browser_open(PORTAL))
    assert "Reused the tab" in out
    assert len(ctx.pages) == 1                               # no new tab
    assert ("goto", PORTAL) not in pages[0].calls               # NOT reloaded
    assert ("bring_to_front", PORTAL) in pages[0].calls         # just focused
    assert bc._current_page is pages[0]


def test_open_uses_new_tab_when_active_is_different(monkeypatch):
    # The user's own tab (Google Sheets) must NOT be navigated — open a new tab.
    ctx, pages = _open_ctx(monkeypatch, ["https://docs.google.com/sheet1"])
    out = asyncio.run(bc.browser_open(PORTAL))
    assert "new tab" in out
    assert len(ctx.pages) == 2                               # a new tab was opened
    assert pages[0].url == "https://docs.google.com/sheet1"  # user's tab left untouched
    assert ("goto", PORTAL) not in pages[0].calls
    assert ctx.pages[1].url == PORTAL                           # the new tab holds the page


def test_open_navigates_a_blank_tab_without_new(monkeypatch):
    ctx, pages = _open_ctx(monkeypatch, ["about:blank"])
    asyncio.run(bc.browser_open(PORTAL))
    assert len(ctx.pages) == 1                               # reused the blank tab
    assert pages[0].url == PORTAL                    # snapshot lines included


def test_read_returns_page_text(fake_ctx):
    out = asyncio.run(bc.browser_read())
    assert "hello world" in out


# --------------------------------------------------------------------------- #
# browser_click_selector — CSS/XPath click for elements with no snapshot ref
# --------------------------------------------------------------------------- #

def test_click_selector_clicks_first_match(fake_ctx):
    _, page = fake_ctx
    sel = ".vxe-body--row:first-child td.col--checkbox"
    out = asyncio.run(bc.browser_click_selector(sel))
    assert ("locator_click", sel, False) in page.calls
    assert f"Clicked {sel!r}" in out
    assert 'button "Sign in"' in out            # fresh snapshot appended


def test_click_selector_falls_back_to_force_click_when_covered(fake_ctx):
    # vxe-table fixed columns render a clone ABOVE the body cell — the normal
    # click fails the hit-target check and must fall back to force.
    _, page = fake_ctx
    page.click_blocked = True
    out = asyncio.run(bc.browser_click_selector("td.col--checkbox"))
    assert ("locator_click", "td.col--checkbox", True) in page.calls
    assert "forced click" in out
    assert "failed" not in out


def test_click_selector_falls_back_to_js_dispatch_last(fake_ctx):
    _, page = fake_ctx
    page.click_blocked = True
    page.force_blocked = True
    out = asyncio.run(bc.browser_click_selector("td.col--checkbox"))
    assert ("dispatch_event", "td.col--checkbox", "click") in page.calls
    assert "JS click" in out
    assert "failed" not in out


def test_click_selector_reports_multiple_matches(fake_ctx):
    _, page = fake_ctx
    page.match_count = 3
    out = asyncio.run(bc.browser_click_selector("tbody tr .vxe-checkbox--icon"))
    assert "matched 3 elements; clicked the first" in out


def test_click_selector_no_match_is_a_clear_error(fake_ctx):
    _, page = fake_ctx
    page.match_count = 0
    out = asyncio.run(bc.browser_click_selector(".nope"))
    assert "no element matches" in out
    assert not any(c[0] == "locator_click" for c in page.calls)


def test_click_selector_needs_selector_and_open_page(fake_ctx):
    _, page = fake_ctx
    assert "needs a 'selector'" in asyncio.run(bc.browser_click_selector("  "))
    page.url = "about:blank"
    assert "browser_open" in asyncio.run(bc.browser_click_selector(".x"))
    assert page.calls == []


# --------------------------------------------------------------------------- #
# browser_evaluate — in-page JS escape hatch for custom-rendered controls
# --------------------------------------------------------------------------- #

def test_evaluate_runs_script_and_returns_result_plus_snapshot(fake_ctx):
    _, page = fake_ctx
    script = "document.querySelector('.ant-checkbox-input').click()"

    async def evaluate(js, arg=None):
        page.calls.append(("evaluate", js, arg))
        return '"clicked"' if js is bc._EVAL_JS else ['[1] button "Sign in"']
    page.evaluate = evaluate

    out = asyncio.run(bc.browser_evaluate(script))
    assert ("evaluate", bc._EVAL_JS, script) in page.calls
    assert 'Result: "clicked"' in out
    assert 'button "Sign in"' in out            # fresh snapshot appended


def test_evaluate_needs_script_and_open_page(fake_ctx):
    _, page = fake_ctx
    assert "needs a 'script'" in asyncio.run(bc.browser_evaluate(""))
    page.url = "about:blank"
    assert "browser_open" in asyncio.run(bc.browser_evaluate("1+1"))
    assert page.calls == []


def test_evaluate_truncates_long_results(fake_ctx):
    _, page = fake_ctx

    async def evaluate(js, arg=None):
        return "x" * (bc._MAX_EVAL + 50) if js is bc._EVAL_JS else []
    page.evaluate = evaluate

    out = asyncio.run(bc.browser_evaluate("bigString"))
    assert "truncated" in out


def test_evaluate_wrapper_stringifies_in_page():
    # DOM nodes / circular values must be stringified inside the page —
    # returning them raw would break Playwright's structured-clone transport.
    assert "JSON.stringify" in bc._EVAL_JS
    assert "eval" in bc._EVAL_JS                # statement lists supported


# --------------------------------------------------------------------------- #
# browser_screenshot — scoped to a working folder
# --------------------------------------------------------------------------- #

def test_screenshot_tool_absent_without_working_dir():
    assert bc.make_screenshot_tool("") == {}
    assert bc.make_screenshot_tool("   ") == {}


def test_screenshot_saves_under_working_dir_and_returns_relpath(fake_ctx, tmp_path):
    _, page = fake_ctx
    reg = bc.make_screenshot_tool(str(tmp_path))
    fn = reg["browser_screenshot"]["fn"]
    out = asyncio.run(fn(path="shots/step1.png"))
    saved = tmp_path / "shots" / "step1.png"
    assert saved.exists()
    assert ("screenshot", str(saved)) in page.calls
    assert "shots/step1.png" in out
    assert "1280x800" in out


def test_screenshot_captures_full_page_not_just_viewport(fake_ctx, tmp_path):
    # A dashboard/table taller than the window must not come out cropped —
    # full_page scrolls + stitches the whole scrollable page into the capture.
    _, page = fake_ctx
    reg = bc.make_screenshot_tool(str(tmp_path))
    fn = reg["browser_screenshot"]["fn"]
    asyncio.run(fn(path="shots/step1.png"))
    assert page.screenshot_kwargs.get("full_page") is True


def test_screenshot_default_path_is_timestamped_png(fake_ctx, tmp_path):
    _, page = fake_ctx
    reg = bc.make_screenshot_tool(str(tmp_path))
    fn = reg["browser_screenshot"]["fn"]
    asyncio.run(fn())
    calls = [c for c in page.calls if c[0] == "screenshot"]
    assert len(calls) == 1
    assert calls[0][1].endswith(".png")
    assert "screenshots" in calls[0][1]


def test_screenshot_rejects_path_escape(fake_ctx, tmp_path):
    reg = bc.make_screenshot_tool(str(tmp_path))
    fn = reg["browser_screenshot"]["fn"]
    out = asyncio.run(fn(path="../../etc/evil.png"))
    assert "escapes the working folder" in out


def test_screenshot_needs_open_page(fake_ctx, tmp_path):
    _, page = fake_ctx
    page.url = "about:blank"
    reg = bc.make_screenshot_tool(str(tmp_path))
    fn = reg["browser_screenshot"]["fn"]
    out = asyncio.run(fn())
    assert "No page open yet" in out


def test_screenshot_with_agent_id_returns_renderable_image_link(fake_ctx, tmp_path):
    # With an agent_id, the tool also emits a markdown image pointing at the
    # agent-files route so the chat UI renders the PNG inline, not just its path.
    reg = bc.make_screenshot_tool(str(tmp_path), agent_id="agent-9")
    fn = reg["browser_screenshot"]["fn"]
    out = asyncio.run(fn(path="shots/step1.png"))
    assert "![screenshot](/api/agents/agent-9/files?path=shots/step1.png)" in out


def test_screenshot_without_agent_id_has_no_image_link(fake_ctx, tmp_path):
    reg = bc.make_screenshot_tool(str(tmp_path))
    fn = reg["browser_screenshot"]["fn"]
    out = asyncio.run(fn(path="shots/step1.png"))
    assert "![screenshot]" not in out


# --------------------------------------------------------------------------- #
# browser_record_start / browser_record_stop — scoped to a working folder
# --------------------------------------------------------------------------- #

@pytest.fixture
def rec_ctx(fake_ctx):
    ctx, page = fake_ctx
    bc._ctx = ctx                        # stop/teardown read the module global
    yield ctx, page
    bc._ctx = None
    bc._record_dir = None


def test_recording_tools_absent_without_working_dir():
    assert bc.make_recording_tools("") == {}
    assert bc.make_recording_tools("   ") == {}


def test_launch_kwargs_set_a_full_size_window():
    # A narrow default window renders pages cramped/cropped regardless of
    # full_page capture — force a real desktop-width window instead.
    args = bc._launch_kwargs()["args"]
    assert f"--window-size={bc._WINDOW_SIZE}" in args
    assert "--window-position=0,0" in args


def test_launch_kwargs_include_video_dir_only_while_recording(tmp_path):
    assert "record_video_dir" not in bc._launch_kwargs()
    bc._record_dir = tmp_path
    try:
        kw = bc._launch_kwargs()
        assert kw["record_video_dir"] == str(tmp_path)
        assert kw["record_video_size"] == bc._REC_SIZE
    finally:
        bc._record_dir = None


def test_record_start_restores_page_and_arms_recording(rec_ctx, tmp_path):
    ctx, page = rec_ctx
    reg = bc.make_recording_tools(str(tmp_path))
    out = asyncio.run(reg["browser_record_start"]["fn"]())
    assert ctx.closed                                   # bounced for the relaunch
    assert bc._record_dir == tmp_path / "recordings" / ".tmp"
    assert ("goto", "https://example.com") in page.calls  # page restored
    assert "Recording started" in out


def test_record_start_twice_is_rejected(rec_ctx, tmp_path):
    reg = bc.make_recording_tools(str(tmp_path))
    asyncio.run(reg["browser_record_start"]["fn"]())
    out = asyncio.run(reg["browser_record_start"]["fn"]())
    assert "Already recording" in out


def test_record_stop_without_start(rec_ctx, tmp_path):
    reg = bc.make_recording_tools(str(tmp_path))
    out = asyncio.run(reg["browser_record_stop"]["fn"]())
    assert "Not recording" in out


def test_record_stop_saves_webm_and_disarms(rec_ctx, tmp_path):
    ctx, page = rec_ctx
    reg = bc.make_recording_tools(str(tmp_path))
    asyncio.run(reg["browser_record_start"]["fn"]())
    bc._ctx = ctx                                       # relaunched context
    raw = bc._record_dir / "abc123.webm"                # playwright's temp video
    raw.write_bytes(b"\x1aE\xdf\xa3 fake webm " * 100)
    page.video = FakeVideo(raw)
    out = asyncio.run(reg["browser_record_stop"]["fn"](path="runs/case1.webm"))
    saved = tmp_path / "runs" / "case1.webm"
    assert saved.exists()
    assert not (tmp_path / "recordings" / ".tmp").exists()   # temp cleaned up
    assert bc._record_dir is None
    assert "runs/case1.webm" in out


def test_record_stop_default_path_is_timestamped(rec_ctx, tmp_path):
    ctx, page = rec_ctx
    reg = bc.make_recording_tools(str(tmp_path))
    asyncio.run(reg["browser_record_start"]["fn"]())
    bc._ctx = ctx
    raw = bc._record_dir / "abc.webm"
    raw.write_bytes(b"x" * 2048)
    page.video = FakeVideo(raw)
    out = asyncio.run(reg["browser_record_stop"]["fn"]())
    files = list((tmp_path / "recordings").glob("*.webm"))
    assert len(files) == 1
    assert "recordings/" in out


def test_record_stop_with_no_video_reports_it(rec_ctx, tmp_path):
    ctx, page = rec_ctx
    reg = bc.make_recording_tools(str(tmp_path))
    asyncio.run(reg["browser_record_start"]["fn"]())
    bc._ctx = ctx
    page.video = None
    out = asyncio.run(reg["browser_record_stop"]["fn"]())
    assert "no video was captured" in out
    assert bc._record_dir is None


def test_record_stop_rejects_path_escape(rec_ctx, tmp_path):
    reg = bc.make_recording_tools(str(tmp_path))
    asyncio.run(reg["browser_record_start"]["fn"]())
    out = asyncio.run(reg["browser_record_stop"]["fn"](path="../../etc/evil.webm"))
    assert "escapes the working folder" in out
    assert bc._record_dir is not None                   # still armed; not lost


def test_recording_schemas_are_valid():
    reg = bc.make_recording_tools("/tmp/x")
    for name in ("browser_record_start", "browser_record_stop"):
        fn = reg[name]["schema"]["function"]
        assert fn["name"] == name
        assert fn["parameters"]["type"] == "object"
