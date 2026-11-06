---
name: Email Triage
description: Read today's inbox via connected Gmail MCP tools and classify every message into Urgent / Needs action / FYI, with a 1-2 line summary (sender + ask) for each urgent item. Read-only unless the user explicitly confirms an action.
when_to_use: When the user asks to check email, triage the inbox, "anything important in my mail", or summarize today's messages.
version: 1.0.0
---

# Email Triage

Triage today's inbox into Urgent / Needs action / FYI and report it — touch nothing without explicit confirmation.

## Inputs

- **Time window** — default: today (since 00:00 local). The user may widen it ("since Monday", "last 3 days").
- **Scope** — default: inbox only, unread + read. Skip spam/trash always.

## Steps

1. **Check the tool.** Look for connected Gmail MCP tools on this agent (names vary by server — scan your tool list for a Gmail/mail MCP). If none is attached, tell the user: "No Gmail MCP is connected — add one in Agents → MCP, then re-run." and STOP. Do not try `browser_open` on Gmail or any other workaround.
2. **Fetch.** Use the Gmail MCP's list/search tool to get messages in the window (query for messages after the window start). For each message capture: sender, subject, date, and enough body/snippet to know the ask. Read-only calls only.
3. **Classify.** Load the rubric — `load_skill_resource({"slug": "email-triage", "path": "references/triage-rubric.md"})` — and assign each message exactly one bucket: **Urgent**, **Needs action**, or **FYI**. When in doubt between two buckets, pick the lower-severity one and note the doubt.
4. **Summarize urgent.** For every Urgent item write 1-2 lines: `<sender> — <what they ask> — <deadline if stated>`. Never paraphrase a deadline loosely; quote dates as written.
5. **Report.** Present the triage in the output format below, in one message.
6. **Actions only on confirmation.** If the user then asks you to reply, archive, label, delete, or forward: restate exactly what you will do to which message, ask "Confirm?", and only act after they say yes in a later message. One confirmation covers one action, not a session.

## Output format

```
📬 Inbox triage — <window>, <n> messages

🔴 Urgent (<n>)
1. <sender> — <ask in one line> — <deadline>
   <optional second line of context>

🟡 Needs action (<n>)
- <sender>: <subject> — <what's needed>

⚪ FYI (<n>)
- <sender>: <subject>
```

End with: "Want me to draft a reply or act on any of these? I'll confirm before doing anything." Reply in the user's language.

## Rules

- **Never send, reply, delete, archive, label, or mark-read without explicit confirmation in a later user message.** Triage itself is strictly read-only.
- **No Gmail MCP → stop at step 1.** Never scrape mail via the control browser as a fallback.
- Do not quote full email bodies into the report — summarize. Email content stays in this chat; never `deliver` or forward it anywhere the user didn't name.
- Newsletters/notifications are FYI by default even if they say "urgent" (see the rubric's spam-urgency note).
- If the window has more than ~50 messages, triage the newest 50 and say how many were left out; offer to continue.
- If a message is unreadable (encrypted, empty snippet), list it under FYI as `<sender>: <subject> (could not read)` — never guess its content.
