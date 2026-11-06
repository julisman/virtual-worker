"""Tier-1 linked-repository tools (linked-wiki revamp §3.1) — read-only fs + shell
rooted at the repo, refusal logging, and wiki-arg routing. Hermetic."""

from __future__ import annotations

import asyncio

import pytest

import config
from agent.linked_repo import make_linked_repo_tools
from storage import wiki_store


@pytest.fixture()
def linked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    (repo / "sources").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Use the index.\n")
    (repo / "guide.md").write_text("# Guide\nhello world\n")
    (repo / "sources" / "code.py").write_text("def foo():\n    return 42\n")
    w = wiki_store.create_linked_wiki("kb", str(repo))
    return w, repo


def _call(tool, **kw):
    return asyncio.run(tool["fn"](**kw))


def test_grants_readonly_repo_tools_rooted_at_repo(linked):
    w, repo = linked
    reg, note = make_linked_repo_tools({"id": "a", "wikis": [w["id"]]})
    assert {"repo_read_file", "repo_list_dir", "repo_glob", "repo_grep", "repo_shell"} <= set(reg)
    assert "repo_write_file" not in reg and "write_file" not in reg
    assert str(repo) in note
    # The repo root is the unit — code under sources/ is reachable (not the .md-only wiki root).
    out = _call(reg["repo_read_file"], path="sources/code.py")
    assert "def foo" in out


def test_repo_schemas_advertise_ranged_batch_and_multi_pattern(linked):
    # The repo_* schemas are hand-written duplicates of the fs ones — if they
    # don't declare the params, the model can never use them (it only reads
    # whole files, exactly the waste the ranged mode exists to prevent).
    w, _repo = linked
    reg, _ = make_linked_repo_tools({"id": "a", "wikis": [w["id"]]})
    read_props = reg["repo_read_file"]["schema"]["function"]["parameters"]["properties"]
    assert {"start_line", "end_line", "files"} <= set(read_props)
    grep_props = reg["repo_grep"]["schema"]["function"]["parameters"]["properties"]
    assert "patterns" in grep_props
    assert "pipe" in reg["repo_shell"]["schema"]["function"]["description"]
    # And the dispatcher actually forwards them to the upgraded fs tools.
    out = _call(reg["repo_read_file"], path="sources/code.py", start_line=2, end_line=2)
    assert "== sources/code.py:2-2 ==" in out and "2\t    return 42" in out
    out = _call(reg["repo_read_file"], files=["sources/code.py:1", "guide.md:2-2"])
    assert "1\tdef foo" in out and "2\thello world" in out
    out = _call(reg["repo_grep"], patterns=["foo", "hello"])
    assert "code.py:1" in out and "guide.md:2" in out


def test_shell_off_omits_repo_shell_but_keeps_fs(linked):
    w, _repo = linked
    reg, _ = make_linked_repo_tools({"id": "a", "wikis": [w["id"]], "guardrails": {"shell": "off"}})
    assert "repo_shell" not in reg
    assert "repo_read_file" in reg


def test_refused_repo_shell_logs_a_refusal_row(linked):
    w, _repo = linked
    reg, _ = make_linked_repo_tools({"id": "a", "wikis": [w["id"]]})
    out = _call(reg["repo_shell"], command="rm -rf /")
    assert out.startswith("refused")
    rows = wiki_store.list_shell_refusals(w["id"])
    assert rows and rows[0]["command"] == "rm -rf /"


def test_wiki_arg_routes_between_two_linked_repos(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo1 = tmp_path / "one"
    repo1.mkdir()
    (repo1 / "only-one.md").write_text("# one")
    repo2 = tmp_path / "two"
    repo2.mkdir()
    (repo2 / "only-two.md").write_text("# two")
    w1 = wiki_store.create_linked_wiki("one", str(repo1))
    w2 = wiki_store.create_linked_wiki("two", str(repo2))
    reg, _ = make_linked_repo_tools({"id": "a", "wikis": [w1["id"], w2["id"]]})

    listed_two = _call(reg["repo_list_dir"], wiki="two")
    assert "only-two.md" in listed_two and "only-one.md" not in listed_two
    listed_one = _call(reg["repo_list_dir"], wiki="one")
    assert "only-one.md" in listed_one
    # Default (no wiki arg) picks the first linked wiki.
    assert "only-one.md" in _call(reg["repo_list_dir"])


def test_no_linked_wiki_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    reg, note = make_linked_repo_tools({"id": "a", "wikis": []})
    assert reg == {} and note == ""


def test_default_repo_tooling_runs_repo_script_readonly_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    (repo / "scripts").mkdir(parents=True)
    (repo / "intro.md").write_text("# i\n")
    kb = repo / "scripts" / "kb"
    kb.write_text("#!/bin/sh\necho kb-ran\n")
    kb.chmod(0o755)

    # The creation default is now repo-tooling, so the in-repo script is allowed.
    w = wiki_store.create_linked_wiki("kb", str(repo))
    assert w["trustTier"] == "repo-tooling"
    reg, note = make_linked_repo_tools({"id": "a", "wikis": [w["id"]]})
    out = _call(reg["repo_shell"], command="./scripts/kb chain x")
    assert "kb-ran" in out
    assert "repo-tooling" in note

    # Dropping the same link to read-only refuses the command — tier-1 still enforceable,
    # and the refusal is logged per linked wiki.
    wiki_store.set_trust_tier(w["id"], "readonly")
    reg2, _ = make_linked_repo_tools({"id": "a", "wikis": [w["id"]]})
    refused = _call(reg2["repo_shell"], command="./scripts/kb chain x")
    assert refused.startswith("refused")
    rows = wiki_store.list_shell_refusals(w["id"])
    assert rows and rows[0]["command"] == "./scripts/kb chain x"
