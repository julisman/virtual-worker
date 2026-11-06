"""repo→wiki generation — the compose-and-drive loop (agent/repo_wiki).

Hermetic: no real model. We monkeypatch ``loop.run`` with a fake that stands in
for the agent and drives the *real* composed registry — it calls the actual
``wiki_write`` tool it was handed, which proves the fs + wiki toolkits were merged
and wired correctly, and that pages land in the OKF bundle. Stores are isolated by
pointing ``config.APP_HOME`` at a tmp dir (mirrors test_scheduler/test_wiki)."""

import asyncio

import pytest

import config
from agent import repo_wiki
from storage import wiki_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


def _fake_run_factory(captured):
    """A stand-in for loop.run: asserts the composed registry, then acts as the
    agent by calling the real wiki_write tool and emitting the loop's event dicts."""
    async def fake_run(messages, model, tools=None, **kwargs):
        captured["model"] = model
        captured["tools"] = set((tools or {}).keys())
        captured["session_id"] = kwargs.get("session_id")
        captured["max_iterations"] = kwargs.get("max_iterations")
        captured["provider"] = kwargs.get("provider")
        # The composed registry must carry BOTH toolkits.
        assert "read_file" in tools and "grep" in tools           # make_fs_tools
        assert "wiki_write" in tools and "wiki_search" in tools    # make_wiki_tools
        # Act like the agent: write two cross-linked pages using the given tools.
        r1 = await tools["wiki_write"]["fn"](
            path="overview.md",
            content="---\ntype: Overview\ntitle: Overview\n---\nSee [[modules/backend.md]].")
        r2 = await tools["wiki_write"]["fn"](
            path="modules/backend.md",
            content="---\ntype: Module\ntitle: Backend\n---\nThe API. See app.py:1.")
        yield {"type": "tool_call", "id": "c1", "name": "wiki_write", "args": {}}
        yield {"type": "tool_result", "id": "c1", "name": "wiki_write", "result": r1}
        yield {"type": "tool_call", "id": "c2", "name": "wiki_write", "args": {}}
        yield {"type": "tool_result", "id": "c2", "name": "wiki_write", "result": r2}
        yield {"type": "done", "runId": "run-xyz", "sessionId": kwargs.get("session_id")}
    return fake_run


def test_generate_writes_pages(home, tmp_path, monkeypatch):
    wiki = wiki_store.create_wiki("Repo wiki")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")

    captured: dict = {}
    monkeypatch.setattr(repo_wiki.loop, "run", _fake_run_factory(captured))

    result = _run(repo_wiki.generate_repo_wiki(str(repo), wiki["id"], model="test-model"))

    # The two pages the fake agent wrote actually landed in the OKF bundle.
    paths = {p["path"] for p in wiki_store.list_pages(wiki["id"])}
    assert paths == {"overview.md", "modules/backend.md"}
    assert result["pagesWritten"] == 2
    assert result["wikiWrites"] == 2
    assert result["toolCalls"] == 2
    assert result["runId"] == "run-xyz"
    assert result["error"] is None
    # An explicit model runs local (no external provider target).
    assert captured["model"] == "test-model"
    assert captured["provider"] is None
    assert captured["session_id"] == f"repo-wiki:{wiki['id']}"
    assert captured["max_iterations"] == repo_wiki.DEFAULT_MAX_ITERATIONS


def test_resolves_active_model_when_unset(home, tmp_path, monkeypatch):
    wiki = wiki_store.create_wiki("Repo wiki")
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_resolve(agent, requested_model=None):
        return "active-local", None
    monkeypatch.setattr(repo_wiki, "resolve_target", fake_resolve)

    captured: dict = {}
    monkeypatch.setattr(repo_wiki.loop, "run", _fake_run_factory(captured))

    result = _run(repo_wiki.generate_repo_wiki(str(repo), wiki["id"]))
    assert result["model"] == "active-local"
    assert captured["model"] == "active-local"


def test_missing_wiki_raises(home):
    with pytest.raises(ValueError, match="no such wiki"):
        _run(repo_wiki.generate_repo_wiki("/tmp", "nope", model="m"))


