---
name: Comparison Matrix
description: Criteria-driven comparison of 2-5 products, vendors, or tools — agree the criteria with the user first, research every cell with sources, and output a markdown matrix with per-option verdicts and a constraint-matched recommendation.
when_to_use: When the user wants to choose between named options ("X vs Y", "compare these tools", "which vendor should we pick") and needs a structured, sourced comparison rather than a quick opinion.
version: 1.0.0
---

# Comparison Matrix

Compare 2-5 options against criteria the user signed off on, fill every cell from
sourced research, and recommend the option matching the user's constraints.

## Inputs

- **options** — 2-5 named products/vendors/tools. More than 5 ⇒ ask the user to
  shortlist (or propose a cut) before comparing.
- **criteria** — what to judge on. If not given, propose them (step 1).
- **constraints** — the user's hard limits and priorities (budget, platform, team
  size, must-have features, self-hosted vs cloud). Ask if absent — the
  recommendation depends on them.

## Steps

1. **Agree criteria first.** Propose 4-7 criteria fitted to the decision (typical:
   price, core capability fit, platform/integration, maturity/support, licensing,
   performance). Mark which are the user's hard constraints. Ask the user to
   confirm or edit — do NOT research before the criteria are agreed, or you'll
   research the wrong things. (Skip only if the user already gave explicit criteria;
   restate them and proceed.)
2. For each option, `browser_search("<option> <criterion>")` for the criteria not
   answerable from its official pages, and `browser_fetch` the option's official
   product/pricing/docs pages plus 1-2 independent reviews or comparisons. Reuse
   one fetched page for several cells where it genuinely covers them.
3. Fill every cell of the matrix with a short factual value (number, tier name,
   yes/no + qualifier) and a source index `[n]`. A cell you could not source is
   "unknown [—]", never a guess. Note stale sources (pricing pages change; date any
   figure that looks old).
4. Sanity-check comparability: same currency and billing period for prices, same
   plan tier compared across vendors, same benchmark conditions. Normalize or
   footnote where they differ.
5. Write a one-line verdict per option: its standout strength and its main
   liability, from the matrix rows.
6. Recommend: pick the option that best satisfies the user's stated constraints —
   walk the hard constraints first (eliminate options that fail one), then rank the
   survivors on the priority criteria. If two remain close, say what single
   question would break the tie instead of forcing a pick.
7. Present the result (format below). Offer `write_file` to save the matrix as
   markdown in the working folder, and `deliver` if the user names a channel.

## Output format

```
## Comparison: <Option A> vs <Option B> [vs ...] — <YYYY-MM-DD>
Criteria agreed with you: <list, hard constraints marked *>

| Criterion | <Option A> | <Option B> | <Option C> |
|---|---|---|---|
| <criterion>* | <value> [1] | <value> [2] | unknown [—] |

**Verdicts**
- <Option A>: <one line — strongest for X, weak on Y>

**Recommendation**: <option> — because <ties directly to the user's constraints>.
Runner-up: <option>, if <the condition that would flip the choice>.

Sources: 1. <url> — <what it covered> ...
```

## Rules

- Never present a matrix built on criteria the user hasn't seen: agreement (or
  their own explicit list) comes first.
- Every filled cell traces to a fetched page; official vendor claims are fine for
  features but flag cells where ONLY the vendor's own material supports a
  superiority claim.
- "unknown" is an acceptable cell value; a fabricated one is not. If a whole
  criterion is mostly unknown, say the comparison is weak on that axis.
- The recommendation must cite the user's constraints, not generic popularity; with
  no constraints given and none obtainable, present verdicts only and say a
  recommendation needs their constraints.
- Keep the matrix scannable: cells ≤ ~8 words; details go in verdicts or footnotes.
- Egress is search/fetch plus the user's named delivery target only.
