---
name: Standup Reporter
description: Compose the user's daily standup — yesterday / today / blockers — from task-board activity and memory, then deliver it to the team channel (chat channel if connected, else in-app).
when_to_use: When the user asks for their standup, daily status update, "post my standup", or a scheduled run fires asking for the daily standup report.
version: 1.0.0
---

# Standup Reporter

Assemble an honest yesterday/today/blockers standup from real activity and post it to the team channel.

## Inputs

- **Reporting window** — "yesterday" = the previous working day (on Monday, that's Friday). Resolve to absolute dates from the session clock.
- **Extras** — anything the user tells you to include verbatim ("mention the demo went well").
- **Target channel** — `memory_search("standup channel OR standup target")`. If nothing stored, ask once where standups go and `memory_write` the answer.

## Steps

1. **Yesterday.** Call `task_list` and collect items whose status changed in the window — completed, moved forward, or commented on. `memory_search("<yesterday's date>")` for work noted outside the board. If the task board is unavailable, build this section only from memory + what the user tells you.
2. **Today.** From `task_list`: in-progress and top-priority open items assigned to/claimed by this agent or the user. Cap at 3-4 items — a standup states intent, not the whole backlog.
3. **Blockers.** Look for: overdue tasks, tasks with comments indicating waiting ("waiting on", "blocked"), and `memory_search("blocked OR waiting on")` entries still open. No blockers → write "None".
4. **Compose** in the output format. Keep every line short and factual; include the user's extras verbatim.
5. **Show first when interactive.** If the user asked in chat, show the draft and send only after they approve. If running from a schedule, skip the approval and send directly — the schedule itself is the standing approval.
6. **Deliver.**
   - If this agent has a connected chat channel and a known target: `channel_send` to the stored group/user (alternatively `deliver("channel", target, text)`).
   - Else: `deliver("in_app", <user>, text)` so it lands as an in-app notification.
   - Never guess a chat group id — only use one from memory or the user's message; if absent, fall back to in-app and say why.

## Output format

```
🧍 Standup — <Name>, <YYYY-MM-DD>

Yesterday
- <done item> ✅
- <progressed item> (in progress)

Today
- <planned item>
- <planned item>

Blockers
- <blocker — who/what it waits on>   (or "None")
```

Max ~12 lines. Match the team's language (Indonesian teams get an Indonesian standup).

## Rules

- **Never fabricate progress.** Only report what the task board, memory, or the user actually shows. An empty yesterday is reported as "— (no tracked activity)" — not padded.
- Don't leak private detail into a team channel: task titles yes, long internal notes no.
- The only egress is the one delivery to the named team target. One send per day — if asked to re-send, edit and state it replaces the earlier one.
- If both chat channel and the task board are unavailable, still compose the standup in chat from what the user tells you — the skill degrades to a formatter.
- After a successful send, `memory_write` a one-liner: "standup <date> sent to <target>" so tomorrow's run can spot a double-send.
