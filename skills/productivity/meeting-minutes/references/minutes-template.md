# Meeting minutes — output template

Fill exactly. Replace `<...>`; drop a section only if truly empty (keep Action
items even when empty, with "— none recorded").

```
# Minutes — <Meeting title>
**Date:** <YYYY-MM-DD>
**Attendees:** <Name>, <Name>, … <!-- alphabetical; append "(partial)" if someone joined late/left early and the transcript says so -->

## Decisions
1. <Decision as a complete statement, e.g. "Ship v2 behind a feature flag on 2026-08-01.">
2. …
<!-- none: "— no decisions recorded" -->

## Action items
| # | Action | Owner | Due |
|---|--------|-------|-----|
| 1 | <verb-first action, ≤ 100 chars> | <Name or "unassigned"> | <YYYY-MM-DD or "no date"> |
<!-- none: "— none recorded" -->

## Discussion summary
- <3-6 bullets covering the main threads, in meeting order>

## Open questions
- <question> — parked for <who/when, if stated>
<!-- none: omit the section -->
```

Rules:
- Actions start with a verb ("Send", "Review", "Draft"), one action per row —
  split "X and Y" commitments into two rows with the same owner.
- Dates always absolute (`YYYY-MM-DD`), resolved against the meeting date.
- Flag rows needing attention: append ⚠️ to `unassigned` owners and past-due dates.
