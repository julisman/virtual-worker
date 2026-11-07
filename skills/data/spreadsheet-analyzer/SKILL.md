---
name: Spreadsheet Analyzer
description: Analyze an Excel or CSV file — profile every column, compute key stats and outliers, and report 3-5 concrete insights about the data.
when_to_use: When the user drops a spreadsheet (xlsx/csv) in the working folder and asks what is in it, to summarize it, find anomalies, or "make sense of this data".
version: 1.0.0
---

# Spreadsheet Analyzer

You take one spreadsheet from the working folder and return a column profile,
key statistics, outliers, and 3-5 insights the user can act on.

## Inputs

- Path to one `.xlsx`, `.xls`, or `.csv` file in the working folder. If the user
  did not name one, `glob` for `*.xlsx`, `*.xls`, `*.csv` and confirm which to use.
- Optional: a question to focus on (e.g. "which region underperforms?"). Default:
  a general profile + insights.

## Steps

1. Call `document_info(path)` first to get size/sheet structure. For Excel files
   with several sheets, ask which sheet matters (default: the first non-empty one).
2. Small file (fits in one read): call `document_read(path)` and analyze the
   content directly — build the column profile yourself.
3. Large CSV (document_info shows many rows, or document_read truncates): use the
   bundled profiler instead of reading it all. Call
   `load_skill_resource({"slug": "spreadsheet-analyzer", "path": "scripts/profile_csv.py"})`,
   save the script into the working folder with `write_file` (e.g. `profile_csv.py`),
   then run `shell("python3 profile_csv.py <file.csv>")` and use its report.
4. Large Excel with no shell available: read what `document_read` returns, state
   clearly that the profile covers only the first N rows returned.
5. From the profile, derive per column: type (numeric/text/date-like), empty
   count, min/max/mean for numerics, top values for categoricals.
6. Look for outliers: numeric values far from the mean (rough rule: > 3x the
   typical spread, or obviously impossible values like negative quantities),
   suspicious duplicates, columns that are mostly empty.
7. Write 3-5 insights grounded in the numbers (trends, concentration, data
   quality problems, anomalies). Tie each insight to a specific column or value.

## Output format

```
## <filename> — profile
Rows: N · Columns: M (analyzed: all rows / first N rows)

| Column | Type | Empty | Stats |
|---|---|---|---|
| ...    | ...  | ...   | min/max/mean or top values |

## Outliers & data quality
- <finding with the exact rows/values>

## Insights
1. ... (3-5 numbered, each citing a concrete number)
```

## Rules

- Never invent statistics. Every number in the output must come from
  `document_read` output or the `profile_csv.py` report.
- If `shell` is not available for a large CSV, fall back to `document_read` and
  say the analysis is partial — do not silently pretend it covers the whole file.
- Empty or unreadable file: say so and stop; do not fabricate a profile.
- Mixed-type columns (numbers and text) are reported as text with a note —
  flag them as a data-quality issue.
- Stay local: no network calls; the data never leaves the machine.
- Reply in the user's language; keep column names verbatim from the file.
