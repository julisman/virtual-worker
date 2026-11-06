# Worked example — a complete skill

This is a full, spec-compliant `SKILL.md`. Study its shape (crisp frontmatter, an
imperative step-by-step body, a fixed output format) and mirror it when you author
a new skill.

```markdown
---
name: Python Security Review
description: Review Python code for common security vulnerabilities and report findings ranked by severity, with a fix for each.
when_to_use: When the user asks for a security review, audit, or vulnerability check of Python code.
version: 1.0.0
---

# Python Security Review

Review the provided Python code for security vulnerabilities and report each
finding with its severity, location, and a concrete fix.

## Inputs

- The Python source to review (a file, a diff, or pasted snippets).
- Optional: the framework in use (Django/Flask/FastAPI) — adjust checks to it.

## Steps

1. Read all provided code before judging any part of it — note entry points
   (request handlers, CLI args, deserialization, file/DB access).
2. Check each category below and record every concrete hit (file + line + why):
   - **Injection** — SQL/OS-command/template built from unsanitized input;
     `eval`/`exec`/`pickle.loads`/`yaml.load` on untrusted data.
   - **AuthN/AuthZ** — missing access checks, hardcoded credentials/secrets,
     weak/absent token verification.
   - **Crypto** — `md5`/`sha1` for passwords, `random` for secrets, disabled TLS
     verification, static IVs/keys.
   - **Data exposure** — secrets in logs, verbose error/tracebacks to users,
     sensitive data in responses.
   - **Input validation** — unvalidated path joins (traversal), unchecked
     redirects, missing size/type limits.
3. For each finding, assign a severity: **Critical / High / Medium / Low**, based
   on exploitability and impact.
4. For each finding, write a one-line fix (and a code snippet when it's short).
5. If you find nothing in a category, don't invent issues — say it's clean.

## Output format

Report as a table sorted by severity (Critical first):

| Severity | Location | Issue | Fix |
|---|---|---|---|

Then a one-line summary: total findings by severity. If the code is clean, say so
plainly and note what you checked.

## Rules

- Only report issues you can point to a specific line for — no speculation.
- Prefer a standard-library or framework-native fix over a new dependency.
- Never include a real exploit payload; describe the risk and the fix.
```

Notice what makes it good: the `description`/`when_to_use` are specific enough that
another agent knows exactly when to load it; the body is all imperative steps with
a fixed output contract; and it tells the agent both what to do *and* what **not**
to do (don't invent findings, don't speculate).
