---
name: Fact Checker
description: Verify a claim by decomposing it into checkable sub-claims, gathering at least 3 independent sources per sub-claim, triangulating, and issuing a fixed-scale verdict (Supported / Partly supported / Unsupported / Contradicted / Unverifiable) with sources.
when_to_use: When the user asks whether something is true, wants a claim, statistic, quote, or rumor verified, or asks you to fact-check a statement or article excerpt.
version: 1.0.0
---

# Fact Checker

Verify a claim: split it into checkable sub-claims, triangulate each against at
least 3 independent sources, and issue a fixed-scale verdict per sub-claim.

## Inputs

- **claim** — the statement to check, verbatim. If it came inside a longer text,
  quote exactly which sentence(s) you are checking.
- **context** — optional: who said it, when, where — affects which reading to test.

## Steps

1. Quote the claim verbatim, then decompose it into atomic sub-claims — each a
   single factual assertion checkable in isolation (an entity, a number, a date, a
   causal link each become their own sub-claim). List them before researching.
2. Load the verdict scale definitions:
   `load_skill_resource({"slug": "fact-checker", "path": "references/verdict-rubric.md"})` —
   apply them exactly; do not improvise verdict wording.
3. For each sub-claim, run 2-3 `browser_search` queries: one phrased to confirm,
   one phrased to refute (e.g. "<claim> debunked", "<claim> false"), one neutral.
   Searching only for confirmation is the classic failure — always run the refuting
   query.
4. `browser_fetch` candidate sources until you have at least 3 **independent** ones
   per sub-claim — independent means not citing each other or a common wire story;
   different outlets republishing one press release count as ONE source. Prefer
   primary evidence (official records, filings, the original dataset or paper).
5. Triangulate per sub-claim: tally what each source supports, contradicts, or
   partially matches (right fact, wrong number/date/scope). Weigh source quality
   per the rubric, then assign one verdict from the fixed scale.
6. Derive the overall verdict for the whole claim from the sub-claim verdicts using
   the combination rule in `references/verdict-rubric.md`.
7. Output the report (format below). If asked to deliver it, send the full text via
   `deliver(channel, target, text)`.

## Output format

```
## Fact check: "<claim verbatim>"
Date checked: <YYYY-MM-DD>

Overall verdict: <scale term> — <one-line reason>

### Sub-claim 1: <atomic assertion>
Verdict: <Supported | Partly supported | Unsupported | Contradicted | Unverifiable>
Evidence: <2-4 sentences: what the sources show, incl. exact figures/dates found>
Sources: <url> · <url> · <url>

<!-- repeat per sub-claim -->

Caveats: <time-sensitivity, disputed definitions, what could change the verdict>
```

## Rules

- Use ONLY the five scale terms, capitalized exactly as in the rubric.
- Fewer than 3 independent sources for a sub-claim ⇒ its verdict CANNOT be
  Supported or Contradicted; per the rubric it degrades to Partly supported or
  Unverifiable. Say how many sources you found.
- Cite only pages actually opened with `browser_fetch`; snippets never count as
  evidence. Every listed URL must have been fetched.
- Quote numbers, dates, and names exactly as the source prints them; a near-match
  (right event, different figure) is evidence for Partly supported, not Supported.
- Distinguish "no evidence found" (Unverifiable) from "evidence it is false"
  (Contradicted) — never conflate them.
- Note when a claim was true at one time but is outdated — date the evidence.
- Stay neutral on opinions and predictions: they are not checkable facts — mark
  such sub-claims Unverifiable with a note, or exclude them explicitly.
- Egress is search/fetch and the named delivery target only.
