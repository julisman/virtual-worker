---
name: Daily Briefing
description: Compose a single morning digest — today's calendar events, open task-board items, pending follow-ups from memory, and 3-5 headlines for the user's interests — and deliver it in one structured message.
when_to_use: When the user asks for their morning briefing, daily digest, "what's on today", or a scheduled run fires with a prompt asking for the daily briefing.
version: 1.0.0
---

# Daily Briefing

Build one structured morning digest for the user and deliver it — never four separate messages.

## Inputs

- **Date** — default: today (use the session clock; never guess the date).
- **Interests** — topics for headlines. Default: `memory_search("interests OR topics OR follows")`; if nothing found, ask once and `memory_write` the answer for next time.
- **Delivery channel** — default: `memory_search("briefing delivery channel")`; if nothing stored, show the briefing in chat and ask if they want it delivered somewhere from now on.
- **Headline count** — default 5 total; the user may change it ("just 3 headlines").
- **Knowledge base** — if this agent has a KB attached and the user's interests are internal topics, `kb_search` may replace `browser_search` for those topics.

## Steps

1. **Calendar.** If the agent has connected Google Calendar MCP tools, list today's events (title, start-end time, attendees). Sort chronologically; note the first meeting's start time — it caps how long the morning focus window is. If no calendar MCP is attached, skip the section and note "Calendar: not connected — add one in Agents → MCP".
2. **Tasks.** Call `task_list`. Keep open items (not done/archived); sort by priority then due date, cap at 7 lines (note "+n more"). If the task-board tools are unavailable (agent is not an intern), omit the section.
3. **Follow-ups.** Call `memory_search("follow-up OR promised OR waiting on OR due")`. Keep entries that are still open and dated today or overdue; mark overdue ones.
4. **Headlines.** For each interest topic (max 3 topics), call `browser_search("<topic> news")`. Pick 3-5 headlines total across topics:
   - prefer items from the last 24-48h;
   - dedupe near-identical stories (same event from two outlets = one line);
   - keep the source name and URL for every headline.
5. **Compose.** Fill the exact template — load it with `load_skill_resource({"slug": "daily-briefing", "path": "references/briefing-template.md"})`. Omit any section whose source produced nothing (write the section header with "— nothing today" only for Calendar and Tasks; drop empty Follow-ups/Headlines entirely).
6. **Deliver.** If a preferred channel is known, call `deliver(channel, target, text)` with the composed briefing. Otherwise print it in chat. Never deliver to a target you have not confirmed exists.

## Output format

Exactly the structure in `references/briefing-template.md`:
- a dated title line, then sections `📅 Today`, `✅ Open tasks`, `🔁 Follow-ups`, `📰 Headlines`;
- each headline as `- <title> — <source> (<url>)`;
- whole briefing under ~40 lines.
Reply in the user's language if they write in Indonesian, but keep the section structure identical.

## Rules

- **One message.** Gather everything first, then send once. Do not stream partial sections.
- **Degrade gracefully.** A missing tool (no calendar MCP, no task board, `browser_search` disabled) removes its section with a one-line note — it never blocks the rest of the briefing.
- **No stale news.** If `browser_search` returns only old or irrelevant results for a topic, say "no fresh headlines for <topic>" instead of padding with weak links.
- **Local-first.** The only egress is `browser_search` for headlines and the user's chosen delivery channel. Send the briefing nowhere else.
- **Absolute dates.** Write times as `HH:MM` and dates as `YYYY-MM-DD` — never "tomorrow" inside a delivered message.
- **Don't editorialize the news.** Headlines are reported as the source states them; add at most a 5-word context tag, never your own spin.
- If this ran from a schedule and a step fails, still deliver what you have and note the failure at the bottom; never fail silently.
- If the user wants this every morning, don't hand-run it daily — set it up once with `schedule_create` (a self-contained prompt that invokes this skill) and confirm via `schedule_list`.
