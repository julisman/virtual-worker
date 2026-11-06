---
name: Executive Summary
description: Condense a long document into a one-page executive summary — 3-bullet TL;DR, key findings, risks, and asks/decisions needed, with every number preserved exactly.
when_to_use: When the user gives a long report, PDF, Word doc, or text file and wants a summary, brief, TL;DR, or one-pager for leadership or busy readers.
version: 1.0.0
---

# Executive Summary

You compress a long document into one page a busy decision-maker can act on. The
summary is grounded in the document only — never in outside knowledge or guesses.

## Inputs

- **Document** — a file path in the working folder, or text pasted in chat.
- **Audience** (optional) — default: senior leadership with no prior context.
- **Emphasis** (optional) — e.g. "focus on budget"; otherwise weight by what the
  document itself treats as important.

## Steps

1. Get the full document text:
   - PDF / Word / Excel / PowerPoint → `document_read(path)`. If it fails or the file
     type is unclear, check with `document_info(path)` first.
   - Plain text / markdown → `read_file`.
   - Pasted text → use it as-is.
   - Path not found → `list_dir` or `glob` to locate it; if still missing, ask the user.
2. Read end to end before writing anything. Note section structure, conclusions, and
   every figure (amounts, percentages, dates, counts) with its exact value and unit.
3. Extract, as raw notes: (a) the document's core message, (b) findings with their
   supporting numbers, (c) stated or implied risks and caveats, (d) requests, open
   decisions, and deadlines.
4. Write the summary in the output format below. Order findings by importance to the
   audience, not by document order.
5. Verify numbers: compare every figure in your summary against the source text
   character-for-character (value, unit, currency, period). Fix any mismatch. Never
   round, convert, or aggregate unless the user asked.
6. If the document names no risks or asks, write "None stated in the document" for
   that section — do not invent any.
7. Present in chat. Only save to a file if the user asks (then `write_file`, e.g.
   `<docname>-summary.md`).

## Output format

```
# Executive Summary — <document title>

**TL;DR**
- (3 bullets, each one sentence, together carrying the whole story)

**Key findings**
- (3–6 bullets, each with its exact supporting number where one exists)

**Risks**
- (bullets; or "None stated in the document")

**Asks / decisions needed**
- (bullets naming the decision, owner if stated, and deadline if stated)
```

Total length: one page (~350 words max). TL;DR is exactly 3 bullets.

## Rules

- **Numbers are sacred.** Reproduce every figure exactly as written in the source —
  same precision, unit, and currency. "About 2M" for "IDR 1,987,400,000" is wrong.
- Summarize only what the document says. No outside context, no filled gaps, no
  editorializing. If a key section is illegible or truncated, say so under Risks.
- Very long documents: `document_read` may paginate or truncate — read all parts
  before summarizing; never summarize from a fragment silently.
- Keep the author's qualifiers ("preliminary", "estimated", "unaudited") attached to
  the claims they qualify.
- If the user gives multiple documents, confirm whether they want one combined summary
  or one per document before starting.
