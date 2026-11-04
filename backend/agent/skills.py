"""Skill context for agents — progressive disclosure, like the openAsetin engine.

Only each attached skill's *name / description / when_to_use* go into the system
prompt (the catalog). The full SKILL.md body is pulled on demand when the model
calls the ``load_skill`` tool. This keeps the prompt small while giving the model
access to detailed procedures only when relevant.
"""

from __future__ import annotations

from storage import skill_store


def build_skill_context(slugs: list[str]) -> str:
    """The L1 system-prompt catalog for the attached skills (empty if none).

    Only each skill's name/slug/description (+ when_to_use) are shown — the full
    body (L2) and bundled resources (L3) load on demand via tools."""
    if not slugs:
        return ""
    lines = ["## Skills",
             "You have access to the following installed skills (procedural knowledge "
             "packs). Only the title + description are loaded here — when the user's "
             "request matches a skill's description or *when* condition, you MUST FIRST "
             "call the `load_skill` tool with that skill's slug to read the full "
             "instructions, then follow them exactly. Do not guess a skill's contents.",
             ""]
    have = False
    for slug in slugs:
        skill = skill_store.get_skill(slug)
        if not skill:
            continue
        have = True
        desc = skill.get("description") or ""
        when = skill.get("whenToUse") or ""
        entry = f"- **{skill['name']}** (`{slug}`) — {desc}".rstrip()
        if when:
            entry += f" _(when: {when})_"
        lines.append(entry)
    if not have:
        return ""
    lines.append("")
    lines.append('Usage: `load_skill({"slug": "<slug>"})` returns the full instructions. '
                 '`load_skill_resource({"slug": "<slug>", "path": "references/foo.md"})` '
                 "returns a bundled resource file the skill lists.")
    return "\n".join(lines)


def _resolve_slug(name: str, slugs: list[str]) -> str | None:
    """Match a load_skill argument against attached skills, by slug or name."""
    if name in slugs:
        return name
    low = (name or "").strip().lower()
    for slug in slugs:
        if slug.lower() == low:
            return slug
        skill = skill_store.get_skill(slug)
        if skill and skill.get("name", "").lower() == low:
            return slug
    return None


def make_load_skill_tool(slugs: list[str]) -> dict:
    """L2: a ``load_skill`` tool (schema + fn) scoped to the attached skills.

    Returns the full SKILL.md body plus a listing of any L3 resources the skill
    bundles (so the model knows what it can pull via ``load_skill_resource``)."""
    async def load_skill(slug: str = "", name: str = "") -> str:
        resolved = _resolve_slug(slug or name, slugs)
        if not resolved:
            avail = ", ".join(slugs) or "(none)"
            return f"unknown skill: {(slug or name)!r}. Available: {avail}"
        skill = skill_store.get_skill(resolved)
        body = (skill or {}).get("body", "").strip()
        if not body:
            return f"skill {resolved!r} has no content"
        out = [
            f"# Skill: {skill['name']} ({resolved}, v{skill.get('version', '1.0.0')})",
            f"_{skill.get('description', '')}_",
            "",
            body,
        ]
        resources = skill_store.list_resources(resolved)
        if resources:
            out += ["", "## Resources",
                    "This skill bundles files you can load with `load_skill_resource`:"]
            out += [f"- `{r['path']}` ({r['size']} bytes)" for r in resources]
        return "\n".join(out)

    return {
        "fn": load_skill,
        "schema": {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load the full instructions (L2) of one of your installed "
                               "skills when it becomes relevant. Returns the complete "
                               "SKILL.md body — read it, then follow it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "the skill slug (or name) to load"},
                    },
                    "required": ["slug"],
                },
            },
        },
    }


def make_load_skill_resource_tool(slugs: list[str]) -> dict:
    """L3: a ``load_skill_resource`` tool to read a bundled file of an attached
    skill (after ``load_skill`` has shown the resource path is available)."""
    async def load_skill_resource(slug: str = "", path: str = "") -> str:
        resolved = _resolve_slug(slug, slugs)
        if not resolved:
            avail = ", ".join(slugs) or "(none)"
            return f"unknown skill: {slug!r}. Available: {avail}"
        if not path:
            return "load_skill_resource needs a 'path' (e.g. references/foo.md)"
        content = skill_store.read_resource(resolved, path)
        if content is None:
            return f"resource not found (or not allowed): {path!r} in skill {resolved!r}"
        return f"# {resolved}/{path}\n\n{content}"

    return {
        "fn": load_skill_resource,
        "schema": {
            "type": "function",
            "function": {
                "name": "load_skill_resource",
                "description": "Load an L3 resource file (reference/asset/script) bundled "
                               "with one of your skills. Use only after load_skill listed "
                               "the resource path. Path-traversal is rejected.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "the skill slug owning the resource"},
                        "path": {"type": "string", "description": "resource path, e.g. references/style.md"},
                    },
                    "required": ["slug", "path"],
                },
            },
        },
    }
