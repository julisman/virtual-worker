---
name: Calendar Day Planner
description: Turn today's or tomorrow's calendar (via connected Calendar MCP tools) into a time-blocked day plan — prep notes per meeting, focus blocks in the gaps, buffers, plus conflict and back-to-back warnings.
when_to_use: When the user asks to plan their day, "what does tomorrow look like", block focus time, or check for meeting conflicts.
version: 1.0.0
---

# Calendar Day Planner

Read one day's events and produce a time-blocked plan the user can follow hour by hour.

## Inputs

- **Day** — "today" or "tomorrow" (default: today before 12:00 local, tomorrow after). Resolve to a concrete `YYYY-MM-DD` using the session clock and confirm it in the output title.
- **Working hours** — default 09:00-18:00; use `memory_search("working hours")` first, ask only if the plan looks wrong without it.
- **Top priority** — optionally ask "what's the one thing that must get done?"; if the user already said it, use that for the first focus block.

## Steps

1. **Check the tool.** If the agent has connected Google Calendar MCP tools, fetch all events for the chosen day (title, start, end, attendees, location/link, description). If no calendar MCP is attached, say "No Calendar MCP connected — add one in Agents → MCP" and offer to build a plan from events the user pastes instead.
2. **Normalize.** Sort events by start time. Drop all-day informational events (holidays, birthdays) into a one-line note. Treat events the user declined as absent.
3. **Detect problems.**
   - **Conflict**: two events overlap in time → flag both, suggest which to move (prefer moving the one with fewer attendees or no external people).
   - **Back-to-back**: gap < 10 min between consecutive meetings → flag; suggest shaving 5 min or adding buffer.
   - **Marathon**: ≥ 3 consecutive hours of meetings → flag and suggest a break.
4. **Prep notes.** For each meeting write one prep line from its title/description/attendees (e.g. "1:1 with Sari — review last week's action items"). If context is thin, check `memory_search("<meeting title or attendee>")` for prior notes. Never invent agenda details — write "no agenda listed" when there is none.
5. **Fill the gaps.** Turn gaps ≥ 45 min inside working hours into focus blocks; assign the user's top priority to the largest morning block. Gaps of 15-45 min become "shallow work / email" blocks. Insert a lunch block near midday if free.
6. **Present.** Output the plan in the format below. If the user asks to remember working hours or a standing priority, `memory_write` it.

## Output format

```
🗓 Plan — <Weekday> <YYYY-MM-DD>

⚠️ Watch out
- <conflict/back-to-back/marathon warnings, or "— clean day, no conflicts">

09:00–09:30  🧘 Settle in / inbox
09:30–11:00  🎯 Focus: <top priority>
11:00–11:30  📞 <Meeting> — prep: <one line>
...

Unscheduled but due: <tasks/follow-ups the user mentioned, if any>
```

One line per block, chronological, whole day covered inside working hours. Keep prep lines to one sentence.

## Rules

- **Read-only calendar.** Never create, move, or decline events — only suggest changes; the user acts on them (or explicitly asks you to, if the MCP has write tools, and you confirm the exact change first).
- Never guess timezones: use event times as returned; if an event carries a different timezone, show both.
- Focus blocks are suggestions — mark them clearly as yours, not as existing calendar entries.
- If the day has zero events, still produce a plan: 2-3 focus blocks from the user's priorities plus breaks.
- No egress: this skill uses only the calendar MCP and chat. Do not `browser_search` attendees or companies unless the user asks for meeting research.