def test_bad_repo_path_raises(home, tmp_path):
    wiki = wiki_store.create_wiki("Repo wiki")
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="not a directory"):
        _run(repo_wiki.generate_repo_wiki(str(missing), wiki["id"], model="m"))


def test_no_active_model_raises(home, tmp_path, monkeypatch):
    wiki = wiki_store.create_wiki("Repo wiki")
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_resolve(agent, requested_model=None):
        return None, None
    monkeypatch.setattr(repo_wiki, "resolve_target", fake_resolve)

    with pytest.raises(ValueError, match="no active model"):
        _run(repo_wiki.generate_repo_wiki(str(repo), wiki["id"]))


# --------------------------------------------------------------------------- #
# The wiki_ingest_repo agent tool (make_repo_wiki_tools)
# --------------------------------------------------------------------------- #

def _local_resolve(monkeypatch):
    async def fake_resolve(agent, requested_model=None):
        return "local-model", None
    monkeypatch.setattr(repo_wiki, "resolve_target", fake_resolve)


def test_tool_creates_and_attaches_new_wiki(home, tmp_path, monkeypatch):
    from storage import agent_store
    agent = agent_store.create_agent({"name": "Docs bot"})
    repo = tmp_path / "myrepo"
    repo.mkdir()

    _local_resolve(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(repo_wiki.loop, "run", _fake_run_factory(captured))

    tools = repo_wiki.make_repo_wiki_tools(agent)
    out = _run(tools["wiki_ingest_repo"]["fn"](repo_path=str(repo)))

    # A new wiki named after the repo folder was created…
    wikis = wiki_store.list_wikis()
    assert len(wikis) == 1 and wikis[0]["name"] == "myrepo (repo)"
    assert "Created wiki" in out and "attached to you" in out
    # …its pages were written…
    assert {p["path"] for p in wiki_store.list_pages(wikis[0]["id"])} == {
        "overview.md", "modules/backend.md"}
    # …and it was auto-attached to the calling agent.
    assert wiki_store.get_wiki(wikis[0]["id"])["id"] in agent_store.get_agent(agent["id"])["wikis"]


def test_tool_reuses_repo_named_wiki_on_refresh(home, tmp_path, monkeypatch):
    """A second ingest of the same repo with no wiki name updates the same wiki
    (the weekly-refresh flow), rather than creating a duplicate."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _local_resolve(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(repo_wiki.loop, "run", _fake_run_factory(captured))

    tools = repo_wiki.make_repo_wiki_tools({"id": "a1", "wikis": []})
    first = _run(tools["wiki_ingest_repo"]["fn"](repo_path=str(repo)))
    second = _run(tools["wiki_ingest_repo"]["fn"](repo_path=str(repo)))

    assert "Created wiki" in first and "Updated wiki" in second
    assert len(wiki_store.list_wikis()) == 1  # no duplicate


def test_tool_updates_existing_wiki_by_name(home, tmp_path, monkeypatch):
    existing = wiki_store.create_wiki("Backend docs")
    repo = tmp_path / "svc"
    repo.mkdir()

    _local_resolve(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(repo_wiki.loop, "run", _fake_run_factory(captured))

    tools = repo_wiki.make_repo_wiki_tools({"id": "a1", "wikis": []})
    out = _run(tools["wiki_ingest_repo"]["fn"](repo_path=str(repo), wiki="Backend docs"))

    assert "Updated wiki" in out and "Backend docs" in out
    # Reused the same wiki (no second one created).
    assert len(wiki_store.list_wikis()) == 1
    assert wiki_store.list_pages(existing["id"])  # pages landed in it


def test_tool_bad_dir_returns_message(home, monkeypatch):
    _local_resolve(monkeypatch)
    tools = repo_wiki.make_repo_wiki_tools({"id": "a1"})
    out = _run(tools["wiki_ingest_repo"]["fn"](repo_path="/no/such/dir"))
    assert "not a directory" in out


def test_compose_exposes_wiki_ingest_repo(home, monkeypatch):
    from storage import agent_store
    from agent.compose import compose
    agent = agent_store.create_agent({"name": "Bot", "tools": []})
    _system, registry, *_ = compose(agent_store.get_agent(agent["id"]))
    assert "wiki_ingest_repo" in registry
