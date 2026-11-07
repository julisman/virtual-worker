"""Linked-wiki revamp §5–6 — persisted approval queue + the 4-tool assistant
surface. The old token-ceremony flow is gone; these test propose → approve/reject
→ execute, restart persistence, and that no tool can approve.

Hermetic: a temp checkout + temp Virtual Worker home, no real model or network.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

import config
from agent import wiki as agent_wiki
from storage import agent_store, linked_wiki, wiki_jobs, wiki_store


@pytest.fixture()
def linked_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "vw-home")
    repo = tmp_path / "kb"
    repo.mkdir()
    (repo / "intro.md").write_text("# Intro\n\nHello.\n")
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(
        json.dumps({"operations": {
            "setup": {"argv": ["/bin/echo", "setup"], "mode": "explicit_user", "kind": "setup"},
            "recall": {"argv": ["/bin/echo", "recall"], "mode": "assistant"},
        }}))
    (repo / "AGENTS.md").write_text("# KB\n\n- `./scripts/kb sync` — pull the latest manifest\n")
    return repo


def _registrar():
    return agent_wiki.make_wiki_registrar_tools({"id": agent_store.DEFAULT_AGENT_ID})


def _call(tool, **kw):
    return asyncio.run(tool["fn"](**kw))


# ─── Read-only discovery still works ──────────────────────────────────────────

def test_discover_finds_manifest_and_instruction_ops(linked_repo):
    repo, root = linked_wiki.resolve_root(str(linked_repo), ".")
    ops = {op["name"]: op for op in linked_wiki.discover_operations(repo, root)}
    assert ops["setup"]["source"] == "manifest" and ops["setup"]["mode"] == "explicit_user"
    assert ops["recall"]["mode"] == "assistant"
    assert ops["sync"]["source"] == "instruction" and ops["sync"]["mode"] == "explicit_user"


def test_discover_infers_lifecycle_ops_from_conventions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "vw-home")
    repo = tmp_path / "conv"
    (repo / "scripts").mkdir(parents=True)
    (repo / "intro.md").write_text("# Intro\n")
    # An executable setup script, a Makefile sync target, a package.json check script.
    setup = repo / "setup.sh"
    setup.write_text("#!/bin/sh\necho setup\n")
    setup.chmod(0o755)
    (repo / "Makefile").write_text("sync:\n\tgit pull\n\nfoo:\n\techo foo\n")
    (repo / "package.json").write_text(json.dumps({"scripts": {"check": "eslint .", "dev": "vite"}}))
    repo_p, root_p = linked_wiki.resolve_root(str(repo), ".")
    ops = {op["name"]: op for op in linked_wiki.discover_operations(repo_p, root_p)}
    assert ops["setup"]["source"] == "convention" and ops["setup"]["argv"] == ["./setup.sh"] and ops["setup"]["kind"] == "setup"
    assert ops["sync"]["argv"] == ["make", "sync"] and ops["sync"]["kind"] == "sync"
    assert ops["check"]["argv"] == ["npm", "run", "check"] and ops["check"]["kind"] == "maintenance"
    # Non-lifecycle targets/scripts are not surfaced.
    assert "foo" not in ops and "dev" not in ops


def test_discover_mines_lifecycle_commands_from_readme(tmp_path, monkeypatch):
    # Many repos document setup/sync in the README, not AGENTS.md/CLAUDE.md, and
    # invoke them through a dispatcher script (./scripts/kb setup). Discovery must
    # mine the README and map lifecycle verbs to their kind; read-only query
    # commands mentioned there are not lifecycle ops and are dropped.
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "vw-home")
    repo = tmp_path / "kb"
    (repo / "scripts").mkdir(parents=True)
    (repo / "intro.md").write_text("# Intro\n")
    (repo / "AGENTS.md").write_text("Use `./scripts/kb query \"<q>\"` to search.\n")
    (repo / "README.md").write_text(
        "## Getting started\n"
        "./scripts/kb setup       # one-time dev setup\n"
        "./scripts/kb sync        # follow the pins\n"
        "./scripts/kb bootstrap   # first run\n"
        "./scripts/kb update      # pull the diff\n")
    repo_p, root_p = linked_wiki.resolve_root(str(repo), ".")
    ops = {op["name"]: op for op in linked_wiki.discover_operations(repo_p, root_p)}
    assert ops["setup"]["kind"] == "setup" and ops["setup"]["argv"] == ["./scripts/kb", "setup"]
    assert ops["sync"]["kind"] == "sync"
    assert ops["bootstrap"]["kind"] == "setup"    # bootstrap → setup kind
    assert ops["update"]["kind"] == "sync"        # update → sync kind
    assert "query" not in ops                     # read-only command — not a lifecycle op, dropped


def test_discover_prefers_manifest_over_convention(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "vw-home")
    repo = tmp_path / "conv2"
    repo.mkdir()
    (repo / "intro.md").write_text("# Intro\n")
    (repo / "setup.sh").write_text("#!/bin/sh\necho no\n")
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(
        json.dumps({"operations": {"setup": {"argv": ["/bin/echo", "manifest"], "mode": "explicit_user", "kind": "setup"}}}))
    repo_p, root_p = linked_wiki.resolve_root(str(repo), ".")
    ops = {op["name"]: op for op in linked_wiki.discover_operations(repo_p, root_p)}
    assert ops["setup"]["source"] == "manifest" and ops["setup"]["argv"] == ["/bin/echo", "manifest"]


def test_link_propose_merges_assistant_suggested_operations(linked_repo):
    # The repo declares setup+recall (manifest) and mentions sync (instruction).
    # The assistant suggests a `check` command it read from the README; a `setup`
    # suggestion is ignored because the manifest already declares one.
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo), name="KB",
                           operations={
                               "check": {"argv": ["make", "check"], "mode": "explicit_user", "kind": "maintenance"},
                               "setup": {"argv": ["/bin/echo", "override"], "mode": "explicit_user", "kind": "setup"},
                           }))
    p = wiki_store.get_proposal(out["proposalId"])
    ops = {op["name"]: op for op in p["payload"]["operations"]}
    assert ops["check"]["source"] == "suggested" and ops["check"]["argv"] == ["make", "check"]
    # The manifest's setup wins over the assistant's suggestion.
    assert ops["setup"]["source"] == "manifest" and ops["setup"]["argv"] == ["/bin/echo", "setup"]


def test_link_propose_registers_only_lifecycle_ops(tmp_path, monkeypatch):
    # Read-only kb subcommands mentioned in docs must NOT clutter the proposal —
    # the agent runs those directly under repo tooling. Only setup/sync/check-style
    # ops (plus anything the manifest declared) are offered to register.
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "vw-home")
    repo = tmp_path / "kb"
    repo.mkdir()
    (repo / "intro.md").write_text("# Intro\n")
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(
        json.dumps({"operations": {"provision": {"argv": ["/bin/echo", "p"], "mode": "assistant", "kind": "custom"}}}))
    (repo / "AGENTS.md").write_text(
        "- `./scripts/kb setup` — bootstrap\n"
        "- `./scripts/kb sync` — pull the latest\n"
        "- `./scripts/kb chain \"<q>\"` — read-only explore\n"
        "- `./scripts/kb recall find \"<q>\"` — read-only search\n")
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(repo), name="KB"))
    names = {op["name"] for op in wiki_store.get_proposal(out["proposalId"])["payload"]["operations"]}
    assert {"setup", "sync"} <= names           # lifecycle ops kept
    assert "provision" in names                 # manifest-declared op kept (author intent)
    assert "chain" not in names and "recall-find" not in names   # read-only docs noise dropped


def test_link_propose_rejects_invalid_suggested_operation(linked_repo):
    out = _call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo),
                operations={"bad": {"argv": [], "mode": "explicit_user"}})
    assert "refused" in out and "invalid" in out


# ─── Propose files a persisted awaiting_approval row ──────────────────────────

def test_link_propose_creates_awaiting_proposal(linked_repo):
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo), name="KB"))
    assert out["kind"] == "link" and out["proposalId"]
    p = wiki_store.get_proposal(out["proposalId"])
    assert p["status"] == "awaiting_approval" and p["wikiId"] is None
    assert p["payload"]["name"] == "KB"
    assert {op["name"] for op in p["payload"]["operations"]} >= {"setup", "recall", "sync"}


def test_link_propose_defaults_to_repo_tooling_tier(linked_repo):
    # No trust_tier arg → the filed proposal requests the new repo-tooling default.
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo), name="KB"))
    p = wiki_store.get_proposal(out["proposalId"])
    assert p["payload"]["requestedTrustTier"] == "repo-tooling"


def test_proposal_survives_a_restart(linked_repo):
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo), name="KB"))
    pid = out["proposalId"]
    # "Restart": re-import the store module; the row lives in the tmp APP_HOME db.
    importlib.reload(wiki_store)
    assert any(p["id"] == pid for p in wiki_store.list_proposals("pending"))
    assert wiki_store.get_proposal(pid)["status"] == "awaiting_approval"


# ─── Approve executes the link (checkedOps + trust-after-upsert + tier) ───────

def test_approve_link_creates_wiki_trusts_and_sets_tier(linked_repo):
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo),
                           name="KB", trust_tier="repo-tooling"))
    pid = out["proposalId"]
    # Approve only the assistant-mode op (recall) + setup; drop sync.
    result = asyncio.run(wiki_store.approve_proposal(pid, {"checkedOps": ["recall", "setup"]}))
    assert result["status"] == "executed"
    wiki_id = result["wikiId"]
    assert wiki_id and wiki_id in agent_store.get_default_agent()["wikis"]
    assert wiki_store.get_trust_tier(wiki_id) == "repo-tooling"
    ops = {op["name"]: op for op in wiki_store.list_linked_operations(wiki_id)}
    assert "recall" in ops and "setup" in ops and "sync" not in ops
    assert ops["recall"]["trusted"] is True          # assistant-mode trusted AFTER upsert
    assert ops["setup"]["trusted"] is True            # manual (explicit_user) needs no separate grant
    assert ops["setup"]["kind"] == "setup"            # kind survives the link upsert (not reset to custom)
    assert result["result"]["setupOperation"] == "setup"


def test_approve_on_resolved_proposal_is_rejected_by_store(linked_repo):
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo), name="KB"))
    pid = out["proposalId"]
    asyncio.run(wiki_store.approve_proposal(pid, {}))
    with pytest.raises(wiki_store.WikiError, match="not awaiting approval"):
        asyncio.run(wiki_store.approve_proposal(pid, {}))


# ─── Reject stores a note that wiki_proposal_status relays ────────────────────

def test_reject_stores_note_and_status_relays_it(linked_repo):
    out = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo), name="KB"))
    pid = out["proposalId"]
    wiki_store.reject_proposal(pid, "not this repo")
    status = _call(_registrar()["wiki_proposal_status"], proposal_id=pid)
    assert "rejected" in status and "not this repo" in status
    with pytest.raises(wiki_store.WikiError, match="already resolved"):
        wiki_store.reject_proposal(pid, "again")


# ─── No approve-capable tool in the registrar ─────────────────────────────────

def test_registrar_has_only_propose_tools(linked_repo):
    reg = _registrar()
    assert set(reg) == {"wiki_link_probe", "wiki_link_propose", "wiki_ops_propose", "wiki_proposal_status"}
    # None of the propose tools can approve/execute — no approve fn is exposed.
    assert not any("approve" in name for name in reg)


# ─── Operations proposal + one-off run reachable via wiki_proposal_status ─────

def test_ops_propose_runonce_launches_adhoc_and_status_reads_output(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))

    async def scenario():
        out = json.loads(await _registrar()["wiki_ops_propose"]["fn"](
            wiki=w["id"], run_once=[{"argv": ["/bin/echo", "one-off-output"], "reason": "list"}]))
        pid = out["proposalId"]
        result = await wiki_store.approve_proposal(pid, {})
        job_ids = result["result"]["jobIds"]
        # let the adhoc job finish
        for jid in job_ids:
            job = wiki_jobs._get(jid)
            if job:
                await asyncio.wait_for(job._done.wait(), timeout=10)
        return await _registrar()["wiki_proposal_status"]["fn"](proposal_id=pid)

    status = asyncio.run(scenario())
    assert "executed" in status and "one-off-output" in status


def test_ops_propose_pure_tier_change_is_trust_tier_proposal(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    out = json.loads(_call(_registrar()["wiki_ops_propose"], wiki=w["id"], trust_tier="repo-tooling"))
    assert out["kind"] == "trust_tier"
    result = asyncio.run(wiki_store.approve_proposal(out["proposalId"], {}))
    assert result["status"] == "executed"
    assert wiki_store.get_trust_tier(w["id"]) == "repo-tooling"


# ─── Route layer: /proposals not swallowed by /{wiki_id}; discovery gone ──────

def test_routes_proposals_and_discovery_absent(linked_repo):
    from fastapi.testclient import TestClient

    import app

    client = TestClient(app.app)
    # File a link proposal, then read it back through the top-level route — this
    # only works if /proposals is registered before the /{wiki_id} catch-all.
    proposed = json.loads(_call(_registrar()["wiki_link_propose"], repo_path=str(linked_repo), name="KB"))
    pid = proposed["proposalId"]

    listed = client.get("/api/wiki/proposals?status=pending")
    assert listed.status_code == 200
    assert any(p["id"] == pid for p in listed.json()["proposals"])

    one = client.get(f"/api/wiki/proposals/{pid}")
    assert one.status_code == 200 and one.json()["kind"] == "link"

    # Approve via the route creates the wiki.
    approved = client.post(f"/api/wiki/proposals/{pid}/approve", json={})
    assert approved.status_code == 200 and approved.json()["status"] == "executed"
    # Re-approving a resolved proposal → 409.
    assert client.post(f"/api/wiki/proposals/{pid}/approve", json={}).status_code == 409

    # The old discovery endpoint is gone — no route serves its JSON API shape
    # anymore. What the fall-through returns depends on the environment (the
    # SPA catch-all serves HTML when frontend/dist exists; plain FastAPI 404
    # JSON otherwise), so assert the invariant itself: never a 200 with the
    # old {"pending": [...]} payload.
    wiki_id = approved.json()["wikiId"]
    gone = client.get(f"/api/wiki/{wiki_id}/discovery/pending")
    if gone.status_code == 200 and "application/json" in gone.headers.get("content-type", ""):
        assert "pending" not in gone.json()
