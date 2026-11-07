---
name: Social DM
description: Read the brand's inbox on Threads, TikTok or Instagram, answer service inquiries in brand voice, and hand people off to WhatsApp with the number in readable digits plus one qualifying question. Escalates complaints and existing-booking issues to a person.
when_to_use: When asked to check DMs, answer the inbox, or triage messages for a brand. Private replies only; never for public comments or replies to strangers' posts.
version: 1.0.0
requires_skills: [social-core]
---

# Social DM

Answer the inbox and move people to WhatsApp.

Answering inbound messages is not a spam signal: a person who messaged you expects a
reply. DMs are not capped by the daily reply budget and need no pacing. `guard` still
applies, because a paused or blocked account must stay quiet.

## Preflight

1. Ensure `social.py` is in the working folder (see Social Core).
2. `shell("python3 social.py guard <brand> <platform>")`. Exit 1: stop this account.
3. Read `brands/<brand>/BRAND.md`.

## Work the inbox

Open the inbox (URLs per platform are in Social Core's platform notes) and work through
threads **by recency, reading the actual last message**, not by unread status. Every
stalled inquiry found in practice was marked read: someone opened it, replied once, and
abandoned it. Unread badges over-count (shares, old notifications) and under-count
(abandoned conversations). Check the requests or hidden folder too: cold inbound from
strangers lands there and is routinely missed for weeks. Accepting a request is the
owner's call; surface it, do not click it.

## Classify each thread

- **Service inquiry**: they want to book, rent, buy, price, or ask about availability.
  The main path: answer, then hand off.
- **Spam, bots, irrelevant**: leave it. Do not reply, do not engage.
- **Sensitive, do not auto-reply**: a complaint or anything angry; anything about an
  *existing* order or booking (change, delay, no-show, damage); refunds, money disputes;
  accidents, injuries, anything legal; press or partnership; anything you are not
  confident you understand. Append to `reports/escalations.md` (platform, handle,
  time, one-line summary, link) and log it:
  `shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"dm_escalate\",\"author\":\"@…\",\"note\":\"complaint about yesterday's delivery\"}'")`.
  An angry customer answered by a bot becomes a public post about being answered by a
  bot. Escalating is always the cheaper mistake.

## Answering a service inquiry

Reply in brand voice, in their language. Answer whatever `BRAND.md` answers; never invent
a price, an item, a date or a policy. Then hand off. Links are fine in DMs.

Get the number with `shell("python3 social.py wa-link <brand> <platform> \"<topic>\"")`:
it prints the encoded link (good for a bio) and the readable number. **In a DM send the
readable form**, `0812-3456-7890 (wa.me/628123456789)`; the encoded link wraps into
lines of `%20` noise and reads like a bot. **Always pair it with one qualifying
question**, never send it alone: the question keeps the conversation alive if they do not
click. A bare "message us on WhatsApp" with no number is how threads die.

Do not try to close in the DM. Your job is answered-and-moved, not booked.

Record every thread:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"dm_reply\",\"author\":\"@…\",\"topic\":\"<what they asked>\",\"handoff\":true,\"text\":\"<what you sent>\"}'")`

## Finish

Log the run even if the inbox was empty:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"run\",\"skill\":\"social-dm\",\"note\":\"threads handled\"}'")`

Report: threads handled, how many handed off, how many escalated and what they were, and
anything asked repeatedly; a question asked five times in a week belongs in `BRAND.md`.
