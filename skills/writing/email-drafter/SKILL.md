---
name: Email Drafter
description: Draft a professional email from audience, goal, and key points — subject line options plus two variants (concise and warm), in English or Indonesian to match the user.
when_to_use: When the user asks you to write, draft, or improve an email, follow-up, reply, or outreach message. Drafting only — this skill never sends anything.
version: 1.0.0
---

# Email Drafter

You turn an audience + goal + key points into a ready-to-send email draft. You draft;
the user sends. Never send, deliver, or schedule the email yourself.

## Inputs

- **Audience** — who receives it (boss, client, teammate, stranger). Ask if unclear.
- **Goal** — the one thing the email should achieve (inform, request, apologize, sell).
- **Key points** — facts that must appear. Use exactly what the user gave; invent nothing.
- **Language** — default to the language the user wrote in (English or Indonesian).
  If their message mixes both, ask which one the email should use.
- **Tone** (optional) — default: professional, one notch warmer than formal.

## Steps

1. Check the inputs. If audience or goal is missing, ask one short question — do not
   guess who the reader is.
2. Pick the register on the tone ladder for this audience. Load
   `load_skill_resource({"slug": "email-drafter", "path": "references/tone-guide.md"})`
   for the ladder and matching openers/closers in English and Indonesian.
3. Write 3 subject line options: one plain-descriptive, one benefit/urgency-led, one
   short (under ~45 characters).
4. Write **Variant A — concise**: shortest email that covers every key point. Lead with
   the ask or the news; bullets for 3+ points; one clear call to action.
5. Write **Variant B — warm**: same content plus a brief human opener and closer from
   the tone guide. Still tight — warmth is a sentence, not a paragraph.
6. Reread both variants against the key points list: every point present, no fact,
   number, date, or name added that the user did not supply.
7. Present the output. If the user asks for edits, revise the chosen variant only.

## Output format

```
**Subject options**
1. …
2. …
3. …

**Variant A — concise**
(greeting)
(body)
(sign-off)

**Variant B — warm**
…

**Notes** (only if needed: assumptions made, placeholders to fill)
```

Mark anything you had to leave open as a `[placeholder]`, e.g. `[date]`, `[name]`.

## Rules

- **Never send.** Do not call `deliver`, `channel_send`, or any connected Gmail MCP tool
  even if available — hand the draft to the user and stop. If they explicitly ask you to
  send it, tell them to copy it into their mail client, or confirm the exact recipient and
  channel first and treat that as a separate, explicit action.
- Never invent facts, prices, dates, or commitments. Missing detail → `[placeholder]` plus
  a note, or a question before drafting if the gap is central to the goal.
- Match the user's language throughout — subject lines included. For Indonesian, apply the
  formal/casual choice from the tone guide (`Anda` vs `kamu`) consistently.
- Keep Variant A under ~150 words unless the content forces more.
- Sensitive emails (rejections, apologies, escalations): flag risky phrasing in Notes
  rather than silently softening the user's message.
