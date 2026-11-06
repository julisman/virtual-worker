# Skills library

Drop-in skill packs installable from **Agents → Skills** (or bundled as the
built-in gallery). Each pack is a self-contained
[agentskills.io](https://agentskills.io) skill: a `SKILL.md` with YAML
frontmatter (`name` / `description` / `when_to_use` / `version`) + a markdown
body, optionally bundled with `references/`, `assets/`, and `scripts/`.

## Packs here

| Pack | What it is |
|------|------------|
| `skill-creator/` | The **meta-skill**: teaches an agent to author *other* skills. Bundles the SKILL.md **spec** and a worked **example** as `references/`. Attach it to an agent that has **Skill authoring** on (Agent → Guardrails), then ask it to "create a skill for …" and it writes a spec-compliant pack via the `create_skill` tool. |
| `senior-software-engineer/` | Delivers coding tasks by **driving Claude Code** (the `claude` CLI) inside the agent's working folder — bundles a `claude-run` wrapper (`scripts/`) that survives the shell tool's time cap, then polls, reviews the diff, and verifies. Needs the shell tool + a working folder. |
| `delegate-agent/` | Spawns and supervises durable **Codex CLI** and **Claude Code** agents through one backend-neutral interface: backend/model discovery, role-aware defaults, concurrent groups, progress inspection, steering, cancellation, session resume. |
| `productivity/` | 10 assistant skills for a worker's day-to-day: daily briefing, email triage (via a connected Gmail MCP), calendar day planning, meeting minutes, standup reports, task-board grooming, brain-dump→tasks, follow-up tracking, weekly review, recurring-digest setup. |
| `research/` | 10 web-research skills: trending-topic digests, arXiv paper scanning, deep research briefs, competitor watch, fact checking, stock quotes, news digests, product price scouting, people/company briefs, comparison matrices. |
| `writing/` | 10 writing skills: email drafting, executive summaries, blog posts, social-media repurposing, release notes, proposals, tone rewriting, EN↔ID translation/localization, documentation, press releases. |
| `data/` | 10 file/data skills (several bundle stdlib-Python `scripts/` with `--self-test`): spreadsheet analysis, PDF extraction, invoice processing, CSV cleaning, survey summaries, log analysis, JSON wrangling, contact dedupe, expense categorizing, HTML chart reports. |
| `ops/` | 10 operational skills: website monitoring, web form filling, approval-request triage, repo onboarding, safe chat-channel broadcasts, memory hygiene, scheduled-job debugging, polite web scraping, dependency audits, working-folder backups. |

The five category packs (`productivity/` `research/` `writing/` `data/` `ops/` —
50 skills) are gated by `backend/tests/test_skill_packs.py`: frontmatter/spec
checks, loader round-trip through `storage.skill_store`, zip round-trip, and a
`--self-test` run of every bundled script.

Both `.md` and `.zip` uploads are supported in the UI. A `.zip` must contain a
`SKILL.md` (at any depth); sibling files are kept as resources.

## Skill structure & leveling (progressive disclosure)

Only what's relevant reaches the model, so the prompt stays small:

```
skills/<slug>/
├── SKILL.md            ← L1 frontmatter (name/description/when_to_use/version)
│                          + L2 body (the full instructions)
├── references/         ← L3 resource files (markdown, text…)
├── assets/             ← L3 assets (configs, images…)
└── scripts/            ← L3 helper scripts
```

- **L1 — catalog**: every attached skill's `name`, `slug`, `description`
  (and `when_to_use`) are injected into the system prompt on every turn.
- **L2 — instructions**: the model calls `load_skill({slug})` to read the full
  `SKILL.md` body only when the skill becomes relevant.
- **L3 — resources**: `load_skill_resource({slug, path})` loads a bundled file
  on demand. `load_skill` lists what's available.

### Frontmatter fields

| Field | Required | Notes |
|-------|----------|-------|
| `name` | yes | Display name |
| `description` | yes | One line; shown in the catalog |
| `when_to_use` | no | Trigger hint for the model |
| `version` | no | Semver; used by the built-in gallery to offer updates |
