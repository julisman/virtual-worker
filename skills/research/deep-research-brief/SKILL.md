---
name: Deep Research Brief
description: Multi-round research on one question — decompose into sub-questions, search and fetch the best sources for each, and synthesize a cited brief with per-claim confidence notes and an open-questions section.
when_to_use: When the user asks a substantial question that needs real research across multiple sources ("research X for me", "give me a brief on Y", "what's the state of Z"), not a quick lookup or a news roundup.
version: 1.0.0
---

# Deep Research Brief

Answer one research question properly: break it into sub-questions, research each
against multiple fetched sources, and synthesize a cited brief with confidence notes.

## Inputs

- **question** — the single question to research. If the user gave several, pick the
  main one and confirm; one brief = one question.
- **depth** — default 3-5 sub-questions, 2-3 fetched sources each. Honor "quick"
  (2 sub-questions) or "thorough" (up to 6) if stated.
- **audience/purpose** — optional; shapes tone and what to emphasize.

## Steps

1. Restate the question in one line and decompose it into 3-5 sub-questions that
   together cover it (definitions/status quo, evidence, counterpoints, implications).
   Show the decomposition to the user; adjust if they object, otherwise proceed.
2. For each sub-question, run 1-2 `browser_search` queries. From the results pick
   the 2-3 most authoritative, non-duplicate sources (prefer primary documents,
   official pages, established outlets over aggregators).
3. `browser_fetch` each picked source. Extract the specific facts that bear on the
   sub-question, keeping the URL attached to each fact. If a fetch fails, substitute
   the next-best result.
4. After the first pass, list what is still unknown or contradictory. Run ONE more
   targeted round of `browser_search` + `browser_fetch` on those gaps. Stop after
   this round — remaining gaps become Open questions.
5. Synthesize the brief using the template in
   `load_skill_resource({"slug": "deep-research-brief", "path": "references/brief-template.md"})`.
   Every claim carries a citation `[n]` and every finding a confidence note
   (High / Medium / Low, with the reason: source count, source quality, agreement).
6. Where fetched sources disagree, present both positions with their citations —
   never silently pick a side.
7. Deliver in chat. Only if the user asked for a channel, send the full brief via
   `deliver(channel, target, text)`. Offer `write_file` to save it as markdown in
   the working folder if the user wants a copy.

## Output format

Exactly the structure in `references/brief-template.md`: title + date + question,
"Bottom line" (2-3 sentences), findings grouped by sub-question with inline `[n]`
citations and a confidence note each, an "Open questions" section, and a numbered
source list with URLs and one-line descriptions.

## Rules

- Cite only pages you actually fetched with `browser_fetch`; search-result snippets
  alone never support a claim. Every `[n]` must resolve to a listed URL.
- Confidence notes are mandatory per finding — a brief without them is incomplete.
- Distinguish fact from inference: mark your own synthesis as "Assessment:" so it
  can't be mistaken for a sourced claim.
- If the question is ambiguous (which market? which timeframe?), ask ONE clarifying
  question before researching; don't guess a scope.
- Cap effort: at most 2 research rounds and ~15 fetches total. If that's not enough,
  say what a deeper pass would need instead of looping.
- If `kb_search` is available and the topic plausibly overlaps attached knowledge,
  query it once and cite KB hits as `[KB]` alongside web sources; skip silently if
  no knowledge base is attached.
- Egress is limited to search/fetch and the user's named delivery target.
- Write the brief in the user's language; keep quoted source text in its original
  language with a translation.
