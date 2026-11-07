---
name: Json Wrangler
description: Transform JSON files with a bundled stdlib tool — flatten nested objects to dot-keys, convert an array of objects to CSV, or filter array items by a field value.
when_to_use: When the user has a JSON file to reshape — "flatten this", "turn this JSON into a spreadsheet/CSV", "keep only the items where status is active" — rather than to merely read or explain.
version: 1.0.0
---

# Json Wrangler

You transform JSON files in the working folder with `scripts/json_tool.py`,
choosing the right subcommand for the user's ask.

## Inputs

- Path to a `.json` file in the working folder (if unnamed, `glob` for `*.json`
  and confirm).
- The transformation the user wants; output filename (defaults: `out.json` /
  `out.csv`).

## Choosing the subcommand

- `flatten` — the user wants nested structure collapsed: `{"user":{"name":"Ana"}}`
  becomes `{"user.name":"Ana"}`; list items get numeric keys (`tags.0`). Right
  for "make this flat / diffable / one key per line".
- `tocsv` — the input is an ARRAY of objects and the user wants a
  table/spreadsheet/Excel-openable file. Each object becomes a row (nested keys
  are flattened into dot-named columns; the column set is the union across rows).
- `filter --path a.b --equals X` — keep only array items whose field at the
  dot-path equals X (string compare, so `--equals 42` matches the number 42).
  Right for "only the active ones", "just the rows where region is ID".
- Combinations run as a pipeline: e.g. filter first to `filtered.json`, then
  `tocsv` on that output.

## Steps

1. Inspect the input with `read_file` (or `document_info` if very large) to see
   the shape: object vs array, nesting depth, the field to filter on.
2. Pick the subcommand(s) per the guide above; tell the user your plan in one line.
3. Fetch the tool:
   `load_skill_resource({"slug": "json-wrangler", "path": "scripts/json_tool.py"})`,
   save it into the working folder with `write_file` as `json_tool.py`.
4. Run it via shell, e.g.:
   - `shell("python3 json_tool.py flatten data.json -o data.flat.json")`
   - `shell("python3 json_tool.py tocsv data.json -o data.csv")`
   - `shell("python3 json_tool.py filter data.json --path status --equals active -o active.json")`
5. Verify: `read_file` the first lines of the output; for `tocsv` confirm the
   header columns; for `filter` confirm the kept-count printed by the tool is
   plausible against step 1.
6. Report what was produced and the counts.

## Output format

```
<subcommand> on <in.json> → <out file>
- items in / out (filter) or rows x columns (tocsv) or keys produced (flatten)
- note anything lossy (e.g. empty cells for objects missing a column)
```

## Rules

- The tool errors (exit non-zero) if the shape does not fit — e.g. `tocsv` on a
  non-array. Relay the error and suggest the fix (often: flatten or filter
  first, or the array lives under a top-level key — extract it first).
- If `shell` is unavailable: for small files (< ~100 items) do the transform
  manually and `write_file` the result, stating it was done by hand; for large
  files tell the user to enable shell.
- Never reorder or rewrite values beyond the transform; `filter` keeps items
  byte-equivalent, `tocsv` writes values as-is.
- The original file is never modified; always write to a new output path.
- Deep filtering beyond exact equality (ranges, contains) is out of scope —
  offer to do it manually on small data instead of inventing flags.
- Local-first: no network; inputs and outputs stay in the working folder.
