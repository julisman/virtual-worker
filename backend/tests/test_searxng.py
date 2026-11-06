"""Managed SearXNG — install detection + browser_search URL fallback (no spawn)."""

import asyncio

import pytest

import config
from agent import tools
from runtime import searxng_manager as sm


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(config, "SEARXNG_HOME", tmp_path / "searxng")
    return tmp_path


def test_installed_false_when_missing(home):
    assert sm.installed() is False


def test_installed_true_with_venv_and_src(home):
    (home / "searxng" / "venv" / "bin").mkdir(parents=True)
    (home / "searxng" / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (home / "searxng" / "src" / "searx").mkdir(parents=True)
    (home / "searxng" / "src" / "searx" / "webapp.py").write_text("# app")
    assert sm.installed() is True


def test_base_url_uses_port(home, monkeypatch):
    monkeypatch.setattr(config, "SEARXNG_PORT", 8899)
    assert sm.base_url() == "http://127.0.0.1:8899"


def test_adopt_noop_when_not_installed(home, monkeypatch):
    monkeypatch.setattr(config, "MANAGED_SEARXNG", True)
    out = asyncio.run(sm.adopt_or_autostart())
    assert out == {"managed": False, "reason": "not installed"}


def test_adopt_noop_when_disabled(home, monkeypatch):
    monkeypatch.setattr(config, "MANAGED_SEARXNG", False)
    out = asyncio.run(sm.adopt_or_autostart())
    assert out["managed"] is False and out["reason"] == "disabled"


def test_browser_search_falls_back_to_managed_url(monkeypatch):
    # No per-agent URL, no VW_SEARXNG_URL → use the managed instance's URL.
    monkeypatch.setattr(tools, "_SEARXNG_URL", "")
    monkeypatch.setattr(tools, "_managed_searxng_url", lambda: "http://managed.local")
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"results": [{"title": "T", "url": "https://u", "content": "c"}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k): seen["url"] = url; return _Resp()

    monkeypatch.setattr(tools.httpx, "AsyncClient", _Client)
    out = asyncio.run(tools.make_search_tool({"provider": "searxng"})["fn"](query="q"))
    assert "T" in out
    assert seen["url"].startswith("http://managed.local")


def test_managed_url_helper_gated_by_install(home, monkeypatch):
    monkeypatch.setattr(config, "MANAGED_SEARXNG", True)      # managed on, but not installed
    assert tools._managed_searxng_url() == ""