---
name: Recurring Digest Setup
description: Wizard for setting up any recurring scheduled digest (e.g. "daily 10am AI trends to my chat channel group with links") — clarify topic/time/channel/target, create the schedule with a self-contained prompt, verify it, and save the target for reuse.
when_to_use: When the user asks for a recurring digest, briefing, report, or news roundup on a schedule — "every day at X send me Y", "weekly summary of Z to my group".
version: 1.0.0
---

# Recurring Digest Setup

Turn "send me X every day at Y" into a verified `schedule_create` with a prompt good enough to run unattended.

## Steps

1. **Collect the four facts.** From the request, then ask for whatever is missing (one compact question covering all gaps):
   - **Topic** — what the digest covers, specific enough to search ("AI agent frameworks news", not "tech").
   - **Schedule** — time(s) + days. Convert to a cron expression in the user's local time; confirm it in words ("daily at 10:00", "Mon/Wed/Fri 08:30").
   - **Channel** — `channel`, `in_app`, `desktop`, `webhook`, or `obsidian`.
   - **Format wishes** — length, links (default: include source links), language.
2. **Resolve the exact target — never guess IDs.** For `channel`/`webhook`/`obsidian`:
   - First `memory_search("<channel> target OR group id OR <group name>")`.
   - chat channel with a connected bot: confirm a remembered ID with the user, or look the group/user up with `memory_search` / `channel_contacts`. No connected chat channel → say so (connect it in the agent's settings) and offer `in_app` instead.
   - Webhook: the user must paste the URL; obsidian: confirm the vault/note path.
   - If memory has nothing and the user can't provide the ID, STOP the setup — a digest to a guessed target is a data leak.
3. **Draft the digest prompt.** It must be self-contained — the scheduled run has no chat context. Include: the topic; the exact steps (typically `browser_search` the topic, pick the top N fresh items, dedupe, format with links); the output format (title with the date, N bullets `- <title> — <source> (<url>)`); the delivery instruction (`deliver("<channel>", "<exact target>", text)` — or `channel_send` for chat channel); the language; and a failure rule ("if search returns nothing fresh, deliver a short 'no news today' note — never fail silently").
4. **Show the plan** (format below) and get an explicit "yes" before creating.
5. **Create.** Call `schedule_create` with the cron expression, a clear name ("Daily AI trends digest — 10:00 → chat channel <group>"), and the drafted prompt.
6. **Verify.** Call `schedule_list` and confirm the new entry exists with the right cron and name. Show it to the user. If it's missing or wrong, fix or `schedule_delete` + recreate — never leave a half-configured schedule.
7. **Remember.** `memory_write`: `DIGEST TARGET | channel: <channel> | target: <exact id> | name: <human name> | digest: <topic> | cron: <expr> | created: <YYYY-MM-DD>` so future setups and edits find it. Offer a test run now ("want me to run one digest immediately so you can check the format?") — on yes, execute the digest prompt once manually.

## Output format

Plan (step 4):
```
📰 Digest plan
- Topic: <topic>
- Schedule: <cron> (= <plain words, local time>)
- Deliver: <channel> → <exact target name + id>
- Format: <n> items, links included, <language>
Prompt the schedule will run:
> <the full self-contained prompt>
Create it?
```
After verification: `✅ Scheduled: <name> — next runs per <cron>. Target saved to memory.`

## Rules

- **Never guess a group/target ID.** Memory, a lookup tool, or the user — those are the only three sources. No target → no schedule.
- One digest = one schedule. Editing an existing digest means `schedule_list` → change → verify, not stacking a second job.
- The scheduled prompt must name concrete tools that exist and an exact target — pronouns like "my group" are bugs.
- Recurring egress is a standing commitment: restate clearly what will leave the machine, where, how often, before creating.
- If `schedule_create`/`schedule_list` are unavailable on this agent, stop and tell the user scheduling is disabled — do not simulate a schedule.
