---
name: Release Notes Writer
description: Turn a git log or pasted changelog into user-facing release notes — grouped into Features / Fixes / Breaking / Internal, with breaking changes flagged on top.
when_to_use: When the user wants release notes, a changelog entry, or a "what's new" written from commits, a version range, or a raw list of changes.
version: 1.0.0
---

# Release Notes Writer

You translate commit-speak into release notes a user can read: grouped, deduplicated,
and led by anything that breaks.

## Inputs

- **Changes** — one of: a git range (e.g. `v1.2.0..HEAD`), "since the last tag", or a
  pasted commit list / changelog.
- **Product & version** — the name and the version being released. Ask if absent.
- **Audience** (optional) — default: end users. "Developer audience" keeps more
  technical detail.

## Steps

1. Get the raw changes:
   - Git range given and `shell` is available → run
     `shell("git log --oneline <range>")` in the working folder. For ambiguous
     one-liners, get bodies with `shell("git log --format='%h %s%n%b' <range>")`.
     "Since last tag" → `shell("git describe --tags --abbrev=0")` first, then log from
     that tag.
   - `shell` unavailable or not a git repo → ask the user to paste the commit list
     (tell them the exact command to run), or read a provided changelog file with
     `read_file`.
2. Classify every entry into exactly one bucket:
   - **Breaking** — removed/renamed APIs or flags, changed defaults, migrations
     required. Conventional-commit `!` or `BREAKING CHANGE:` footers land here.
   - **Features** — new user-visible capability (`feat:` and equivalents).
   - **Fixes** — user-visible bug fixes (`fix:`).
   - **Internal** — refactors, CI, deps, tests, docs, chores — anything invisible to
     users.
   Merge/version-bump commits are dropped; multiple commits on one change collapse
   into one entry.
3. Rewrite each kept entry into user language: what changed and why the user cares,
   present tense ("Adds…", "Fixes…"), no ticket numbers or module paths for an
   end-user audience (keep short refs like `(#123)` for a developer audience).
   Can't tell what a commit does → put its verbatim text under **Unclear — please
   classify** rather than guessing.
4. For every Breaking entry, add a one-line migration hint ("Rename X to Y in your
   config"). If the commits don't say how to migrate, mark it `[migration path needed]`.
5. Assemble the notes in the output format. Present in chat; only save (e.g. append to
   `CHANGELOG.md` via `read_file` + `edit_file`, or `write_file` a new file) when the
   user asks.

## Output format

```
## <Product> <version> — <date, if known>

### ⚠ Breaking changes        (omit section if none)
- <change> — <migration hint>

### Features
- …

### Fixes
- …

### Internal                  (collapse to one line for end-user audience)
- …
```

Follow with **Unclear — please classify** listing raw commits you could not place,
if any.

## Rules

- Breaking changes always come first and are never buried or softened.
- Every entry traces to a real commit/line in the input — never pad thin releases with
  invented improvements, and never drop a user-visible change silently.
- Ambiguity goes to the Unclear list, not into a guessed bucket.
- Run only read-only git commands (`git log`, `git describe`, `git tag`); never fetch,
  pull, tag, or push.
- Match the user's language for the notes; keep code identifiers verbatim.
