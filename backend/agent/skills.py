"""Skill context for agents — progressive disclosure, like the openAsetin engine.

Only each attached skill's *name / description / when_to_use* go into the system
prompt (the catalog). The full SKILL.md body is pulled on demand when the model
calls the ``load_skill`` tool. This keeps the prompt small while giving the model
access to detailed procedures only when relevant.
"""

from __future__ import annotations

from storage import skill_store


def collect_wiki_skills(agent: dict | None) -> dict[str, dict]:
    """Repo-local skills shipped by each attached *linked* wiki, from
    ``<repo>/.claude/skills`` and ``<repo>/.virtual-worker/skills`` (linked-wiki revamp §7).

    Keyed ``wiki:<wiki-name>:<skill-slug>`` → ``{meta, root, wikiName, slug}``.
    Surfaced through the normal ``load_skill`` catalog — never copied, so they
    update with ``git pull``."""
    from storage import wiki_store

    out: dict[str, dict] = {}
    for wid in [w for w in ((agent or {}).get("wikis") or []) if w]:
        if not wiki_store.is_linked(wid):
            continue
        wiki = wiki_store.get_wiki(wid) or {}
        name = wiki.get("name") or wid
        try:
            repo, _root = wiki_store.linked_paths(wid)
        except wiki_store.WikiError:
            continue
        for sub in (".claude/skills", ".vw/skills"):
            base = repo / sub
            for meta in skill_store.list_skills_in(base):
                key = f"wiki:{name}:{meta['slug']}"
                out.setdefault(key, {"meta": meta, "root": base, "wikiName": name, "slug": meta["slug"]})
    return out


def _resolve_wiki_skill(name: str, wiki_skills: dict[str, dict] | None) -> str | None:
    """Resolve a load_skill argument to a namespaced repo-skill key: an exact key,
    else a bare slug/name if it is unambiguous across the repo skills."""
    if not wiki_skills:
        return None
    arg = (name or "").strip()
    if arg in wiki_skills:
        return arg
    low = arg.lower()
    matches = [k for k, v in wiki_skills.items()
               if v["slug"].lower() == low or v["meta"].get("name", "").lower() == low]
    return matches[0] if len(matches) == 1 else None


def build_skill_context(slugs: list[str], wiki_skills: dict[str, dict] | None = None) -> str:
    """The L1 system-prompt catalog for the attached skills (empty if none).

    Only each skill's name/slug/description (+ when_to_use) are shown — the full
    body (L2) and bundled resources (L3) load on demand via tools. Repo-local
    skills from linked wikis are listed too, tagged with their source repo."""
    wiki_skills = wiki_skills or {}
    if not slugs and not wiki_skills:
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
    for key, info in wiki_skills.items():
        meta = info["meta"]
        have = True
        desc = meta.get("description") or ""
        when = meta.get("whenToUse") or ""
        entry = f"- **{meta['name']}** (`{key}`) — {desc}".rstrip()
        if when:
            entry += f" _(when: {when})_"
        entry += f" (from linked repo {info['wikiName']})"
        lines.append(entry)
    if not have:
        return ""
    lines.append("")
    lines.append('Usage: `load_skill({"slug": "<slug>"})` returns the full instructions. '
                 '`load_skill_resource({"slug": "<slug>", "path": "references/foo.md"})` '
                 "returns a bundled resource file the skill lists.")
    return "\n".join(lines)


def build_connector_native_note(slugs: list[str], connector_names: list[str],
                                connector_tool_names: list[str]) -> str:
    """A system-prompt note for dual-mode skills (docs/notes/log-skill-dual-mode-plan.md).

    A skill that opts in with ``connector_native: true`` frontmatter ships CLI
    (`an external CLI`) instructions for external agents; when one of its
    ``requires_connectors`` presets is attached to *this* agent, the in-process
    connector tools are the right path. Compose has ground truth (the attached
    connectors and the exact tool names built from them), so it tells the model
    outright instead of making it self-detect. Connectors are user-named
    (``dms-outbound``), so presets are resolved via the connector store — a raw
    name∩preset intersection would never match. Goes into the ``system`` string,
    not the skill body, so it applies however ``load_skill`` returns the body
    mid-run. Empty string when nothing matches."""
    if not slugs or not connector_names or not connector_tool_names:
        return ""
    from connectors import connector_identity
    from connectors import store as connector_store

    names_by_preset: dict[str, list[str]] = {}
    identities: dict[str, dict] = {}
    for cname in connector_names:
        rec = connector_store.get_connector(cname)
        if not rec or not rec.get("enabled"):
            continue
        names_by_preset.setdefault(rec.get("preset") or "", []).append(cname)
        identities[cname] = connector_identity(rec)
    if not names_by_preset:
        return ""

    lines: list[str] = []
    for slug in slugs:
        skill = skill_store.get_skill(slug)
        if not skill or not skill.get("connectorNative"):
            continue
        matched = [n for preset in (skill.get("requiresConnectors") or [])
                   for n in names_by_preset.get(preset, [])]
        if not matched:
            continue
        tools = sorted(t for t in connector_tool_names
                       if any(t.startswith(f"{n}_") for n in matched))
        if not tools:
            continue
        # Derived identity facts (which org/group/envs each connector reaches)
        # so the model routes between same-preset connectors without guessing
        # from the name.
        idents = "; ".join(
            f"`{n}` = " + " · ".join(f"{k}: {v}" for k, v in identities[n].items())
            for n in matched if identities.get(n))
        lines.append(f"- `{slug}`: call " + ", ".join(f"`{t}`" for t in tools)
                     + (f" ({idents})" if idents else "")
                     + ' — mechanics: `load_skill_resource({"slug": "' + slug
                     + '", "path": "references/invoke-tool.md"})`.')
    if not lines:
        return ""
    return ("## Connector-native skills\n"
            "These skills' connectors are attached to you as in-process tools. "
            "Call the tools below directly; ignore any an external CLI / CLI "
            "instructions in the skill body — never shell out to the CLI:\n"
            + "\n".join(lines))


