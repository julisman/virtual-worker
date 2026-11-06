---
name: Memory Keeper
description: Memory hygiene — decide what deserves memory_write (stable facts with absolute dates), dedupe via memory_search before writing, update instead of duplicating, and memory_forget wrong or stale facts after corrections.
when_to_use: When you learn a stable fact worth keeping (ID, email, preference, correction, recurring context), when the user says "remember/forget this", or when the user corrects something you previously stored.
version: 1.0.0
---

# Memory Keeper

Keep the agent's persistent memory small, current, and correct: write only durable facts, one copy each, and remove what is wrong.

## Inputs

- **Candidate fact(s)** — something just learned, or an explicit "remember X" / "forget X" / a user correction.
- No other inputs; this skill acts on the current conversation.

## Steps

1. Decide if the candidate deserves memory at all. Test: *will this still be true and useful in a month, in a conversation with no context from today?* If unsure, consult `load_skill_resource({"slug": "memory-keeper", "path": "references/what-to-remember.md"})` for the save / don't-save lists and phrasing patterns.
2. **Dedupe before every write.** `memory_search` for the fact's subject (the person, ID, project, or preference name). Three outcomes:
   - **Identical fact already stored** → write nothing; say it's already remembered.
   - **Related but outdated/conflicting fact found** → this is an update: `memory_forget` the old entry, then `memory_write` the new one. Never leave both.
   - **Nothing found** → `memory_write` the new fact.
3. Phrase the entry self-contained and searchable (patterns in the reference file): subject first, then the fact, then a `| <YYYY-MM-DD>` date stamp. Convert every relative time to an **absolute date** — never store "tomorrow", "next week", "recently".
4. On a **user correction** ("no, it's actually…", "that's the old one"): treat it as mandatory hygiene — `memory_search` for what you had, `memory_forget` the wrong entry, `memory_write` the corrected fact, and confirm both actions to the user.
5. On an explicit "forget X": `memory_search` for it, show the matching entries, and `memory_forget` the ones the user means (confirm first if more than one matches).
6. Batch case (end of a task with several new facts): run steps 1–3 per fact; skip the chaff rather than storing a summary blob.

## Output format

One short line per action, grouped:

```
Memory updated:
  + saved:   <entry as written>
  ~ updated: <old> → <new>
  - forgot:  <removed entry>
  · skipped: <candidate> (already stored | not durable)
```

Say "Nothing worth remembering from this" when that is the honest outcome.

## Rules

- Stable facts only: IDs, emails, names, preferences, corrections, recurring context, decisions. Never store secrets (passwords, tokens, OTPs), transient state ("build is broken"), or whole-conversation summaries — full lists in the reference file.
- Absolute dates always; "tomorrow" stored today is a lie next week.
- One fact = one entry. Never append a contradicting fact next to an old one — forget-then-write.
- Corrections outrank convenience: a wrong stored fact is worse than no fact; when the user corrects you, fixing memory is part of the task, not optional.
- Do not memory_write speculatively about people (inferred opinions, health, personal matters the user didn't ask you to keep).
- When `memory_search` returns near-duplicates you created earlier, consolidate: keep the best-phrased one, forget the rest.
