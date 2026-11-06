"""The skill factory — meta skills that write themselves (PRD-14, ADK Pattern 4).

An opt-in agent gains tools to *author* its own installed skills at runtime,
turning the operator-curated skill library into a self-extending one. Mirrors the
other self-tool factories (``make_memory_tools``, ``make_kb_tools``, …): returns a
``{name: {"schema", "fn"}}`` registry slice that ``compose()`` merges in when the
agent's ``skillFactory`` guardrail is on.

The tools are the *write action*; the bundled ``skill-creator`` meta-skill (data,
not code) is what teaches the model to produce spec-compliant SKILL.md content —
together they form the blog's Pattern 4 (inline instructions + embedded L3 spec
resources + a persistence tool). Writes are pure local disk I/O, confined to
``<VW_HOME>/skills/`` — no network egress.
"""

from __future__ import annotations

from storage import skill_store


def build_skill_factory_context() -> str:
    """The system-prompt block for an authoring-enabled agent (kept short)."""
    return (
        "## Skill authoring\n"
        "You can create and refine your own installed skills — reusable procedural "
        "knowledge packs in the agentskills.io SKILL.md format. When the user asks you "
        "to make/save/create a skill (or you notice a repeatable procedure worth "
        "capturing), use these tools:\n"
        "- `list_all_skills` — see what already exists FIRST, so you don't duplicate one.\n"
        "- `create_skill` — persist a new skill (name, description, when_to_use, body). "
        "The body is the full instructions: a tight, imperative, step-by-step procedure.\n"
        "- `update_skill` — refine an existing skill in place (by slug) instead of "
        "creating a near-duplicate.\n"
        "If a `skill-creator` skill is installed, `load_skill` it first (and load its "
        "spec/example resources) for the exact format. A new skill goes into the shared "
        "library; the operator attaches it to whichever agents should use it."
    )


def make_skill_factory_tools(agent_id: str = "") -> dict:
    """Return the ``create_skill`` / ``update_skill`` / ``list_all_skills`` tools.

    Each ``fn`` returns a concise string result; validation/collision errors are
    caught and returned as structured messages so the agent loop never crashes."""

    async def create_skill(name: str = "", description: str = "", when_to_use: str = "",
                           body: str = "", version: str = "1.0.0",
                           overwrite: bool = False) -> str:
        try:
            skill = skill_store.write_skill(
                name, body, description=description, when_to_use=when_to_use,
                version=version or "1.0.0", overwrite=bool(overwrite))
        except ValueError as exc:
            return f"could not create skill: {exc}"
        return (f"Created skill '{skill['name']}' (slug `{skill['slug']}`, "
                f"v{skill.get('version', '1.0.0')}). It's now in your skill library; "
                "an operator can attach it to any agent.")

    async def update_skill(slug: str = "", name: str = "", description: str = "",
                           when_to_use: str = "", body: str = "", version: str = "") -> str:
        existing = skill_store.get_skill(slug) if slug else None
        if not existing:
            avail = ", ".join(s["slug"] for s in skill_store.list_skills()) or "(none)"
            return f"no such skill: {slug!r}. Existing skills: {avail}"
        # Merge provided fields over the existing skill; unspecified fields unchanged.
        merged = {
            "name": name.strip() or existing["name"],
            "description": description.strip() if description else existing.get("description", ""),
            "when_to_use": when_to_use.strip() if when_to_use else existing.get("whenToUse", ""),
            "body": body if body.strip() else existing.get("body", ""),
            "version": version.strip() or existing.get("version", "1.0.0"),
        }
        try:
            skill = skill_store.write_skill(
                merged["name"], merged["body"], description=merged["description"],
                when_to_use=merged["when_to_use"], version=merged["version"],
                slug=existing["slug"], overwrite=True)
        except ValueError as exc:
            return f"could not update skill: {exc}"
        return (f"Updated skill '{skill['name']}' (slug `{skill['slug']}`, "
                f"v{skill.get('version', '1.0.0')}).")

    async def list_all_skills() -> str:
        skills = skill_store.list_skills()
        if not skills:
            return "The skill library is empty — no skills exist yet."
        lines = ["Installed skills (the shared library):"]
        for s in skills:
            entry = f"- **{s['name']}** (`{s['slug']}`) — {s.get('description', '')}".rstrip()
            when = s.get("whenToUse") or ""
            if when:
                entry += f" _(when: {when})_"
            lines.append(entry)
        return "\n".join(lines)

    return {
        "create_skill": {
            "fn": create_skill,
            "schema": {
                "type": "function",
                "function": {
                    "name": "create_skill",
                    "description": "Author a NEW installed skill (agentskills.io SKILL.md "
                                   "format) and save it to the shared skill library. Call "
                                   "list_all_skills first to avoid duplicating an existing "
                                   "skill. Fails (without overwriting) if the slug exists — "
                                   "use update_skill to refine one instead.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "display name (the slug is derived from it)"},
                            "description": {"type": "string", "description": "one-line summary of what the skill does"},
                            "when_to_use": {"type": "string", "description": "the condition under which the skill applies"},
                            "body": {"type": "string", "description": "the full instructions (markdown): a tight, imperative, step-by-step procedure"},
                            "version": {"type": "string", "description": "semver-ish version (default 1.0.0)"},
                            "overwrite": {"type": "boolean", "description": "replace an existing skill with the same slug (default false)"},
                        },
                        "required": ["name", "body"],
                    },
                },
            },
        },
        "update_skill": {
            "fn": update_skill,
            "schema": {
                "type": "function",
                "function": {
                    "name": "update_skill",
                    "description": "Refine an EXISTING installed skill in place, by slug. "
                                   "Only the fields you pass are changed; the rest are kept. "
                                   "Use this instead of creating a near-duplicate skill.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "slug": {"type": "string", "description": "the slug of the skill to update"},
                            "name": {"type": "string", "description": "new display name (optional)"},
                            "description": {"type": "string", "description": "new one-line summary (optional)"},
                            "when_to_use": {"type": "string", "description": "new when-to-use condition (optional)"},
                            "body": {"type": "string", "description": "new full instructions body (optional)"},
                            "version": {"type": "string", "description": "new version (optional)"},
                        },
                        "required": ["slug"],
                    },
                },
            },
        },
        "list_all_skills": {
            "fn": list_all_skills,
            "schema": {
                "type": "function",
                "function": {
                    "name": "list_all_skills",
                    "description": "List every skill in the shared library (name, slug, "
                                   "description, when-to-use). Use before create_skill to "
                                   "avoid duplicates and to pick a skill to update.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        },
    }
