---
name: Dependency Auditor
description: Audit project dependencies in the working folder via shell — detect the ecosystem (npm or Python), run outdated/vulnerability checks, summarize by severity, and propose a safe upgrade order. Report only, never auto-upgrade.
when_to_use: When the user asks to check dependencies, find outdated or vulnerable packages, plan upgrades, or "is this project safe to update" for a repo in the working folder.
version: 1.0.0
---

# Dependency Auditor

Report the state of a project's dependencies — what's outdated, what's vulnerable, and the order to upgrade — without changing a single file.

## Inputs

- **Project path** — relative path in the working folder. Default: the working folder root.
- **Scope** — default: direct dependencies; include transitive findings when the audit tool surfaces them.

## Steps

1. Confirm the `shell` tool is available. If it is off, stop and tell the user to enable the shell tool for this agent — without it you can only `read_file` the manifests and report declared versions, with no outdated/vulnerability data (offer that fallback).
2. Detect the ecosystem with `glob`/`list_dir`: `package.json` → npm; `requirements.txt` or `pyproject.toml` → Python. Both present → audit both. Neither → report "no supported manifest found" and stop.
3. **npm**: from the project dir run via `shell`:
   - `npm outdated --json` (current vs wanted vs latest per package)
   - `npm audit --json` (known vulnerabilities with severity)
   If `node_modules` is missing, `npm outdated` still works from the manifest, but `npm audit` needs a lockfile — note whichever data source is missing rather than guessing.
4. **Python**: via `shell`:
   - `pip list --outdated` (use the project's venv pip if one exists, e.g. `.venv/bin/pip`, else `python3 -m pip`)
   - Cross-check the pinned versions in `requirements.txt`/`pyproject.toml` (`read_file`) against the outdated list. There is no stdlib vulnerability scanner — mark the vulnerability column "not scanned (no audit tool)" unless the project already has one installed.
5. Classify each finding: **patch** (x.y.Z), **minor** (x.Y.z), **major** (X.y.z), and severity for vulnerabilities (critical/high/moderate/low).
6. Propose an upgrade order: (1) security patches for critical/high vulns, (2) plain patch bumps, (3) minor bumps, (4) majors last — each major flagged with a one-line breaking-change note (from the audit output or the package's stated major-version change; if unknown, say "check changelog before upgrading").
7. Report per the Output format. Make **no** changes: no `npm install`, `npm update`, `npm audit fix`, or `pip install` — ever.

## Output format

```
Dependency audit — <project> (<npm | python | both>)
Vulnerable: <n>  (critical <n> · high <n> · moderate <n> · low <n>)
Outdated:   <n>  (patch <n> · minor <n> · major <n>)

Top findings:
  [critical] <pkg> <installed> → <fixed-in>  — <one-line issue>
  [major]    <pkg> <installed> → <latest>    — breaking: <note>

Suggested upgrade order:
  1. <security patches>   2. <patches>   3. <minors>   4. <majors — with notes>

Not scanned / caveats: <e.g. "python vulns not scanned", "no lockfile — npm audit skipped">
```

## Rules

- **Report only. NEVER upgrade, install, or modify manifests/lockfiles** — even if the user says "fix it" mid-audit, finish the report and let them explicitly request changes as a separate task.
- Run only read-only commands: `npm outdated`, `npm audit`, `pip list --outdated`, version prints. No `--fix` flags, no installs, no network beyond what those commands themselves do against their registries.
- Parse JSON output when available; if a command fails (no npm, offline, broken venv), quote the error and mark that section "unavailable" instead of fabricating results.
- Never invent CVEs, severities, or "fixed-in" versions — only what the tool output states; unknowns are reported as unknowns.
- Monorepos: audit the manifest nearest the given path; list other manifests found and ask before auditing them all.
- Keep raw tool output out of the summary; offer to `write_file` a full `dependency-audit-<YYYY-MM-DD>.md` report if the user wants details.
