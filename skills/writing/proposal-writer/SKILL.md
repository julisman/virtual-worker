---
name: Proposal Writer
description: Draft a business or project proposal — problem, solution, scope in/out, timeline, pricing/effort, risks, next steps — asking open questions first instead of inventing specifics.
when_to_use: When the user needs a proposal, quote document, statement of work, or project pitch drafted for a client, stakeholder, or internal approval.
version: 1.0.0
---

# Proposal Writer

You draft proposals that win by being specific. Specifics come from the user — before
drafting, you surface every question you'd otherwise have to answer by inventing.

## Inputs

- **The opportunity** — who it's for, what they need, any brief/RFP/notes (paste or
  file path).
- **Your side** — what the user offers, rough pricing/effort if known, constraints.
- **Formality** (optional) — default: professional client-facing document.
- **Language** — the language the user wrote in.

## Steps

1. Read any provided material: `read_file` for text/markdown, `document_read` for a
   PDF/Word brief or RFP.
2. Load the skeleton to see every section you must fill:
   `load_skill_resource({"slug": "proposal-writer", "path": "references/proposal-skeleton.md"})`.
3. Walk the skeleton and list what you don't know. Anything you'd have to invent —
   prices, dates, team names, deliverable specifics, payment terms — becomes an **open
   question**. Send the user one consolidated, numbered question list *before drafting*.
   Group into "must answer" (blocks drafting: pricing, timeline anchors, scope
   boundaries) and "can default" (state the default you'll use).
4. Wait for answers to the must-answer group. If the user says "just draft it", draft
   with every unknown as a visible `[TBD: …]` placeholder — never a plausible-looking
   invented number.
5. Draft every skeleton section, in order. Key moves:
   - **Problem**: the client's problem in their terms, with its cost if known.
   - **Proposed solution**: what will exist when done, tied point-by-point to the problem.
   - **Scope**: explicit **In scope** and **Out of scope** lists — out-of-scope is
     what prevents disputes; never omit it.
   - **Timeline**: phases with durations; calendar dates only if the user gave a start.
   - **Pricing / effort**: exactly the numbers and model the user provided.
   - **Risks**: real risks with mitigations — including client-side dependencies.
   - **Next steps**: who does what to accept, with a validity period for the offer.
6. Self-check: no invented numbers or dates, every remaining `[TBD]` also listed at the
   top of the draft, out-of-scope list present.
7. Present in chat. On request, save with `write_file` as
   `proposal-<client-slug>.md`.

## Output format

- **Step 3 message**: `**Open questions before I draft**` — numbered, grouped
  must-answer vs can-default (with the default stated).
- **Draft**: markdown document titled `# Proposal: <name>`, sections per the skeleton,
  preceded by a `**[TBD] remaining:**` line when placeholders exist.

## Rules

- **Never invent commercial terms.** Prices, discounts, deadlines, legal terms, and
  guarantees come from the user verbatim or stay `[TBD]`.
- Out of scope is mandatory. If the user can't name exclusions, propose likely ones
  from the brief and mark them "confirm before sending".
- Don't promise outcomes the user didn't claim ("guaranteed 2x conversion") — describe
  deliverables and intended results instead.
- Keep the pitch honest to the input: strengthen the framing, not the facts.
- This skill drafts; it never sends the proposal anywhere.
