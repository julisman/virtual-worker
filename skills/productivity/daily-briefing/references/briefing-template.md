# Daily briefing — output template

Fill this exactly. Replace `<...>` placeholders; delete instruction comments.
Omit the Follow-ups and Headlines sections entirely when empty; keep Calendar
and Tasks with a "— nothing today" note when empty.

```
☀️ Daily Briefing — <Weekday>, <YYYY-MM-DD>

📅 Today
- <HH:MM>–<HH:MM> <event title> (<attendee count> people)   <!-- one line per event, chronological -->
- …
<!-- if calendar MCP not connected: "Calendar: not connected — add one in Agents → MCP" -->
<!-- if no events: "— nothing today" -->

✅ Open tasks (<n>)
- [<priority>] <task title> — due <YYYY-MM-DD | no due date>   <!-- max 7; note "+<n> more" if truncated -->
- …
<!-- if no open tasks: "— nothing today" -->

🔁 Follow-ups
- <who> owes/expects <what> — due <YYYY-MM-DD> (<overdue?>)
- …

📰 Headlines
- <title> — <source> (<url>)     <!-- 3-5 total, freshest first -->
- …

Have a good one! 🦦
```

Formatting rules:
- Priorities shown as `[high]`, `[med]`, `[low]`.
- Mark overdue follow-ups with `⚠️ overdue`.
- Total length target: under 40 lines. Trim tasks before trimming calendar.
