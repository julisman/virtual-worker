---
name: Survey Summarizer
description: Turn a survey-responses CSV into a report — response count, per-question counts and percentages, and qualitative themes from free-text answers with representative anonymized quotes.
when_to_use: When the user has exported survey results (Google Forms, Typeform, internal tool) as CSV and wants a readable summary of what respondents said, quantitative and qualitative.
version: 1.0.0
---

# Survey Summarizer

You read one survey-responses CSV (one row per respondent, one column per
question) and produce a report: numbers for closed questions, themes with
anonymized quotes for open ones.

## Inputs

- Path to the responses CSV in the working folder (if unnamed, `glob` for
  `*.csv` and confirm).
- Optional: which questions to focus on; whether to write the report to a file
  (default: chat only; on request `write_file` it as `survey-report.md`).

## Steps

1. Read the file with `document_read(path)`. The header row holds the question
   texts; each later row is one respondent.
2. Classify each column:
   - Quantitative: a small set of repeated answers (scales 1-5, yes/no,
     multiple choice) → tally it.
   - Qualitative: mostly unique free text → theme it.
   - Metadata (timestamp, email, name, ID) → use only for the response count
     and deduplication; NEVER quote or list it.
3. Response count: total rows, minus exact duplicate submissions (same
   respondent + same answers) — note how many were dropped.
4. For each quantitative question: counts and percentages per answer option
   (percent of those who answered, with the answered/skipped split stated).
   For numeric scales also give the mean.
5. For each qualitative question: group the answers into 3-6 themes. Name each
   theme, give its rough share (count of answers touching it), and pick 2-3
   short representative quotes. Anonymize quotes: strip names, emails, team
   names, and any detail that identifies the writer; mark edits with [...].
6. Note limits honestly: small n, heavy skips, one-sided sample.
7. Very large files (document_read truncates): analyze the returned rows and
   state the coverage ("first N of M responses") — or, if `shell` is available,
   offer to count answer options across the full file with a short one-off
   python3 script written via `write_file`.

## Output format

```
# Survey summary — <file>
Responses: N (M duplicates removed) · Questions: K

## Q: <question text>            (quantitative)
- Option A — 12 (48%)
- Option B — 9 (36%)  · answered 25/28

## Q: <question text>            (open text)
Themes:
1. <theme name> (~9 mentions) — one-line description
   > "quote 1" · > "quote 2"

## Caveats
- ...
```

## Rules

- Every count and percentage must be tallied from the actual rows — no
  estimated numbers. If coverage is partial, every section must say so.
- Quotes are verbatim except anonymization; never compose a fake "typical"
  quote. If no quote can be anonymized safely, describe the theme without one.
- Do not report per-respondent answers or cross-tabulate in ways that
  de-anonymize (e.g. "the only manager said..."). Aggregates only.
- Fewer than 5 responses: present answers as a simple list, skip percentages —
  they would mislead.
- Local-first: the data never leaves the machine; deliver the report in chat or
  the working folder only.
- Write the report in the user's language; keep quotes in the respondent's language.
