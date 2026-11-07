---
name: Csv Cleaner
description: Clean a messy CSV — trim whitespace, drop blank rows, dedupe exact duplicates, normalize dates to ISO and decimal commas to dots — writing <name>.clean.csv plus a count of every change.
when_to_use: When the user has a CSV with inconsistent formatting (stray spaces, blank lines, duplicate rows, mixed date formats, European decimal commas) and wants a tidy copy before analysis or import.
version: 1.0.0
---

# Csv Cleaner

You clean one messy CSV with the bundled `scripts/clean_csv.py` and report
exactly what changed — nothing is altered silently.

## Inputs

- Path to one `.csv` in the working folder (if unnamed, `glob` for `*.csv` and
  confirm). Output is always written next to it as `<name>.clean.csv`.
- The fixes are fixed-scope: trim cell whitespace, remove fully blank rows,
  remove exact duplicate rows, normalize `DD/MM/YYYY`-style dates to ISO
  `YYYY-MM-DD`, convert decimal commas (`1.234,56` → `1234.56`). Anything
  beyond that (renaming columns, fuzzy dedupe, refills) is out of scope — say
  so and point to Contact Deduper for fuzzy contact dedupe.

## Steps

1. Peek at the file first with `document_read(path)` (or `read_file` for small
   files) to confirm it is a delimited CSV and note anything unusual
   (semicolon delimiters, multiple header rows) to warn about.
2. Fetch the cleaner:
   `load_skill_resource({"slug": "csv-cleaner", "path": "scripts/clean_csv.py"})`.
3. Save it into the working folder with `write_file` as `clean_csv.py`.
4. Run it: `shell("python3 clean_csv.py <file.csv>")`. It writes
   `<name>.clean.csv` and prints the change report (trimmed cells, blank rows
   removed, duplicates removed, dates normalized, decimal commas fixed, rows kept).
5. Spot-check the output with `read_file` on the first ~20 lines of the
   `.clean.csv` — confirm the header survived and dates/numbers look right.
6. Relay the change report to the user, plus your spot-check verdict and any
   warnings from step 1.

## Output format

```
Cleaned <file.csv> → <file>.clean.csv
- trimmed cells: N
- blank rows removed: N
- exact duplicates removed: N
- dates normalized to ISO: N
- decimal commas fixed: N
Rows kept: N (of M)
Spot-check: <ok / what looks off>
```

## Rules

- The original file is never modified — the script only writes `.clean.csv`.
- If `shell` is not available, do not hand-simulate the cleaner on a big file:
  tell the user shell is disabled and offer to clean a small file (< ~200 rows)
  manually via `read_file` + `write_file`, reporting the same change counts.
- Date normalization assumes day-first for `31/12/2024`-style dates; if the file
  looks US-formatted (month-first), warn the user before running and let them
  decide — do not run and shrug afterwards.
- Semicolon-delimited or multi-header files: warn that the script expects
  comma-delimited, single header; offer to convert the delimiter first.
- Ambiguous rows are never dropped beyond exact duplicates; if the user wants
  fuzzy dedupe, that is a different job (Contact Deduper for contacts).
- Local-first: no network; input and output stay in the working folder.
