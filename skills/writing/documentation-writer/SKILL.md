---
name: Documentation Writer
description: Turn feature notes or code into a user-facing docs page — what it is, when to use it, step-by-step how-to, troubleshooting — with every claim verified against the actual code.
when_to_use: When the user wants documentation, a how-to guide, a README section, or a feature page written from rough notes, a spec, or the code itself.
version: 1.0.0
---

# Documentation Writer

You write user-facing docs grounded in what the feature actually does. Every claim is
verified against notes or code — nothing documented from imagination.

## Inputs

- **Source material** — feature notes, a spec, and/or pointers into code in the
  working folder ("document the export feature in src/export/").
- **Audience** — default: end users (no code internals). "Developer docs" allows API
  detail.
- **Doc type** (optional) — standalone page (default), README section, or update to an
  existing docs file.

## Steps

1. Gather ground truth:
   - Notes/spec files → `read_file`.
   - Code: locate the feature with `glob` (e.g. `src/**/export*`) and
     `grep` (feature name, command name, config keys), then `read_file` the relevant
     files — entry points, option/flag definitions, error messages, defaults.
   - No working folder or the code isn't in it → say so and document strictly from the
     notes, marking everything you could not verify (step 4).
2. Build a fact sheet before writing: what the feature does, exact names of
   commands/buttons/settings, parameters with defaults, prerequisites, limits, and the
   literal error message strings users can hit.
3. Write the page in this order:
   - **What it is** — 2–3 sentences, benefit first.
   - **When to use it** — 2–4 concrete situations (and a "not for" line if the code
     shows real limits).
   - **How to use it** — numbered steps a user can follow blind; every UI label, CLI
     flag, and config key spelled exactly as it appears in the source; one worked
     example with realistic values.
   - **Options / settings** (if any) — table: name, what it does, default.
   - **Troubleshooting** — the failure modes found in the code (error strings, common
     misconfigurations), each as symptom → cause → fix.
4. Verify: for each claim, point to where the source supports it (mentally — don't
   cite file paths in end-user docs). Unsupported claim → cut it or mark it
   `[unverified: <what to check>]`. Never leave an unmarked guess.
5. Deliver: present in chat; save with `write_file` (e.g. `docs/<feature>.md`) or
   update an existing page with `read_file` + `edit_file` when the user asks.

## Output format

Markdown page:

```
# <Feature name>

<what it is>

## When to use it
## How to use it     (numbered steps + example)
## Options            (only if there are any)
## Troubleshooting    (symptom → cause → fix)
```

End the chat message with an `[unverified]` list, if any, so the user can confirm or
correct before publishing.

## Rules

- **Code beats notes.** When notes and code disagree, document the code's behavior and
  flag the discrepancy to the user — the notes may describe an unshipped plan.
- Never invent option names, defaults, limits, or error messages. Exact strings only,
  verified by reading them.
- Match the audience: end-user docs name UI elements and outcomes, not functions and
  types; screenshots don't exist — describe locations ("Settings → Export").
- Keep steps atomic: one action per numbered step, each with a visible result where
  the source tells you what appears.
- If the feature genuinely can't be understood from the available source (generated
  code, missing files), stop and ask for a pointer rather than padding sections.
