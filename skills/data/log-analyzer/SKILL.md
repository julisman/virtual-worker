---
name: Log Analyzer
description: Analyze log files in the working folder — per-level counts, top recurring error signatures, busiest time buckets, time range — and narrate likely hotspots.
when_to_use: When the user drops application/server log files in the working folder and asks what went wrong, how many errors there are, or when problems spiked.
version: 1.0.0
---

# Log Analyzer

You crunch log files with the bundled `scripts/log_summary.py` and turn the
numbers into findings: what is failing, how often, and when.

## Inputs

- Log file path(s) in the working folder. If unnamed, locate candidates with
  `glob` (`*.log`, `*.log.*`, `*.txt`, `*.out`) and confirm which to analyze.
- Optional: `--top N` for how many error signatures to show (default 5), and a
  focus question ("why did it crash at 11:00?").

## Steps

1. Locate the files with `glob`; for a quick sanity peek use `read_file` on the
   first lines to confirm the format (timestamps + levels).
2. Fetch the summarizer:
   `load_skill_resource({"slug": "log-analyzer", "path": "scripts/log_summary.py"})`,
   save it into the working folder with `write_file` as `log_summary.py`.
3. Run it per file: `shell("python3 log_summary.py <file.log> --top 10")`.
   It prints: time range, per-level counts, top error signatures (numbers/hex
   ids normalized to `<n>`/`<id>` so repeats group together), busiest hour buckets.
4. For the top signatures, pull 1-2 real example lines with
   `grep` (a distinctive phrase from the signature) so the findings cite raw
   evidence, not just normalized shapes.
5. Correlate: do error spikes line up with the busiest buckets? Does one
   signature dominate? Do errors start after a deploy/restart line?
6. Narrate findings: the likely hotspots (component, operation, time window),
   ranked by evidence. Separate facts (counts, timestamps) from hypotheses.
7. If `shell` is unavailable: degrade to `grep` for level keywords
   (`ERROR`, `WARN`) + `read_file` on the matching regions, and say the counts
   are approximate.

## Output format

```
## Log analysis — <file(s)>
Time range: <first> .. <last> · Lines by level: ERROR=…, WARN=…, INFO=…

Top error signatures:
1. 214x  timeout fetching order <n> from host <n>
   e.g. "2026-01-05 10:02:10 ERROR timeout fetching order 12345 from host-2"

Busiest windows: <bucket> (<count> lines), ...

## Findings
1. <hotspot + evidence>
Hypotheses to check: ...
```

## Rules

- Counts come only from the script (or explicit grep counts) — never eyeballed
  estimates presented as numbers.
- Signatures are normalized; always show at least one raw example line per top
  signature so the user can search their own tooling for it.
- Multiple files: summarize each separately, then a combined narrative — do not
  blend counts across files without saying so.
- Huge files are fine (the script streams line by line); do not paste large log
  chunks into chat — quote at most a few lines each.
- No timestamps/levels detected: report what the script could still count and
  ask for a format hint rather than guessing a parse.
- Logs can contain secrets (tokens, emails): quote only the minimal lines
  needed, and never send log content anywhere (no webhook/channel delivery
  unless the user explicitly asks). Local-first.
