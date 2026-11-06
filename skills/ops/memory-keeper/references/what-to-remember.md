# What to remember — save / don't-save lists and phrasing patterns

## SAVE — durable, reusable facts

- **Identifiers**: chat (WhatsApp/Telegram/Slack) ids, group IDs, channel names, repo URLs,
  ticket-system project keys, server names, webhook targets.
- **Contacts**: emails, names + roles ("Rina = infra lead"), timezones.
- **Preferences**: format ("weekly report as bullet list"), language ("reply in
  Bahasa Indonesia"), tone, delivery channel/target, working hours.
- **Corrections**: any fact the user fixed — store the corrected version.
- **Recurring context**: project names and what they mean, team structure,
  standing constraints ("staging deploys only on Thursdays").
- **Decisions**: agreed processes ("backups keep last 5", "never email client X
  directly").

## DON'T SAVE

- **Secrets**: passwords, API keys, tokens, OTPs, card numbers — never, even if
  the user says "remember my password".
- **Transient state**: "the build is red", "he's on leave this week", task
  progress, anything that expires in days.
- **Relative time**: "tomorrow", "next sprint" — convert to an absolute date or
  don't store it.
- **Conversation summaries / raw logs**: memory is for facts, not transcripts.
- **Duplicates**: anything `memory_search` already returns verbatim.
- **Sensitive personal inferences**: opinions about people, health, conflicts —
  unless the user explicitly asks you to keep it.

## Phrasing patterns — subject first, self-contained, dated

Good entries can be found by a search for their subject and understood with no
other context:

```
channel id | Infra Standup group = 8827413 (group) | confirmed 2026-07-23
contact | Rina Wijaya = rina.w@example.com, infra lead, GMT+7 | 2026-07-23
preference | weekly report: bullet list, delivered in_app Monday 09:00 | 2026-07-23
correction | staging URL is https://stg2.example.com (old stg1 retired) | 2026-07-23
decision | backups: keep last 5 archives, ask before first rotation delete | 2026-07-23
project | "Garuda" = the checkout-rewrite project, owner Dimas | 2026-07-23
```

Anti-patterns:

```
BAD: "User said to remember the thing about the meeting tomorrow"   (relative date, no subject)
BAD: "Rina's email might be rina@..."                               (uncertain — verify first)
BAD: storing "Rina = infra lead" when memory already has it          (duplicate)
```

## Update pattern

When a fact changes, never write a second version next to the first:

1. `memory_search "staging URL"` → find the old entry.
2. `memory_forget` the old entry.
3. `memory_write` the new entry with today's date.
