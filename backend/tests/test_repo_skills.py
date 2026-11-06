"""Repo-local skills (linked-wiki revamp §7): a linked repo's .claude/skills /
.vw/skills surface through load_skill, namespaced and never copied. Hermetic."""

from __future__ import annotations

import asyncio

import pytest

import config
from agent import skills as agent_skills
from storage import wiki_store


@pytest.fixture()
def linked_with_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "ves-home")
    repo = tmp_path / "kb"
    skill_dir = repo / ".claude" / "skills" / "foo"
    (skill_dir / "references").mkdir(parents=True)
    (repo / "intro.md").write_text("# i\n")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Foo Skill\ndescription: does foo\n---\n\nStep 1. Do foo.\n")
    (skill_dir / "references" / "note.md").write_text("ref body here")
    w = wiki_store.create_linked_wiki("kb", str(repo))
    return w, repo


def _call(tool, **kw):
    return asyncio.run(tool["fn"](**kw))


def test_repo_skill_in_catalog_and_loadable(linked_with_skill):
    w, _repo = linked_with_skill
    agent = {"id": "a", "wikis": [w["id"]]}
    wiki_skills = agent_skills.collect_wiki_skills(agent)
    assert "wiki:kb:foo" in wiki_skills

    ctx = agent_skills.build_skill_context([], wiki_skills=wiki_skills)
    assert "wiki:kb:foo" in ctx and "from linked repo kb" in ctx

    load = agent_skills.make_load_skill_tool([], wiki_skills=wiki_skills)
    assert "Do foo" in _call(load, slug="wiki:kb:foo")
    assert "Do foo" in _call(load, slug="foo")           # bare slug, unambiguous

    res = agent_skills.make_load_skill_resource_tool([], wiki_skills=wiki_skills)
    assert "ref body here" in _call(res, slug="wiki:kb:foo", path="references/note.md")
    blocked = _call(res, slug="wiki:kb:foo", path="../../../../etc/passwd")
    assert "not found" in blocked or "not allowed" in blocked


def test_repo_skill_is_never_copied_into_ves_home(linked_with_skill):
    w, _repo = linked_with_skill
    agent_skills.collect_wiki_skills({"id": "a", "wikis": [w["id"]]})
    # Nothing was copied into the Virtual Worker skill library.
    assert not (config.APP_HOME / "skills" / "foo").exists()


def test_compose_grants_load_skill_for_repo_skill(linked_with_skill):
    from agent.compose import compose

    w, _repo = linked_with_skill
    system, registry, *_ = compose({"id": "a", "wikis": [w["id"]]})
    assert "load_skill" in registry
    assert "wiki:kb:foo" in system
    body = _call(registry["load_skill"], slug="wiki:kb:foo")
    assert "Do foo" in body
