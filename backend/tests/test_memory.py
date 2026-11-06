"""Agent memory — store CRUD + context build + tools."""

import asyncio

import pytest

import config
from agent import memory as mem
from storage import memory_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return memory_store


def test_add_list_search_delete(store):
    store.add("a1", "User prefers TypeScript", tags="prefs")
    store.add("a1", "Project deploys to GCP")
    store.add("a2", "different agent")  # scoped out
    notes = store.list_for("a1")
    assert len(notes) == 2
    assert store.count("a1") == 2
    hits = store.search("a1", "typescript")
    assert len(hits) == 1 and "TypeScript" in hits[0]["content"]
    assert store.delete("a1", notes[0]["id"]) is True
    assert store.count("a1") == 1


def test_add_dedupes_exact_content(store):
    a = store.add("a1", "same fact")
    b = store.add("a1", "same fact")
    assert a["id"] == b["id"]
    assert store.count("a1") == 1


def test_clear(store):
    store.add("a1", "one")
    store.add("a1", "two")
    assert store.clear("a1") == 2
    assert store.count("a1") == 0


def test_build_memory_context(store):
    assert mem.build_memory_context("a1") == ""  # empty when no notes
    store.add("a1", "Likes concise answers", tags="style")
    ctx = mem.build_memory_context("a1")
    assert "## Memory" in ctx
    assert "Likes concise answers" in ctx
    assert "[style]" in ctx


def test_memory_tools(store):
    tools = mem.make_memory_tools("a1")
    assert set(tools) == {"memory_write", "memory_search", "memory_forget"}
    out = asyncio.run(tools["memory_write"]["fn"](content="Remember this"))
    assert "remembered" in out
    assert store.count("a1") == 1
    found = asyncio.run(tools["memory_search"]["fn"](query="remember"))
    assert "Remember this" in found
    note_id = store.list_for("a1")[0]["id"]
    assert "forgot" in asyncio.run(tools["memory_forget"]["fn"](id=note_id))
    assert store.count("a1") == 0


def test_memory_write_requires_content(store):
    tools = mem.make_memory_tools("a1")
    assert "needs" in asyncio.run(tools["memory_write"]["fn"](content="  "))


def test_context_has_no_footer_when_everything_is_shown(store):
    for i in range(3):
        store.add("a1", f"fact {i}")
    ctx = mem.build_memory_context("a1")
    assert "older note(s) not shown" not in ctx  # all 3 shown -> no phantom footer


def test_context_footer_counts_hidden_notes_exactly(store, monkeypatch):
    monkeypatch.setattr(mem, "MAX_INJECTED_NOTES", 2)
    for i in range(3):
        store.add("a1", f"fact {i}")
    ctx = mem.build_memory_context("a1")
    assert "(1 older note(s) not shown" in ctx
