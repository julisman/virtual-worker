---
name: Brain Dump To Tasks
description: Parse a freeform brain-dump (rambling text, voice-note transcript, bullet soup) into discrete tasks with short titles, priorities, and due dates, dedupe against the existing board, and create them via task_create after the user approves the parsed list.
when_to_use: When the user dumps an unstructured list of things on their mind and wants it turned into task-board items — "get this out of my head", "make tasks from this", a pasted to-do braindump.
version: 1.0.0
---

# Brain Dump To Tasks

Turn one messy brain-dump into a clean, deduplicated set of task-board items — created only after the user approves the parse.

## Inputs

- **The dump** — pasted text, or a file in the working folder (`read_file`). This is the only required input.
- **Default priority** — `med` unless the dump signals urgency per item.

## Steps

1. **Split.** Break the dump into candidate tasks: one actionable outcome per task. Split "and"-chained items; merge restatements of the same thing. Ignore pure venting/notes that contain no action, but list them at the bottom as "not tasks" so the user sees nothing was silently dropped.
2. **Normalize each candidate:**
   - **Title** — verb-first, ≤ 60 characters ("Book dentist appointment", not "I really need to sort out the dentist thing").
   - **Priority** — `high` if the dump says urgent/asap/blocking or has a near deadline; `low` if "someday/maybe/eventually"; else `med`.
   - **Due date** — only if the dump states one; convert relative phrases ("Friday", "end of month") to absolute `YYYY-MM-DD` using the session clock. No stated date → no due date. Never invent one.
   - **Description** — the original phrasing from the dump, so context survives the short title.
3. **Dedupe.** Call `task_list` and compare candidates against existing open tasks (title similarity + same deliverable in description). Mark each duplicate as `skip (exists: #<id>)` — or `update #<id>` if the dump adds a new due date/priority to an existing task. If the task board is unavailable on this agent, say so and offer the parsed list as a markdown checklist via `write_file` instead.
4. **Show the parsed list** in the output format below. Do not create anything yet.
5. **Confirm & create.** The user approves all, picks numbers, or edits lines. Then call `task_create` once per approved new task (title, description, priority, due date) and `task_update` for approved updates. Report created IDs. On a failed call, report it and continue.

## Output format

```
🧠 Parsed <n> items from your dump

| # | Title (≤60) | Prio | Due | Action |
|---|-------------|------|-----|--------|
| 1 | <title> | high | 2026-07-25 | create |
| 2 | <title> | med | – | create |
| 3 | <title> | med | – | skip (exists: #<id>) |

Not tasks (kept out): <brief list or "—">
Create the "create" rows? (all / numbers / edit)
```

After creation: `Created: #<id> <title>` per task, one line each.

## Rules

- **No silent creation.** `task_create` runs only after explicit approval of the shown list, and only for the shown rows.
- Preserve the user's language in titles (Indonesian dump → Indonesian titles).
- Ambiguous items ("mom???") become a task only with a clarifying title guess marked `(?)` — the user fixes it at the confirm step; when truly unparseable, put it under "not tasks".
- One dump = one pass. If the user adds more items mid-flow, re-show the full updated list before creating.
- No egress, no delivery, no memory writes — the board (and chat) is the only output. Use `memory_write` only if the user explicitly says "remember" about an item instead of making it a task.
