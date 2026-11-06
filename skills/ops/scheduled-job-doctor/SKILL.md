---
name: Scheduled Job Doctor
description: Diagnose and fix a misfiring scheduled run — inspect the schedule spec and last outcomes, find why it fails (bad cron, ambiguous prompt, missing tool/target), rewrite the prompt to be self-contained, and recreate the schedule.
when_to_use: When a scheduled job didn't run, ran at the wrong time, or ran but produced wrong/empty output, or when the user asks to review, fix, or clean up their schedules.
version: 1.0.0
---

# Scheduled Job Doctor

Find why a scheduled run misbehaves, then replace it with a schedule that runs itself correctly with zero chat context.

## Inputs

- **Which job** — the user names it, or you find it via `schedule_list`. Default: the job the user is complaining about; if ambiguous, list and ask.
- **User timezone** — ask if unknown; needed to talk about run times honestly.

## Steps

1. `schedule_list` and locate the job. Capture its spec verbatim: cron or one-time time, the prompt text, and any recorded last-run outcomes.
2. Diagnose against the common failure classes, in order:
   - **Bad cron** — invalid field, or a cron that means something else than intended (e.g. `0 9 * * 1` is Mondays, not daily; `* * * * *` is every minute). Expand the cron to plain words and compare with what the user wanted.
   - **Timezone drift** — the schedule fires in server time but the user reasons in their local time; compute both.
   - **Ambiguous prompt** — the prompt leans on chat context that a cold run doesn't have: "check the page again", "send it to him", "the usual report". A scheduled prompt must name its URL/target/format explicitly.
   - **Missing tool or target** — the prompt needs a tool the agent lacks (shell off, no chat channel, no MCP attached) or a delivery target that no longer resolves. Check the prompt's verbs against the agent's actual tools.
   - **One-time already fired** — a one-time schedule in the past will never run again; it needs recreating, not fixing.
3. Rewrite the prompt to be **self-contained**: every URL, target ID, file path, format, and delivery channel spelled out inside the prompt; absolute descriptions instead of "again/the usual"; an explicit "if X fails, report the error via deliver(...)" clause.
4. Show the user the diagnosis plus the before/after (old spec → new cron + new prompt) and get their OK.
5. On OK: `schedule_delete` the broken job, then `schedule_create` the fixed one. Delete-then-create, never leave both firing.
6. `schedule_list` again to verify the new job exists, and compute the next run time in the **user's timezone**; state it as an absolute date-time.

## Output format

```
Schedule doctor — <job name/id>
Diagnosis: <failure class> — <one-line specifics>
Old:  cron <expr> ("<in words>") · prompt: "<old prompt>"
New:  cron <expr> ("<in words>") · prompt: "<new prompt>"
Applied: deleted old, created new. Next run: <weekday YYYY-MM-DD HH:MM> (<user tz>).
```

Before user approval, end at the `New:` line with "Apply this?" instead of `Applied:`.

## Rules

- Never `schedule_delete` without the user's OK on the replacement — a deleted schedule with no replacement is a silent outage.
- Fix one job per confirmation; for a full cleanup, present a table of all jobs with per-job diagnoses and fix them one at a time.
- Do not invent last-run history; if outcomes aren't recorded, say "no run history available" and diagnose from the spec alone.
- If the root cause is a missing tool (no chat channel, shell off, MCP detached), fixing the prompt won't help — tell the user what to enable/connect first, and only then recreate the job.
- Prefer the simplest cron that matches intent; explain any cron you write in plain words so the user can veto it.
- If a run needs the current date, make the prompt say "use today's actual date" — never bake a stale example date into a recurring prompt.
