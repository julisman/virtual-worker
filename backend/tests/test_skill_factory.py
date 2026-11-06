"""Skill factory (PRD-14) — programmatic writer, authoring tools, compose gate."""

import asyncio

import pytest

import config
import storage.skill_store as ss
from agent.compose import compose
from agent.skill_factory import build_skill_factory_context, make_skill_factory_tools
from storage import agent_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# skill_store.write_skill — the programmatic writer
# --------------------------------------------------------------------------- #

def test_write_skill_roundtrips(home):
    skill = ss.write_skill(
        "Order Lookup", "Look up the order, then report its status.",
        description="Find an order by id.", when_to_use="When asked about an order.",
        version="2.1.0")
    assert skill["slug"] == "order-lookup"
    # Re-read from disk: frontmatter parses back cleanly.
    got = ss.get_skill("order-lookup")
    assert got["name"] == "Order Lookup"
    assert got["description"] == "Find an order by id."
    assert got["whenToUse"] == "When asked about an order."
    assert got["version"] == "2.1.0"
    assert "report its status" in got["body"]


def test_write_skill_refuses_overwrite_then_allows(home):
    ss.write_skill("Dup", "First body.")
    with pytest.raises(ValueError, match="already exists"):
        ss.write_skill("Dup", "Second body.")
    # explicit overwrite replaces it
    ss.write_skill("Dup", "Second body.", overwrite=True)
    assert "Second body." in ss.get_skill("dup")["body"]


def test_write_skill_validates(home):
    with pytest.raises(ValueError, match="name is required"):
        ss.write_skill("", "body")
    with pytest.raises(ValueError, match="body is required"):
        ss.write_skill("Name", "   ")
    with pytest.raises(ValueError, match="too long"):
        ss.write_skill("Big", "x" * (ss.MAX_SKILL_BODY + 1))


def test_write_skill_normalizes_multiline_frontmatter(home):
    # A description with newlines must still round-trip as single-line frontmatter.
    ss.write_skill("Multi", "Body.", description="line one\nline two\n\nline three")
    got = ss.get_skill("multi")
    assert got["description"] == "line one line two line three"


def test_write_skill_slug_is_confined(home):
    # A crafted name can't escape the skills dir — slugify strips path chars.
    skill = ss.write_skill("../../evil", "Body.")
    assert skill["slug"] == "evil"
    assert (config.APP_HOME / "skills" / "evil" / "SKILL.md").exists()


# --------------------------------------------------------------------------- #
# make_skill_factory_tools — the agent-facing tools
# --------------------------------------------------------------------------- #

def test_create_skill_tool_persists(home):
    tools = make_skill_factory_tools("agent-1")
    out = asyncio.run(tools["create_skill"]["fn"](
        name="Weekly Report", description="Draft the weekly report.",
        when_to_use="Every Friday.", body="1. Gather metrics. 2. Summarize."))
    assert "Created skill" in out
    assert "weekly-report" in out
    saved = ss.get_skill("weekly-report")
    assert "Gather metrics" in saved["body"]


def test_create_skill_tool_dedupe_message(home):
    tools = make_skill_factory_tools()
    asyncio.run(tools["create_skill"]["fn"](name="Thing", body="Body."))
    again = asyncio.run(tools["create_skill"]["fn"](name="Thing", body="Other."))
    assert "could not create skill" in again
    assert "update_skill" in again
    # original untouched
    assert ss.get_skill("thing")["body"].strip() == "Body."


def test_create_skill_tool_validation_is_structured(home):
    tools = make_skill_factory_tools()
    out = asyncio.run(tools["create_skill"]["fn"](name="NoBody", body=""))
    assert "could not create skill" in out  # returned, not raised


def test_update_skill_tool_merges(home):
    ss.write_skill("Runbook", "Old body.", description="Old desc.",
                   when_to_use="Old when.", version="1.0.0")
    tools = make_skill_factory_tools()
    out = asyncio.run(tools["update_skill"]["fn"](
        slug="runbook", body="New body.", version="1.1.0"))
    assert "Updated skill" in out
    got = ss.get_skill("runbook")
    assert got["body"].strip() == "New body."
    assert got["version"] == "1.1.0"
    # untouched fields preserved
    assert got["description"] == "Old desc."
    assert got["whenToUse"] == "Old when."


def test_update_skill_tool_missing(home):
    tools = make_skill_factory_tools()
    out = asyncio.run(tools["update_skill"]["fn"](slug="nope", body="x"))
    assert "no such skill" in out


def test_list_all_skills_tool(home):
    tools = make_skill_factory_tools()
    assert "empty" in asyncio.run(tools["list_all_skills"]["fn"]())
    ss.write_skill("Alpha", "A.", description="First.")
    ss.write_skill("Beta", "B.", description="Second.")
    out = asyncio.run(tools["list_all_skills"]["fn"]())
    assert "Alpha" in out and "`alpha`" in out
    assert "Beta" in out and "Second." in out


# --------------------------------------------------------------------------- #
# compose() gate — tools present iff the skillFactory guardrail is on
# --------------------------------------------------------------------------- #

def test_compose_registers_factory_when_enabled(home):
    agent = {"id": "a", "persona": "P", "guardrails": {"skillFactory": True}}
    system, registry, *_ = compose(agent)
    assert "create_skill" in registry
    assert "update_skill" in registry
    assert "list_all_skills" in registry
    assert "Skill authoring" in system


def test_compose_omits_factory_by_default(home):
    agent = {"id": "a", "persona": "P"}
    system, registry, *_ = compose(agent)
    assert "create_skill" not in registry
    assert "Skill authoring" not in (system or "")


def test_compose_omits_factory_when_flag_false(home):
    agent = {"id": "a", "persona": "P", "guardrails": {"skillFactory": False}}
    _, registry, *_ = compose(agent)
    assert "create_skill" not in registry


# --------------------------------------------------------------------------- #
# guardrail default + coercion
# --------------------------------------------------------------------------- #

def test_skillfactory_guardrail_defaults_off():
    assert agent_store.DEFAULT_GUARDRAILS["skillFactory"] is False
    assert agent_store._guardrails(None)["skillFactory"] is False


def test_skillfactory_guardrail_coerced_bool():
    import json
    g = agent_store._guardrails(json.dumps({"skillFactory": 1}))
    assert g["skillFactory"] is True


def test_build_skill_factory_context_mentions_tools():
    ctx = build_skill_factory_context()
    assert "create_skill" in ctx and "list_all_skills" in ctx
