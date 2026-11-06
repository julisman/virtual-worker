---
name: Weekly Review
description: Friday retrospective in one structured report — completed vs planned from the task board, notable memory entries from the week, wins and risks, and proposed priorities for next week.
when_to_use: When the user asks for a weekly review, week recap, "how did this week go", Friday retro, or a scheduled weekly-review run fires.
version: 1.0.0
---

# Weekly Review

Build the week's retrospective — what got done vs planned, what it means, what's next — as one structured report.

## Inputs

- **Week window** — default: Monday through today of the current week (resolve absolute dates from the session clock; state the range in the title). The user may ask for a different week.
- **Delivery** — default: show in chat. Use a stored channel from `memory_search("weekly review channel")` if one exists, or one the user names.

## Steps

1. **Board data.** Call `task_list` (include recently done items). Bucket by the week window using item timestamps — compute, don't eyeball:
   - **Completed** — done within the window.
   - **Planned-but-open** — items that were due this week, or in progress at week start, and still open. Count carried-over vs newly added work if timestamps allow.
   If the task board is unavailable, build the review from memory + the user's own account, and say the board data is missing.
2. **Last week's plan.** `memory_search("week summary OR next-week priorities")` for the previous review's stored summary — comparing this week's completions against what was *planned* is the heart of the retro. No stored plan → compare against due dates only and note it.
3. **Memory highlights.** `memory_search` for the week's dates and for "decision OR win OR blocked OR follow-up". Keep entries dated inside the window; pick the 3-6 that mattered.
4. **Wins & risks.** Derive from the data — wins: shipped items, closed follow-ups, unblocked work. Risks: overdue items, repeatedly-carried tasks, open blockers, looming due dates next week. Each win/risk cites its source item; no vibes-only entries.
5. **Next-week priorities.** Propose 3-5, drawn from: overdue + due-next-week tasks, open follow-ups, and anything the user flagged during the week. Mark them clearly as proposals for the user to edit.
6. **Compose.** Fill the template — `load_skill_resource({"slug": "weekly-review", "path": "references/weekly-review-template.md"})`. Numbers must match the data (completed count = items listed).
7. **Deliver.** Show in chat; if a delivery channel is set, also `deliver(channel, target, text)`. If the user confirms the next-week priorities, offer to `task_update` due dates/priorities accordingly (only after confirmation) and `memory_write` a one-line week summary: "week <range>: <n> done, <n> carried, top focus next: <x>".

## Output format

Exactly the structure in `references/weekly-review-template.md`:
- title with the absolute date range;
- `📊 The numbers` (counts that agree with the lists below);
- `✅ Completed`, `📦 Carried over` (task IDs + titles);
- `🌟 Wins`, `⚠️ Risks` (each citing its source), `🧭 Next week` (proposals).
Keep it under ~50 lines; reply in the user's language.

## Rules

- **Numbers over narrative.** Every claim ties to a task, memory entry, or the user's own words — a review that flatters is worse than none.
- Carried-over work is stated plainly, not hidden; a task carried ≥ 2 weeks gets an explicit callout.
- Do not turn the review into a grooming session — link to the Task Board Groomer instead of proposing board edits beyond the confirmed next-week priorities.
- Egress is limited to the single optional delivery target. Nothing else leaves the machine.
- Wins and risks max 4 items each — a retro that lists everything ranks nothing.
- Run mid-week only if asked; note the partial window in the title ("through Wednesday").
- If the week has almost no data (new agent, empty board), produce a short honest review and suggest what to start tracking rather than padding sections.
- For a standing Friday review, set it up once with `schedule_create` (weekly cron, self-contained prompt invoking this skill) and verify via `schedule_list` — don't rely on remembering.
