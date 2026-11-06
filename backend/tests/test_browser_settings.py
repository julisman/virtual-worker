"""Runtime browser settings (settings.json ``browser`` object) + resolver precedence.

Precedence per key: an explicitly-set env var wins (presence check, so "set to
the default value" still overrides), else the persisted setting, else the
built-in default. Resolvers read the store at USE time. Stores are isolated by
monkeypatching config.APP_HOME to tmp_path (repo convention).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import config
from agent import browser_control as bc
from storage import settings


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolate settings.json under tmp_path so each test starts from an empty store."""
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def _clear_env(monkeypatch):
    for env in config.BROWSER_ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)


# --------------------------------------------------------------------------- #
# Precedence: env set beats setting; setting beats default; unset env + setting.
# --------------------------------------------------------------------------- #

def test_control_mode_setting_beats_default(store, monkeypatch):
    _clear_env(monkeypatch)
    assert config.control_browser_headless() is config.CONTROL_BROWSER_HEADLESS  # default
    settings.set("browser", {"controlMode": "headless"})
    assert config.control_browser_headless() is True                            # setting


def test_control_mode_env_beats_setting(store, monkeypatch):
    settings.set("browser", {"controlMode": "headless"})
    monkeypatch.setenv("VW_CONTROL_BROWSER_HEADLESS", "0")                      # env: visible
    assert config.control_browser_headless() is False                           # env wins


def test_channel_setting_and_env_and_default(store, monkeypatch):
    _clear_env(monkeypatch)
    settings.set("browser", {"channel": "msedge"})
    assert config.browser_channel() == "msedge"                                 # setting
    # An empty-string channel is an explicit choice (force bundled Chromium),
    # distinct from null (= auto/default).
    settings.set("browser", {"channel": ""})
    assert config.browser_channel() == ""
    settings.set("browser", {"channel": None})
    assert config.browser_channel() == config.BROWSER_CHANNEL                    # null → default
    monkeypatch.setenv("VW_BROWSER_CHANNEL", "chrome")
    settings.set("browser", {"channel": "msedge"})
    assert config.browser_channel() == "chrome"                                 # env wins


def test_cdp_url_unset_env_uses_setting(store, monkeypatch):
    _clear_env(monkeypatch)
    settings.set("browser", {"cdpUrl": "http://127.0.0.1:9222"})
    assert config.browser_cdp_url() == "http://127.0.0.1:9222"                   # setting
    monkeypatch.setenv("VW_BROWSER_CDP_URL", "")                               # present, empty
    assert config.browser_cdp_url() == ""                                        # env presence wins


def test_fetch_headless_precedence(store, monkeypatch):
    _clear_env(monkeypatch)
    assert config.browser_fetch_headless() is True                              # default
    settings.set("browser", {"fetchHeadless": False})
    assert config.browser_fetch_headless() is False                             # setting
    monkeypatch.setenv("VW_BROWSER_HEADLESS", "1")
    assert config.browser_fetch_headless() is True                              # env wins


