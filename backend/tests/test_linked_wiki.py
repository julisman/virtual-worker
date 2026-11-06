"""Hermetic PRD-21 linked Wiki tests — no real checkout or model needed."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

import config
from agent import wiki
from storage import agent_store, linked_wiki, wiki_store


@pytest.fixture()
def linked_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "sample-kb"
    (repo / "docs" / "nested").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Use the registered query operation. Read CLAUDE.md for the full guide.\n")
    (repo / "docs" / "AGENTS.md").write_text("Wiki-root instructions apply to every page.\n")
    (repo / "docs" / "nested" / "AGENTS.md").write_text("Nested pages have extra rules.\n")
    (repo / "docs" / "retry.md").write_text("# Retry policy\n\nRetry failures with exponential backoff.\n")
    (repo / "docs" / "nested" / "source.md").write_text("# Sources\n\nCanonical source list.\n")
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(json.dumps({"wikiRoot": "docs", "operations": {
        "search": {"description": "Repo-owned search", "argv": ["./query", "{query}"],
                  "inputs": {"query": {"type": "string", "maxLength": 40}}, "mode": "assistant"},
        "update": {"argv": ["./update"], "mode": "explicit_user"},
    }}))
    (repo / "query").write_text("#!/bin/sh\nprintf 'native result: %s\\n' \"$1\"\n")
    (repo / "query").chmod(0o755)
    (repo / "update").write_text("#!/bin/sh\nprintf update\n")
    (repo / "update").chmod(0o755)
    return repo


def _call(tool, **kwargs):
    return asyncio.run(tool["fn"](**kwargs))


def test_linked_repo_is_read_only_and_direct_searches(linked_repo):
    w = wiki_store.create_linked_wiki("Sample", str(linked_repo))
    assert w["sourceKind"] == "linked_repository" and w["readOnly"]
    assert w["id"] in agent_store.get_default_agent()["wikis"]
    assert w["wikiRoot"] == "docs"
    assert wiki_store.list_pages(w["id"])[0]["path"] == "nested/source.md"
    assert wiki_store.search_pages(w["id"], "exponential")[0]["path"] == "retry.md"
    with pytest.raises(wiki_store.WikiError, match="read-only"):
        wiki_store.write_page(w["id"], "nope.md", "no")
    assert not (linked_repo / "nope.md").exists()


def test_linked_agents_context_scoped_instructions_and_trusted_operation(linked_repo):
    w = wiki_store.create_linked_wiki("Sample", str(linked_repo))
    context = wiki.build_wiki_context([w["id"]])
    assert "Read CLAUDE.md" in context and "Wiki-root instructions" in context
    tools = wiki.make_wiki_tools({"id": "agent", "wikis": [w["id"]]})
    assert "wiki_write" not in tools
    assert {"wiki_search", "wiki_read", "wiki_list", "wiki_status", "wiki_instructions", "wiki_run_operation"} <= set(tools)
    root = _call(tools["wiki_instructions"], wiki="Sample")
    scoped = _call(tools["wiki_instructions"], wiki="Sample", path="nested/source.md")
    assert "Read CLAUDE.md" in root and "Nested pages" in scoped
    denied = _call(tools["wiki_run_operation"], wiki="Sample", operation="search", inputs={"query": "retry"})
    assert "not trusted" in denied
    wiki_store.set_trusted_operations(w["id"], ["search"])
    assert "native result: retry" in _call(tools["wiki_run_operation"], wiki="Sample", operation="search", inputs={"query": "retry"})
    assert "native result: retry" in _call(tools["wiki_search"], wiki="Sample", query="retry")


def test_linked_read_tools_redirect_to_repo_tools(linked_repo):
    w = wiki_store.create_linked_wiki("Sample", str(linked_repo))
    tools = wiki.make_wiki_tools({"id": "agent", "wikis": [w["id"]]})
    read = _call(tools["wiki_read"], wiki="Sample", path="nested/source.md")
    assert "repo_read_file" in read and "docs/nested/source.md" in read
    assert "Canonical source list" not in read
    listing = _call(tools["wiki_list"], wiki="Sample")
    assert "repo_list_dir" in listing and "docs/" in listing
    # The direct-search fallback surfaces repo-root-relative paths + a verify nudge.
    search = _call(tools["wiki_search"], wiki="Sample", query="exponential")
    assert "docs/retry.md" in search and "repo_grep" in search


def test_changed_manifest_revokes_operation_trust(linked_repo):
    w = wiki_store.create_linked_wiki("Sample", str(linked_repo))
    wiki_store.set_trusted_operations(w["id"], ["search"])
    manifest_path = linked_repo / ".virtual-worker" / "wiki.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["operations"]["search"]["argv"] = ["./query", "--new", "{query}"]
    manifest_path.write_text(json.dumps(manifest))

    assert "search" in wiki_store.probe_linked_wiki(w["id"])["invalidatedOperations"]
    with pytest.raises(wiki_store.WikiError, match="not trusted"):
        wiki_store.run_linked_operation(w["id"], "search", {"query": "retry"})


def test_registrar_proposes_link_and_user_approves(linked_repo):
    import asyncio as _asyncio

    from agent.compose import compose

    system, registry, *_ = compose(agent_store.get_default_agent())
    assert {"wiki_link_probe", "wiki_link_propose", "wiki_ops_propose", "wiki_proposal_status"} <= set(registry)
    assert "Do not use `wiki_ingest_repo`" in system
    registrar = wiki.make_wiki_registrar_tools(agent_store.get_default_agent())
    proposed = json.loads(_call(
        registrar["wiki_link_propose"], repo_path=str(linked_repo), wiki_root="docs", name="Inventory Wiki"))
    assert proposed["kind"] == "link" and proposed["proposalId"]
    # The tool only proposes — the wiki does not exist until the user approves.
    assert not any(row["name"] == "Inventory Wiki" for row in wiki_store.list_wikis())
    result = _asyncio.run(wiki_store.approve_proposal(proposed["proposalId"], {}))
    assert result["status"] == "executed"
    linked = next(row for row in wiki_store.list_wikis() if row["name"] == "Inventory Wiki")
    assert linked["sourceKind"] == "linked_repository"
    # Approval auto-attaches the new wiki to the proposing agent, so its wiki
    # tools can use the link it asked for without a manual editor round-trip.
    proposer = agent_store.get_default_agent()
    assert linked["id"] in proposer["wikis"]
    assert result["result"]["attachedToAgent"] == proposer["id"]


def test_link_approval_attaches_wiki_to_nondefault_proposer(linked_repo):
    """create_linked_wiki auto-attaches only the seeded Assistant; a proposal
    from another registrar-capable agent must attach the proposer too."""
    import asyncio as _asyncio

    duty = agent_store.create_agent({"name": "DutyBot", "tools": ["wiki_registrar"]})
    registrar = wiki.make_wiki_registrar_tools(duty)
    proposed = json.loads(_call(
        registrar["wiki_link_propose"], repo_path=str(linked_repo), wiki_root="docs", name="Duty Wiki"))
    result = _asyncio.run(wiki_store.approve_proposal(proposed["proposalId"], {}))
    assert result["status"] == "executed"
    wiki_id = result["result"]["wikiId"]
    assert result["result"]["attachedToAgent"] == duty["id"]
    assert wiki_id in agent_store.get_agent(duty["id"])["wikis"]
    # The seeded Assistant keeps its product-default attachment as well.
    assert wiki_id in agent_store.get_default_agent()["wikis"]


def test_operation_shell_safe_and_gates_only_assistant_ops(linked_repo):
    w = wiki_store.create_linked_wiki("Sample", str(linked_repo))
    # A manual (explicit_user) op runs on a direct request without a separate
    # trust grant — invoking it *is* the approval.
    assert wiki_store.run_linked_operation(w["id"], "update")["exitCode"] == 0
    # An assistant-mode op the agent could run unattended still needs an explicit trust.
    with pytest.raises(wiki_store.WikiError, match="not trusted"):
        wiki_store.run_linked_operation(w["id"], "search", {"query": "retry"})
    wiki_store.set_trusted_operations(w["id"], ["search"])
    # The only variable goes into its own argv element; shell metacharacters are data.
    result = wiki_store.run_linked_operation(w["id"], "search", {"query": "x; echo hacked"})
    assert result["output"] == "native result: x; echo hacked\n"


# --- instruction pair (AGENTS.md + CLAUDE.md) parity + page cache + rg ------ #

def test_instruction_pair_agents_then_claude(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("root agents")
    (repo / "CLAUDE.md").write_text("root claude")
    (repo / "docs" / "AGENTS.md").write_text("wiki agents")
    (repo / "docs" / "CLAUDE.md").write_text("wiki claude")
    repo_p, root_p = linked_wiki.resolve_root(str(repo), "docs")
    paths = [item["path"] for item in linked_wiki.instructions(repo_p, root_p)]
    # AGENTS.md first within each scope; repo scope before the wiki-root scope.
    assert paths == ["AGENTS.md", "CLAUDE.md", "docs/AGENTS.md", "docs/CLAUDE.md"]


def test_claude_md_excluded_from_page_catalog(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Agents\n")
    (repo / "CLAUDE.md").write_text("# Claude\n")
    (repo / "page.md").write_text("# Page\nbody")
    _repo, root = linked_wiki.resolve_root(str(repo), ".")
    linked_wiki._invalidate_page_cache()
    rels = [linked_wiki._rel(root, p) for p in linked_wiki.page_files(root)]
    assert "page.md" in rels
    assert "CLAUDE.md" not in rels and "AGENTS.md" not in rels


def test_page_files_cache_invalidates_on_new_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.md").write_text("# A")
    _repo, root = linked_wiki.resolve_root(str(repo), ".")
    linked_wiki._invalidate_page_cache()
    first = [linked_wiki._rel(root, p) for p in linked_wiki.page_files(root)]
    assert first == ["a.md"]
    (repo / "b.md").write_text("# B")
    linked_wiki._invalidate_page_cache()   # sleep-free: drop the coarse cache
    second = {linked_wiki._rel(root, p) for p in linked_wiki.page_files(root)}
    assert second == {"a.md", "b.md"}


def test_direct_search_fallback_parity_without_rg(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "retry.md").write_text("# Retry\nexponential backoff retry policy")
    (repo / "other.md").write_text("# Other\nunrelated content")
    _repo, root = linked_wiki.resolve_root(str(repo), ".")
    linked_wiki._invalidate_page_cache()
    monkeypatch.setattr(linked_wiki, "_rg_binary", lambda: None)   # force full scan
    hits = linked_wiki.direct_search(root, "exponential", 5)
    assert hits and hits[0]["path"] == "retry.md"


# --- P3: operation kinds ------------------------------------------------------ #

def test_operation_kind_normalization(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ops = linked_wiki.normalize_operations({"operations": {
        "a": {"argv": ["/bin/echo", "x"], "mode": "assistant"},                   # default → maintenance
        "b": {"argv": ["/bin/echo", "x"], "mode": "assistant", "kind": "bogus"},  # invalid → maintenance
        "c": {"argv": ["/bin/echo", "x"], "mode": "assistant", "kind": "sync"},   # honored
    }}, repo)
    assert ops["a"]["kind"] == "maintenance"
    assert ops["b"]["kind"] == "maintenance"
    assert ops["c"]["kind"] == "sync"


def test_discover_operations_kind_from_manifest_and_instructions(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(json.dumps({"operations": {
        "provision": {"argv": ["/bin/echo", "p"], "mode": "explicit_user", "kind": "setup"}}}))
    (repo / "AGENTS.md").write_text(
        '- `./scripts/kb sync` — pull\n- `./scripts/kb recall find "<q>"` — search\n')
    r, root = linked_wiki.resolve_root(str(repo), ".")
    ops = {o["name"]: o for o in linked_wiki.discover_operations(r, root)}
    assert ops["provision"]["kind"] == "setup"        # manifest-declared kind honored
    assert ops["sync"]["kind"] == "sync"              # instruction-derived lifecycle verb
    assert "recall-find" not in ops                   # read-only doc command — not a lifecycle op, dropped


def test_manifest_kind_change_invalidates_trust(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    repo.mkdir()
    (repo / "i.md").write_text("# i\n")
    (repo / ".virtual-worker").mkdir()
    mpath = repo / ".virtual-worker" / "wiki.json"
    mpath.write_text(json.dumps({"operations": {
        "op": {"argv": ["/bin/echo", "x"], "mode": "assistant", "kind": "setup"}}}))
    w = wiki_store.create_linked_wiki("KB", str(repo))
    wiki_store.set_trusted_operations(w["id"], ["op"])
    m = json.loads(mpath.read_text())
    m["operations"]["op"]["kind"] = "sync"             # kind change ⇒ configHash change
    mpath.write_text(json.dumps(m))
    assert "op" in wiki_store.probe_linked_wiki(w["id"])["invalidatedOperations"]


# --- P3: linked_status / wiki_status surfaces --------------------------------- #

def test_linked_status_exposes_p3_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    repo.mkdir()
    (repo / "i.md").write_text("# i\n")
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(json.dumps({"operations": {
        "setup": {"argv": ["/bin/echo", "s"], "mode": "explicit_user", "kind": "setup"}}}))
    w = wiki_store.create_linked_wiki("KB", str(repo))
    st = wiki_store.linked_status(w["id"])
    assert st["trustTier"] == "repo-tooling"
    assert st["setupPending"] is True
    assert st["lastSyncAt"] is None
    assert "currentRevision" in st                     # None for a non-git repo
    tools = wiki.make_wiki_tools({"id": "a", "wikis": [w["id"]]})
    txt = asyncio.run(tools["wiki_status"]["fn"](wiki="KB"))
    assert "Setup pending" in txt and "tier repo-tooling" in txt


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_wiki_status_reports_moved_when_revision_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")

    def _git(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    repo = tmp_path / "kb"
    repo.mkdir()
    _git("init")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (repo / "i.md").write_text("# i\n")
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(json.dumps({"operations": {
        "sync": {"argv": ["/bin/echo", "s"], "mode": "explicit_user", "kind": "sync"}}}))
    _git("add", "-A")
    _git("commit", "-m", "one")
    w = wiki_store.create_linked_wiki("KB", str(repo))   # observedRevision = commit 1
    (repo / "j.md").write_text("# j\n")
    _git("add", "-A")
    _git("commit", "-m", "two")                          # HEAD moves ahead
    tools = wiki.make_wiki_tools({"id": "a", "wikis": [w["id"]]})
    txt = asyncio.run(tools["wiki_status"]["fn"](wiki="KB"))
    assert "moved since the last sync" in txt
