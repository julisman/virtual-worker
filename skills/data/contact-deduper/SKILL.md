---
name: Contact Deduper
description: Deduplicate a contacts CSV — normalize emails and phones, group duplicates by shared email, phone, or near-identical name, write a merged CSV plus a merge report listing every group and conflict.
when_to_use: When the user has a contact/customer list with duplicate people (same person, varying spelling, formats, or partial data) and wants one clean row per person without losing information.
version: 1.0.0
---

# Contact Deduper

You dedupe one contacts CSV with `scripts/dedupe_contacts.py`: duplicates are
grouped, the first row of each group is kept, and every merge and conflict is
reported — nothing is dropped silently.

## Inputs

- Path to a contacts `.csv` in the working folder (if unnamed, `glob` for
  `*.csv` and confirm). It must have columns whose names contain `name`,
  `email`, and/or `phone` (case-insensitive; any subset works).
- Output: `<name>.deduped.csv` next to the input (script default).

## How grouping works (explain this to the user)

Two rows are the same contact when ANY of these match after normalization:
- email (trimmed, lowercased), or
- phone (digits only, compared on the last 10 digits — so `0812-345-678 90`
  and `+62 812 3456 7890` match), or
- name with difflib similarity >= 0.85 (catches `Agus Salim` vs `Agoes Salim`).
Within a group the FIRST row is kept as the survivor; differing values in other
rows are NOT merged into it — they are listed as conflicts for the user to decide.

## Steps

1. Peek at the header with `read_file` to confirm name/email/phone columns
   exist; warn which signals are missing (e.g. no phone column → phone matching
   is skipped).
2. Fetch the script:
   `load_skill_resource({"slug": "contact-deduper", "path": "scripts/dedupe_contacts.py"})`,
   save it into the working folder with `write_file` as `dedupe_contacts.py`.
3. Run `shell("python3 dedupe_contacts.py <contacts.csv>")`. It writes
   `<name>.deduped.csv` and prints the merge report: each duplicate group with
   all its original rows, plus every field where kept and dropped rows disagreed.
4. Review the report for false merges (different people sharing an office phone
   or a generic email like info@) — call these out explicitly.
5. Present the report. For conflicts, ask the user whether to keep the
   survivor's value or edit specific rows; apply requested fixes to the
   deduped CSV with `edit_file`.

## Output format

```
Deduped <file>.csv → <file>.deduped.csv
Rows: <in> → <out> (<g> duplicate groups merged)

Groups (from the merge report):
- Group 1 (3 rows) kept: Agus Salim / agus@mail.com
  ! conflict in 'Phone': kept '0812...', also saw '+62 812...', '0812999...'
Possible false merges to check: <shared office phone, generic emails> or "none"
Open decisions for you: <conflict questions>
```

## Rules

- Never silently drop data: every removed row appears in the merge report, and
  every conflicting field is listed with all its values.
- The survivor is always the FIRST row of the group — deterministic, no
  cleverness. The user decides conflict resolution, not you.
- Generic emails (info@, admin@, sales@) and shared landlines can over-merge:
  always scan groups for different names glued together and flag them.
- Original file is never modified.
- If `shell` is unavailable: for small lists (< ~100 rows) replicate the same
  rules manually via `read_file`/`write_file` with the same report format;
  otherwise tell the user to enable shell.
- Contact data is personal — keep it local; never deliver it off-machine unless
  the user explicitly asks for a specific delivery target.
