---
name: Task Board Groomer
description: Audit the intern task board — flag stale items (>7 days untouched), likely duplicates, and missing priorities/due dates — then propose a change list and apply it via task_update/task_comment only after the user confirms.
when_to_use: When the user asks to groom, clean up, tidy, or audit the task board or backlog, or complains the board is a mess.
version: 1.0.0
---

# Task Board Groomer

Audit the task board, propose a concrete grooming plan, and apply only the confirmed changes.

## Inputs

- **Scope** — default: all non-done tasks from `task_list`. The user may narrow ("only high priority", "only mine").
- **Staleness threshold** — default 7 days without any update/comment; user may override.

## Steps

1. **Fetch.** Call `task_list` and note today's date from the session clock. If task-board tools are unavailable on this agent, say the board requires the intern role and stop.
2. **Analyze** each open task:
   - **Stale**: last update/activity older than the threshold. Compute days from timestamps — never eyeball.
   - **Duplicate**: pairs with near-identical titles or clearly the same deliverable in the description. List as pairs `A ≈ B` with the reason; propose keeping the older/richer one and closing the other.
   - **Missing metadata**: no priority, no due date on a task that plainly needs one, or an empty description on a non-trivial task.
   - **Overdue**: due date in the past but not done.
3. **Propose.** Output the findings + a numbered change list (format below). Every proposed change names the exact task and the exact call you would make (`task_update` field change, or `task_comment` text). Propose — do not touch the board yet.
4. **Confirm.** Ask the user to approve: "all", specific numbers ("1, 3, 5"), or edits. Silence is not approval.
5. **Apply** only the approved changes: `task_update` for status/priority/due-date changes; `task_comment` for nudges on stale tasks (e.g. "Groomed <date>: still relevant? No activity for 12 days."). For duplicates, comment-and-link on the survivor and mark the duplicate per the user's instruction (close/keep). Report each applied change with its task ID; if a call fails, report it and continue with the rest.
6. **Record.** `memory_write` a one-liner: "board groomed <YYYY-MM-DD>: <n> changes" so the next grooming can compare.

## Output format

```
🧹 Board audit — <YYYY-MM-DD> (<n> open tasks)

Stale (> <t> days): <n>
- #<id> <title> — <d> days idle
Duplicates: <n> pairs
- #<idA> ≈ #<idB> — <why>
Missing metadata: <n>
- #<id> <title> — no priority / no due date
Overdue: <n>
- #<id> <title> — due <YYYY-MM-DD>

Proposed changes
1. task_update #<id>: priority → high        (reason)
2. task_comment #<id>: "<comment text>"      (reason)
...
Approve all, pick numbers, or tell me what to change.
```

## Rules

- **Never delete or close a task without naming it in the proposal and getting explicit approval.** Prefer commenting over closing when intent is unclear.
- Never change task descriptions' content beyond what was approved — no silent rewording.
- Suggested priorities are heuristics (deadlines, blocking relationships mentioned in descriptions); mark them as suggestions, never as facts.
- A big board (>40 open tasks) is groomed in slices: worst offenders first, offer to continue.
- This skill makes no network calls and delivers nothing anywhere — board + chat only.
