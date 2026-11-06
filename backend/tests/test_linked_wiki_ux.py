"""PRD-21 Linked Wiki UX — operation CRUD, async job runner, lazy listing.

Hermetic: a temp checkout + temp Virtual Worker home, no real model or network.
"""

from __future__ import annotations

import asyncio

import pytest

import config
from storage import linked_wiki, wiki_jobs, wiki_store


@pytest.fixture()
def linked_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    repo.mkdir()
    (repo / "intro.md").write_text("# Intro\n\nHello.\n")
    return repo


# ─── Operation CRUD ───────────────────────────────────────────────────────────

def test_upsert_creates_local_operation_tagged_source(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    wiki_store.upsert_local_operation(w["id"], "sync", {"argv": ["/bin/echo", "synced"], "mode": "explicit_user"})
    ops = {op["name"]: op for op in wiki_store.list_linked_operations(w["id"])}
    assert ops["sync"]["source"] == "local"
    # A manual (explicit_user) op is runnable without a separate grant — the human
    # who clicks Run is the approval.
    assert ops["sync"]["trusted"] is True


def test_editing_assistant_operation_drops_trust(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    # Trust re-approval (FR-16) applies to assistant-mode ops the agent runs unattended.
    wiki_store.upsert_local_operation(w["id"], "recall", {"argv": ["/bin/echo", "a"], "mode": "assistant"})
    wiki_store.set_trusted_operations(w["id"], ["recall"])
    assert {op["name"]: op["trusted"] for op in wiki_store.list_linked_operations(w["id"])}["recall"] is True
    # Re-authoring the operation must force re-approval.
    wiki_store.upsert_local_operation(w["id"], "recall", {"argv": ["/bin/echo", "b"], "mode": "assistant"})
    assert {op["name"]: op["trusted"] for op in wiki_store.list_linked_operations(w["id"])}["recall"] is False


def test_upsert_rejects_invalid_spec(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    with pytest.raises(wiki_store.WikiError):
        wiki_store.upsert_local_operation(w["id"], "sync", {"argv": [], "mode": "explicit_user"})
    with pytest.raises(wiki_store.WikiError, match="name"):
        wiki_store.upsert_local_operation(w["id"], "Bad Name", {"argv": ["/bin/echo"], "mode": "explicit_user"})


def test_delete_local_and_repo_operation(linked_repo):
    import json

    # A repo-defined operation cannot be deleted.
    (linked_repo / ".virtual-worker").mkdir()
    (linked_repo / ".virtual-worker" / "wiki.json").write_text(
        json.dumps({"operations": {"setup": {"argv": ["/bin/echo", "s"], "mode": "explicit_user"}}}))
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    with pytest.raises(wiki_store.WikiError, match="cannot be deleted"):
        wiki_store.delete_local_operation(w["id"], "setup")
    # A local operation deletes cleanly.
    wiki_store.upsert_local_operation(w["id"], "sync", {"argv": ["/bin/echo", "a"], "mode": "explicit_user"})
    wiki_store.delete_local_operation(w["id"], "sync")
    assert "sync" not in {op["name"] for op in wiki_store.list_linked_operations(w["id"])}


# ─── Async job runner ─────────────────────────────────────────────────────────

async def _await_terminal(job_id, timeout=10.0):
    events = []
    async for event in wiki_jobs.subscribe(job_id):
        events.append(event)
        if event.get("type") == "end":
            break
    return events


def test_job_runs_trusted_operation_to_success(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    wiki_store.upsert_local_operation(w["id"], "sync", {"argv": ["/bin/echo", "hi"], "mode": "explicit_user"})
    wiki_store.set_trusted_operations(w["id"], ["sync"])

    async def scenario():
        job = await wiki_jobs.start(w["id"], "sync", {})
        events = await _await_terminal(job.id)
        return job.id, events

    job_id, events = asyncio.run(scenario())
    end = events[-1]
    assert end["type"] == "end" and end["status"] == "succeeded" and end["exitCode"] == 0
    assert "hi" in end["output"]
    # Persisted for later inspection.
    row = wiki_store.get_job(w["id"], job_id)
    assert row["status"] == "succeeded" and "hi" in row["output"]


def test_job_rejects_untrusted_assistant_operation(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    # An assistant-mode op the agent may run unattended stays gated until trusted.
    wiki_store.upsert_local_operation(w["id"], "recall", {"argv": ["/bin/echo", "hi"], "mode": "assistant"})

    async def scenario():
        with pytest.raises(wiki_store.WikiError, match="not trusted"):
            await wiki_jobs.start(w["id"], "recall", {})

    asyncio.run(scenario())


def test_job_cancel_marks_cancelled(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    wiki_store.upsert_local_operation(w["id"], "wait", {"argv": ["sleep", "5"], "mode": "explicit_user"})
    wiki_store.set_trusted_operations(w["id"], ["wait"])

    async def scenario():
        job = await wiki_jobs.start(w["id"], "wait", {})
        await asyncio.sleep(0.2)
        snap = await wiki_jobs.cancel(job.id)
        # Let _run observe the killed process and finalize.
        await asyncio.wait_for(job._done.wait(), timeout=10)
        return snap, wiki_jobs.get(job.id)

    snap, final = asyncio.run(scenario())
    assert snap["status"] == "cancelled"
    assert final["status"] == "cancelled"


# ─── Lazy / paginated listing ─────────────────────────────────────────────────

def test_page_list_paginates(linked_repo):
    for i in range(60):
        (linked_repo / f"page-{i:03d}.md").write_text(f"# Page {i}\n\nbody\n")
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))

    first = wiki_store.list_pages_page(w["id"], "", 0, 50)
    assert len(first["items"]) == 50
    assert first["total"] == 61          # 60 pages + intro.md
    assert first["hasMore"] is True
    # Title is the filename stem (no per-file heading read).
    assert first["items"][0]["title"] == first["items"][0]["path"].rsplit(".", 1)[0]

    filtered = wiki_store.list_pages_page(w["id"], "page-00", 0, 50)
    assert filtered["total"] == 10 and all("page-00" in it["path"] for it in filtered["items"])

    # GET /{id} embeds at most 50.
    embedded = wiki_store.list_pages_page(w["id"], "", 0, 50)["items"]
    assert len(embedded) <= 50


def test_linked_list_pages_page_delegates(linked_repo):
    w = wiki_store.create_linked_wiki("KB", str(linked_repo))
    _repo, root, _row = wiki_store._linked_roots(w["id"])
    direct = linked_wiki.list_pages_page(root, "", 0, 50)
    assert direct["total"] == 1 and direct["items"][0]["path"] == "intro.md"


# ─── P3: setup/sync surfaces are stamped by KIND, not operation name ───────────

def test_setup_kind_stamps_setup_completed_and_clears_pending(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    repo.mkdir()
    (repo / "intro.md").write_text("# Intro\n")
    (repo / ".virtual-worker").mkdir()
    (repo / ".virtual-worker" / "wiki.json").write_text(json.dumps({"operations": {
        "setup": {"argv": ["/bin/echo", "s"], "mode": "explicit_user", "kind": "setup"}}}))
    w = wiki_store.create_linked_wiki("KB", str(repo))
    assert wiki_store.linked_status(w["id"])["setupPending"] is True
    wiki_store.set_trusted_operations(w["id"], ["setup"])
    wiki_store.run_linked_operation(w["id"], "setup")
    assert wiki_store.get_wiki(w["id"])["setupCompletedAt"] is not None
    assert wiki_store.linked_status(w["id"])["setupPending"] is False


def test_sync_kind_via_job_stamps_last_sync_even_with_custom_name(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    repo.mkdir()
    (repo / "intro.md").write_text("# Intro\n")
    (repo / ".virtual-worker").mkdir()
    # A CUSTOM-named op with kind=sync must still stamp last_sync_at — proving the
    # surface is kind-based, not name-based.
    (repo / ".virtual-worker" / "wiki.json").write_text(json.dumps({"operations": {
        "refresh": {"argv": ["/bin/echo", "ok"], "mode": "explicit_user", "kind": "sync"}}}))
    w = wiki_store.create_linked_wiki("KB", str(repo))
    wiki_store.set_trusted_operations(w["id"], ["refresh"])

    async def scenario():
        job = await wiki_jobs.start(w["id"], "refresh", {})
        await asyncio.wait_for(job._done.wait(), timeout=10)

    asyncio.run(scenario())
    assert wiki_store.get_wiki(w["id"])["lastSyncAt"] is not None