def test_profile_dir_precedence(store, monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    assert config.browser_profile() == config.BROWSER_PROFILE                    # default
    settings.set("browser", {"profileDir": str(tmp_path / "prof")})
    assert config.browser_profile() == tmp_path / "prof"                         # setting
    monkeypatch.setenv("VW_BROWSER_PROFILE", str(tmp_path / "env"))
    assert config.browser_profile() == tmp_path / "env"                          # env wins


def test_focus_policy_is_settings_only(store, monkeypatch):
    _clear_env(monkeypatch)
    assert config.browser_focus_policy() == "allow"                             # default
    settings.set("browser", {"focusPolicy": "never"})
    assert config.browser_focus_policy() == "never"                             # setting


# --------------------------------------------------------------------------- #
# focusPolicy=never suppresses bring_to_front at both call sites.
# --------------------------------------------------------------------------- #

class FakePage:
    def __init__(self, url="https://a.test/", title="A"):
        self.url = url
        self._title = title
        self.fronted = False

    def is_closed(self):
        return False

    async def title(self):
        return self._title

    async def bring_to_front(self):
        self.fronted = True

    async def evaluate(self, script, *_a):
        return ['[1] button "ok"']

    async def goto(self, url, **_kw):
        self.url = url

    async def wait_for_load_state(self, *_a, **_kw):
        pass


class FakeCtx:
    def __init__(self, pages):
        self.pages = list(pages)

    async def new_page(self):
        p = FakePage(url="about:blank", title="")
        self.pages.append(p)
        return p


def _wire(monkeypatch, ctx):
    async def _get_context():
        return ctx
    monkeypatch.setattr(bc, "_available", lambda: True)
    monkeypatch.setattr(bc, "_get_context", _get_context)
    monkeypatch.setattr(bc, "_current_page", None)
    monkeypatch.setattr(bc, "_cdp", None)


def test_focus_never_suppresses_front_in_browser_open(store, monkeypatch):
    _clear_env(monkeypatch)
    settings.set("browser", {"focusPolicy": "never"})
    url = "http://reuse.test/page"
    page = FakePage(url=url)
    ctx = FakeCtx([page])
    _wire(monkeypatch, ctx)
    out = asyncio.run(bc.browser_open(url))          # reuse-existing-tab path
    assert "Reused the tab" in out
    assert page.fronted is False                     # focus steal skipped


def test_focus_allow_keeps_front_in_browser_open(store, monkeypatch):
    _clear_env(monkeypatch)
    settings.set("browser", {"focusPolicy": "allow"})
    url = "http://reuse.test/page"
    page = FakePage(url=url)
    ctx = FakeCtx([page])
    _wire(monkeypatch, ctx)
    asyncio.run(bc.browser_open(url))
    assert page.fronted is True


def test_focus_never_suppresses_front_in_tab_select(store, monkeypatch):
    _clear_env(monkeypatch)
    settings.set("browser", {"focusPolicy": "never"})
    a, b = FakePage(url="https://a.test/"), FakePage(url="https://b.test/")
    ctx = FakeCtx([a, b])
    _wire(monkeypatch, ctx)
    monkeypatch.setattr(bc, "_current_page", b)
    out = asyncio.run(bc.browser_tab_select(1))
    assert "Switched to tab [1]." in out
    assert a.fronted is False                        # focus steal skipped


def test_focus_allow_keeps_front_in_tab_select(store, monkeypatch):
    _clear_env(monkeypatch)
    settings.set("browser", {"focusPolicy": "allow"})
    a, b = FakePage(url="https://a.test/"), FakePage(url="https://b.test/")
    ctx = FakeCtx([a, b])
    _wire(monkeypatch, ctx)
    monkeypatch.setattr(bc, "_current_page", b)
    asyncio.run(bc.browser_tab_select(1))
    assert a.fronted is True


# --------------------------------------------------------------------------- #
# Settings route: GET/PUT round-trip + env-override flags + relaunch.
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(store):
    import app
    return TestClient(app.app)


def test_get_settings_returns_defaults(client, monkeypatch):
    _clear_env(monkeypatch)
    body = client.get("/api/browser/settings").json()
    assert body["controlMode"] == "visible"
    assert body["focusPolicy"] == "allow"
    assert body["channel"] is None
    assert body["envOverrides"] == {k: False for k in config.BROWSER_ENV_VARS}


def test_put_settings_persists_and_get_reads_back(client, monkeypatch):
    _clear_env(monkeypatch)
    r = client.put("/api/browser/settings", json={
        "controlMode": "headless", "focusPolicy": "never",
        "channel": "msedge", "cdpUrl": "http://127.0.0.1:9222",
        "fetchHeadless": False, "profileDir": "/tmp/prof"})
    assert r.status_code == 200
    got = client.get("/api/browser/settings").json()
    assert got["controlMode"] == "headless" and got["focusPolicy"] == "never"
    assert got["channel"] == "msedge" and got["cdpUrl"] == "http://127.0.0.1:9222"
    assert config.browser_focus_policy() == "never"           # applied live


def test_put_settings_rejects_bad_enum(client):
    r = client.put("/api/browser/settings", json={"controlMode": "nope"})
    assert r.status_code == 400


def test_get_settings_flags_env_override(client, monkeypatch):
    monkeypatch.setenv("VW_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    body = client.get("/api/browser/settings").json()
    assert body["envOverrides"]["cdpUrl"] is True


def test_relaunch_closes_control_browser(client, monkeypatch):
    called = {"hit": False}

    async def _close():
        called["hit"] = True
    monkeypatch.setattr(browser_control_module(), "close_control_browser", _close)
    r = client.post("/api/browser/relaunch")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called["hit"] is True


def browser_control_module():
    from agent import browser_control
    return browser_control
