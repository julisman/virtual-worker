---
name: Skill Creator
description: Author new installed skills (agentskills.io SKILL.md packs) that follow the spec — dedupe against the library, write a tight step-by-step body, and persist with create_skill/update_skill.
when_to_use: When the user asks you to make, save, create, or improve a reusable skill, or when you've worked out a repeatable procedure worth capturing as a skill. Requires the agent's Skill authoring capability (create_skill tool).
version: 1.0.0
---

# Skill Creator

This is a **meta-skill**: it teaches you to write *other* skills. Use it whenever
you're asked to create or refine an installed skill, or when you notice a
repeatable procedure worth saving. You persist skills with the `create_skill` /
`update_skill` tools (available because this agent has **Skill authoring** on).

## What a skill is

A skill is a reusable procedural-knowledge pack in the [agentskills.io](https://agentskills.io)
`SKILL.md` format: YAML frontmatter (`name` / `description` / `when_to_use` /
`version`) plus a markdown **body** with the full instructions. Skills load with
progressive disclosure — only the name/description reach the prompt until an agent
calls `load_skill`, so a good skill has a crisp `description`/`when_to_use` and a
self-contained body.

The exact format is in `references/skill-spec.md`; a complete worked example is in
`references/example-skill.md`. **Load them when unsure** —
`load_skill_resource({"slug": "skill-creator", "path": "references/skill-spec.md"})`.

## Workflow

1. **Dedupe first.** Call `list_all_skills`. If a skill already covers the request,
   refine it with `update_skill` instead of creating a near-duplicate.
2. **Clarify the trigger.** Decide the one-line `description` (what it does) and the
   `when_to_use` (the condition that should make an agent reach for it). These are
   what other agents see before loading — make them specific.
3. **Write the body.** A tight, **imperative, step-by-step** procedure. Prefer
   numbered steps or short sections. State inputs, the exact steps, and the output
   format. Don't pad — the body is instructions, not prose. See the example.
4. **Persist.** Call `create_skill` with `name`, `description`, `when_to_use`, and
   `body` (and an optional `version`, default `1.0.0`). To change an existing skill,
   call `update_skill` with its `slug` and only the fields that change.
5. **Confirm.** Report the slug you created/updated and one line on what it does. A
   new skill lands in the shared library; the operator attaches it to the agents
   that should have it.

## Rules

- **Don't overwrite blindly.** `create_skill` refuses an existing slug by default —
  that's a signal to `update_skill` instead. Only pass `overwrite` if the user
  explicitly wants to replace a skill.
- **One skill = one job.** If a request spans two unrelated procedures, make two
  skills.
- **Match the house style.** Study an existing skill (`load_skill`) before writing
  a new one so tone and structure are consistent.
- **Keep it spec-compliant.** When in doubt about a field or structure, load
  `references/skill-spec.md` rather than guessing.