def build_preloaded_skills(slugs: list[str], attached: list[str]) -> str:
    """Full L2 bodies for skills the user force-loaded via the chat `/` picker.

    Unlike the catalog (L1 titles only), this injects the complete SKILL.md body
    so the model follows the skill *this turn* without having to call
    ``load_skill`` first. Scoped to ``attached`` (the agent's own skills) so a
    request can never preload a skill the agent isn't allowed to use."""
    if not slugs:
        return ""
    out: list[str] = []
    for slug in slugs:
        resolved = _resolve_slug(slug, attached)
        if not resolved:
            continue
        skill = skill_store.get_skill(resolved)
        body = (skill or {}).get("body", "").strip()
        if not body:
            continue
        section = [f"### Skill: {skill['name']} (`{resolved}`)", body]
        resources = skill_store.list_resources(resolved)
        if resources:
            section += ["", "Bundled resources (load on demand with `load_skill_resource`):"]
            section += [f"- `{r['path']}`" for r in resources]
        out.append("\n".join(section))
    if not out:
        return ""
    header = ("## Active skill(s) — preloaded by the user\n"
              "The user invoked the following skill(s) with `/`. Their full instructions "
              "are included below — follow them for this turn; you do NOT need to call "
              "`load_skill` for them. Load any bundled resource a skill needs with "
              "`load_skill_resource`.")
    return header + "\n\n" + "\n\n---\n\n".join(out)


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


def make_load_skill_tool(slugs: list[str], wiki_skills: dict[str, dict] | None = None) -> dict:
    """L2: a ``load_skill`` tool (schema + fn) scoped to the attached skills.

    Returns the full SKILL.md body plus a listing of any L3 resources the skill
    bundles (so the model knows what it can pull via ``load_skill_resource``).
    ``wiki_skills`` adds read-only repo-local skills (loaded in place, not copied)."""
    wiki_skills = wiki_skills or {}

    async def load_skill(slug: str = "", name: str = "") -> str:
        arg = slug or name
        resolved = _resolve_slug(arg, slugs)
        if resolved:
            skill = skill_store.get_skill(resolved)
            body = (skill or {}).get("body", "").strip()
            if not body:
                return f"skill {resolved!r} has no content"
            out = [f"# Skill: {skill['name']} ({resolved}, v{skill.get('version', '1.0.0')})",
                   f"_{skill.get('description', '')}_", "", body]
            resources = skill_store.list_resources(resolved)
            if resources:
                out += ["", "## Resources",
                        "This skill bundles files you can load with `load_skill_resource`:"]
                out += [f"- `{r['path']}` ({r['size']} bytes)" for r in resources]
            return "\n".join(out)
        wkey = _resolve_wiki_skill(arg, wiki_skills)
        if wkey:
            info = wiki_skills[wkey]
            skill = skill_store.read_skill_in(info["root"], info["slug"])
            body = (skill or {}).get("body", "").strip()
            if not body:
                return f"skill {wkey!r} has no content"
            out = [f"# Skill: {skill['name']} ({wkey}, from linked repo {info['wikiName']})",
                   f"_{skill.get('description', '')}_", "", body]
            files = (skill or {}).get("files") or []
            if files:
                out += ["", "## Resources",
                        "This skill bundles files you can load with `load_skill_resource`:"]
                out += [f"- `{f}`" for f in files]
            return "\n".join(out)
        avail = ", ".join(list(slugs) + list(wiki_skills)) or "(none)"
        return f"unknown skill: {arg!r}. Available: {avail}"

    return {
        "fn": load_skill,
        "parallel_safe": True,
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


def make_load_skill_resource_tool(slugs: list[str], wiki_skills: dict[str, dict] | None = None) -> dict:
    """L3: a ``load_skill_resource`` tool to read a bundled file of an attached
    skill (after ``load_skill`` has shown the resource path is available)."""
    wiki_skills = wiki_skills or {}

    async def load_skill_resource(slug: str = "", path: str = "") -> str:
        resolved = _resolve_slug(slug, slugs)
        wkey = None if resolved else _resolve_wiki_skill(slug, wiki_skills)
        if not resolved and not wkey:
            avail = ", ".join(list(slugs) + list(wiki_skills)) or "(none)"
            return f"unknown skill: {slug!r}. Available: {avail}"
        if not path:
            return "load_skill_resource needs a 'path' (e.g. references/foo.md)"
        if resolved:
            content = skill_store.read_resource(resolved, path)
            label = resolved
        else:
            info = wiki_skills[wkey]
            content = skill_store.read_resource_in(info["root"], info["slug"], path)
            label = wkey
        if content is None:
            if resolved:
                resources = skill_store.list_resources(resolved)
                avail = ", ".join(r["path"] for r in resources) or "(none)"
            else:
                info = wiki_skills[wkey]
                skill = skill_store.read_skill_in(info["root"], info["slug"])
                avail = ", ".join((skill or {}).get("files") or []) or "(none)"
            return (f"resource not found (or not allowed): {path!r} in skill {label!r}. "
                    f"This skill's actual resources: {avail} — load one of those, "
                    f"do not guess other paths.")
        return f"# {label}/{path}\n\n{content}"

    return {
        "fn": load_skill_resource,
        "parallel_safe": True,
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
