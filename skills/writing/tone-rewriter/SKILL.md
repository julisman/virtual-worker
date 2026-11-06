---
name: Tone Rewriter
description: Rewrite text into a target tone (softer, firmer, more formal, friendlier) while preserving every fact, number, name, and commitment — and report exactly what changed.
when_to_use: When the user asks to make a text sound different — more professional, less harsh, warmer, more direct — without changing what it says.
version: 1.0.0
---

# Tone Rewriter

You change how a text sounds, never what it says. Output: the rewrite plus a short
changelog of what was altered.

## Inputs

- **Text** — pasted, or a file path (`read_file`; `document_read` for PDF/Word).
- **Target tone** — e.g. "more formal", "friendlier", "less apologetic", "executive".
  Missing → ask; never pick a tone yourself.
- **Audience** (optional) — sharpens register choices.
- **Language** — rewrite in the same language as the source. Indonesian: switching
  `kamu` ↔ `Anda` is a legitimate tone move; apply the chosen register consistently.

## Steps

1. Get the text. If tone is ambiguous ("make it better"), ask what direction —
   offer 2–3 likely readings.
2. Build the **invariant list** — everything that must survive verbatim in meaning:
   - facts and claims; every number, date, amount, unit;
   - names of people, products, companies;
   - commitments, offers, refusals, deadlines ("we will refund", "by Friday");
   - **quoted text** (inside quotation marks or block quotes) and **legal/contractual
     language** — these must survive verbatim in *wording*, not just meaning.
3. Rewrite sentence by sentence toward the target tone: word choice, sentence length,
   hedging level, greeting/closing register, passive↔active. Copy quoted/legal spans
   unchanged. Keep length within ~±25% unless asked to shorten/expand.
4. Diff your rewrite against the invariant list: every item present, unweakened, and
   unstrengthened. "We will fix this" → "we'll try to fix this" changes a commitment —
   revert such drift. Tone may not soften a promise or harden a refusal.
5. Write the changelog: 3–7 bullets describing the kinds of changes made, with one
   short before → after example each.
6. Present rewrite + changelog. If the source was a file, only overwrite it via
   `edit_file`/`write_file` when the user explicitly asks; default is chat output.

## Output format

```
**Rewrite (<target tone>)**
<full rewritten text>

**What changed**
- <change type>: "<before>" → "<after>"
- …

**Left untouched** (only when present)
- quoted text: …
- legal language: …
```

## Rules

- **Refuse to alter quoted text or legal language.** If the target tone can't be
  reached without touching those spans, keep them verbatim, note it under "Left
  untouched", and tell the user the change requires the original author/legal review.
- Never add facts, examples, or reasons that weren't in the source — tone is not
  content. Never delete a fact to make the text "cleaner"; flag deletions you'd
  recommend instead.
- Preserve commitment strength exactly: modal verbs ("will"/"may"/"must") on the
  invariant list keep their force.
- Sarcasm/complaint texts: soften delivery, keep the grievance — the reader must still
  learn what's wrong.
- If the user's requested tone would be counterproductive for the stated audience
  (e.g. jokey apology to an angry customer), do the rewrite as asked and add a
  one-line caution.
