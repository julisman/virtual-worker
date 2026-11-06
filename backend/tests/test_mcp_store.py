"""MCP server config store — CRUD + slug."""

import pytest

import config
import storage.mcp_store as ms


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return ms


def test_slugify():
    assert ms.slugify("My Server!") == "my_server"
    assert ms.slugify("") == "server"


def test_create_stdio_and_get(store):
    s = store.create_server({
        "name": "Filesystem", "transport": "stdio",
        "command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"},
    })
    assert s["slug"] == "filesystem"
    assert s["transport"] == "stdio"
    assert s["args"] == ["-y", "pkg"]
    assert s["env"] == {"K": "v"}
    assert s["enabled"] is True
    assert store.get_server(s["id"])["command"] == "npx"


def test_create_http(store):
    s = store.create_server({"name": "Remote", "transport": "http", "url": "https://x/mcp"})
    assert s["transport"] == "http"
    assert s["url"] == "https://x/mcp"


def test_update_partial(store):
    s = store.create_server({"name": "X", "command": "a"})
    upd = store.update_server(s["id"], {"enabled": False})
    assert upd["enabled"] is False
    assert upd["command"] == "a"  # untouched


def test_delete(store):
    s = store.create_server({"name": "X"})
    assert store.delete_server(s["id"]) is True
    assert store.get_server(s["id"]) is None
    assert store.delete_server("missing") is False


def test_allowed_tools_round_trip_and_default(store):
    # Absent → empty list out (= no filter), existing rows unaffected.
    s = store.create_server({"name": "Plain", "command": "a"})
    assert s["allowedTools"] == []
    # Set, read back, then clear with an explicit empty list.
    s2 = store.create_server({"name": "Doctor", "command": "npx",
                              "allowedTools": ["shortlink_generate", " ", ""]})
    assert s2["allowedTools"] == ["shortlink_generate"]
    updated = store.update_server(s2["id"], {"allowedTools": []})
    assert updated["allowedTools"] == []
    # Update that doesn't mention allowedTools leaves it unchanged.
    s3 = store.update_server(s["id"], {"name": "Plain2"})
    assert s3["allowedTools"] == []
