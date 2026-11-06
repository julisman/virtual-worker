---
name: Follow Up Tracker
description: Capture commitments ("X promised Y by DATE") into persistent memory with absolute dates, list open follow-ups on request, set one-time reminders for dated ones via schedule_create, and mark them done.
when_to_use: When the user mentions someone promised/owes something, asks "what am I waiting on", wants a reminder to chase someone, or reports a follow-up as resolved.
version: 1.0.0
---

# Follow Up Tracker

Keep a reliable ledger of who owes what by when — in memory, with reminders — and retire entries when they're done.

## Inputs

- **A commitment** — who, what, and (optionally) by when. From the user's message, or spotted in text they paste (confirm before capturing spotted ones).
- **Or a query** — "what's open?", "what does Rina owe me?".
- **Or a resolution** — "Rina sent the deck, close it".

## Steps

**Capture (new commitment):**
1. Extract: **who** (person/team), **what** (the deliverable), **due** (convert relative dates to absolute `YYYY-MM-DD` via the session clock; no date → record `no date`). Ambiguous phrasing → ask one clarifying question, don't guess.
2. `memory_search("follow-up <who>")` first — if the same commitment is already tracked, say so and update instead of duplicating.
3. `memory_write` one entry in this exact shape so future searches are reliable:
   `FOLLOW-UP OPEN | who: <name> | what: <deliverable> | due: <YYYY-MM-DD|no date> | captured: <YYYY-MM-DD>`
4. If it has a due date, offer a reminder. On yes: `schedule_create` a **one-time** run on the due date (default 09:00) whose prompt is self-contained, e.g. "Check follow-up: <who> promised <what>, due today (<date>). Ask the user if it arrived; if yes, mark the memory entry done." Confirm the created schedule to the user.

**List (query):**
5. `memory_search("FOLLOW-UP OPEN")` (add the person's name when the query names one). Present the open list in the output format, overdue first. Note: memory search is fuzzy — also scan results for entries missing the exact prefix.

**Resolve:**
6. Find the entry via `memory_search`, then rewrite it: `memory_write` the same line with `OPEN` → `DONE | resolved: <YYYY-MM-DD>` and `memory_forget` the old OPEN entry if the memory tools support targeted forget; otherwise write the DONE line and note the supersession inside it. Delete its pending reminder with `schedule_delete` (find it via `schedule_list`).

## Output format

Capture: `✅ Tracking: <who> → <what>, due <date>. Reminder set for <date> 09:00.` (or "no reminder").
List:
```
🔁 Open follow-ups (<n>)
⚠️ OVERDUE
- <who>: <what> — was due <YYYY-MM-DD> (<d> days)
Upcoming
- <who>: <what> — due <YYYY-MM-DD>
No date
- <who>: <what> — captured <YYYY-MM-DD>
```
Resolve: `✔️ Closed: <who> → <what>. Reminder removed.`

## Rules

- **Absolute dates only** in memory — "by Friday" written verbatim becomes meaningless next week.
- One commitment per memory entry; never batch several people into one line.
- Reminders are one-time schedules, not cron — this tracker must not accumulate recurring jobs. Verify with `schedule_list` after creating; if `schedule_create` is unavailable, say reminders are off and track in memory only.
- Never message the person who owes the thing — reminders go to the user; chasing externally happens only if the user explicitly asks (and via a channel they name).
- When listing, if memory returns nothing, say "no open follow-ups tracked" — do not invent history.
