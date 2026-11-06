# SKILL.md format (agentskills.io)

A skill is a folder `<slug>/` containing a `SKILL.md` file (and optional resource
subfolders). `SKILL.md` is YAML-ish frontmatter + a markdown body.

## Frontmatter

Delimited by `---` lines at the very top. Simple `key: value` pairs, one per line.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Display name. The slug is derived from it (lowercased, non-alphanumerics → `-`). |
| `description` | strongly recommended | One line: what the skill does. Shown in the L1 catalog (system prompt) — make it specific and searchable. |
| `when_to_use` | strongly recommended | One line: the condition under which an agent should reach for this skill. |
| `version` | optional | Semver-ish string; defaults to `1.0.0`. |

Keep every frontmatter value on a **single line**. Don't use block scalars or
nested structures — the reader only understands flat `key: value` pairs.

```
---
name: Python Security Review
description: Review Python code for common security vulnerabilities and report findings by severity.
when_to_use: When the user asks for a security review or audit of Python code.
version: 1.0.0
---
```

## Body

Everything after the closing `---` is the markdown body — the **full
instructions** an agent follows once it loads the skill. There is no fixed schema,
but good bodies share a shape:

- A one-line restatement of the skill's job.
- **Inputs** it expects (what the user/agent provides).
- **Steps** — numbered or short sections, imperative voice ("Fetch…", "Compare…",
  "Report…"). One action per step.
- **Output format** — exactly how to present the result.
- **Rules / edge cases** — what to confirm, what to refuse, what to double-check.

Write for an agent, not a human reader: terse, unambiguous, actionable. Avoid
background essays; every line should change what the agent does.

## Progressive disclosure (L1 → L2 → L3)

- **L1 — catalog**: `name` + `description` + `when_to_use` are injected into the
  system prompt for every attached skill. Cheap, always present.
- **L2 — body**: the full `SKILL.md` body loads only when an agent calls
  `load_skill({slug})`. Keep the body focused so L2 stays affordable.
- **L3 — resources**: bundled files under `references/` / `assets/` / `scripts/`
  load on demand via `load_skill_resource({slug, path})`. Put long reference
  material (tables, templates, large examples) here, not in the body — this is how
  a big skill stays cheap until the detail is actually needed.

## Slug rules

The slug is how a skill is addressed and attached. It comes from `name`:
lowercased, every run of non-alphanumeric characters becomes a single `-`, leading/
trailing `-` trimmed. `"Python Security Review"` → `python-security-review`. Two
skills whose names slugify the same collide — pick a distinct name.
