"""Agent-context handoff to shell subprocesses + the browser route additions.

Covers the frozen contract a CLI sidecar (web-auth) is built against:
  * ``make_shell_tool(extra_env=…)`` layers vars onto the child env (and leaves
    inheritance untouched when omitted);
  * ``compose._shell_env`` builds VW_API_BASE / VW_AGENT_BROWSER;
  * ``POST /api/browser/click_selector`` and the ``pageUrl`` key on
    ``GET /api/browser/status``.

Hermetic — real ``python3`` for the env probe, stubbed browser control, no model,
no real browser.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import config
from agent import browser_control, compose
from agent.shell_tool import make_shell_tool


def _call(tool, **kw):
    return asyncio.run(tool["fn"](**kw))


_PROBE = "import os;print(os.environ.get('VW_API_BASE',''))"


# --- make_shell_tool(extra_env=…) ------------------------------------------- #

def test_extra_env_reaches_subprocess(tmp_path):
    # workspace mode (a working folder) so a bare interpreter runs.
    t = make_shell_tool(str(tmp_path), mode="workspace",
                        extra_env={"VW_API_BASE": "http://127.0.0.1:9999"})
    out = _call(t, command=f"python3 -c \"{_PROBE}\"")
    assert "http://127.0.0.1:9999" in out


def test_extra_env_in_pipeline(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="workspace",
                        extra_env={"VW_API_BASE": "http://127.0.0.1:9999"})
    out = _call(t, command=f"python3 -c \"{_PROBE}\" | cat")
    assert "http://127.0.0.1:9999" in out


def test_no_extra_env_inherits_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("VW_API_BASE", "http://inherited:1234")
    t = make_shell_tool(str(tmp_path), mode="workspace")  # extra_env=None
    out = _call(t, command=f"python3 -c \"{_PROBE}\"")
    assert "http://inherited:1234" in out


# --- compose._shell_env ----------------------------------------------------- #

def test_shell_env_browser_flag_on():
    env = compose._shell_env(["browser_open", "write_file"])
    assert env["VW_AGENT_BROWSER"] == "1"


def test_shell_env_browser_flag_off():
    env = compose._shell_env(["write_file", "shell"])
    assert env["VW_AGENT_BROWSER"] == "0"


def test_shell_env_api_base_explicit(monkeypatch):
    monkeypatch.setenv("VW_API_BASE", "http://example:5555")
    assert compose._shell_env([])["VW_API_BASE"] == "http://example:5555"


def test_shell_env_api_base_from_backend_port(monkeypatch):
    monkeypatch.delenv("VW_API_BASE", raising=False)
    monkeypatch.setenv("BACKEND_PORT", "8898")
    assert compose._shell_env([])["VW_API_BASE"] == "http://127.0.0.1:8898"


def test_shell_env_api_base_default(monkeypatch):
    monkeypatch.delenv("VW_API_BASE", raising=False)
    monkeypatch.delenv("BACKEND_PORT", raising=False)
    assert compose._shell_env([])["VW_API_BASE"] == "http://127.0.0.1:8888"


# --- routes ----------------------------------------------------------------- #

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import app
    return TestClient(app.app)


def test_click_selector_success(client, monkeypatch):
    async def stub(selector):
        return "Clicked '.foo'.\n\n[1] button"
    monkeypatch.setattr(browser_control, "browser_click_selector", stub)
    r = client.post("/api/browser/click_selector", json={"selector": ".foo"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "Clicked" in body["message"]


def test_click_selector_failure(client, monkeypatch):
    async def stub(selector):
        return "browser_click_selector: no element matches '.foo'"
    monkeypatch.setattr(browser_control, "browser_click_selector", stub)
    r = client.post("/api/browser/click_selector", json={"selector": ".foo"})
    body = r.json()
    assert body["ok"] is False and "no element" in body["message"]


def test_click_selector_empty_skips_browser(client, monkeypatch):
    called = {"hit": False}

    async def stub(selector):
        called["hit"] = True
        return "Clicked."
    monkeypatch.setattr(browser_control, "browser_click_selector", stub)
    r = client.post("/api/browser/click_selector", json={"selector": ""})
    body = r.json()
    assert body["ok"] is False and called["hit"] is False


def test_status_has_page_url_empty(client, monkeypatch):
    # no tab tracked → pageUrl is "" (isolate from any module-global page other
    # browser tests may have left behind).
    monkeypatch.setattr(browser_control, "_current_page", None)
    r = client.get("/api/browser/status")
    assert r.status_code == 200
    body = r.json()
    assert "pageUrl" in body and body["pageUrl"] == ""
