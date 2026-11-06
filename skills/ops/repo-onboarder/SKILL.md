---
name: Repo Onboarder
description: Onboard into a code repository in the working folder — map the layout with list_dir/glob/grep, read the key files, and write a concise onboarding doc covering purpose, structure, setup, conventions, and where to start.
when_to_use: When the user points you at a repo/codebase in the working folder and asks to understand it, document it, onboard a new developer, or "explain this project".
version: 1.0.0
---

# Repo Onboarder

Explore a repository in the working folder and produce one onboarding document a new developer can start from.

## Inputs

- **Repo path** — relative path inside the working folder. Default: the working folder root.
- **Output file** — where to write the doc. Default: `ONBOARDING.md` at the repo root (ask before overwriting an existing one).
- **Audience** — default: a developer new to this codebase.

## Steps

1. `list_dir` the repo root. Note top-level folders and obvious entry files.
2. `glob` for the signals that identify the stack: `**/package.json`, `**/pyproject.toml`, `**/requirements.txt`, `**/go.mod`, `**/Cargo.toml`, `**/pom.xml`, `**/Makefile`, `**/Dockerfile`, `**/*.config.*`, `**/.github/workflows/*`.
3. `read_file` the key files, in this priority: README (any casing), the main manifest(s) from step 2, the entry point (e.g. `main.py`, `index.ts`, `app.py`, `cmd/*/main.go`), and any CONTRIBUTING/AGENTS/CLAUDE instruction files.
4. `grep` to confirm how things wire together: the entry point's imports, route/handler registration, test locations (`grep` for `test_`/`describe(`), and script names in the manifest.
5. Skim one or two representative source folders with `list_dir` + `read_file` on a small file each, to capture naming and layering conventions — do not read the whole tree.
6. Draft the onboarding doc with these sections, each grounded in files you actually read (cite paths):
   - **Purpose** — what the project does, one paragraph.
   - **Structure** — annotated tree of the top-level folders.
   - **Setup & run** — install, run, and test commands as found in README/manifest/Makefile; mark anything you inferred rather than found.
   - **Conventions** — language style, naming, where new code of each kind goes.
   - **Where to start** — 3–5 concrete first files/flows to read, in order.
7. `write_file` the doc to the output file and report its path.
8. If this agent has wiki tools attached, offer the alternative before writing: `wiki_ingest_repo` builds a full browsable wiki of the repo — ask which the user prefers (quick doc vs full wiki) and do only the chosen one.

## Output format

Chat reply:

```
Onboarding doc written → <path>
Stack: <e.g. Python/FastAPI + React/Vite>
Sections: Purpose · Structure · Setup · Conventions · Where to start
Gaps I could not verify: <list or "none">
```

The doc itself follows the six-section outline in step 6.

## Rules

- Ground every claim in a file you read; when you infer (e.g. a run command that is not documented), label it "inferred — verify".
- Do not read huge/generated folders (`node_modules`, `dist`, `build`, `.git`, vendored deps, lockfiles beyond a glance at names).
- Read-only except the single output file; never modify repo sources.
- Large repos: cap exploration at the key files above plus ~10 extra reads; say what you skipped rather than guessing about it.
- No shell needed for this skill; if the user wants commands *executed* (e.g. run the tests), that requires the `shell` tool — say so if it is off.
- If the folder has no code (empty or docs-only), say so and write a shorter doc instead of padding.
