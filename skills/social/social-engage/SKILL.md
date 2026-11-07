---
name: Social Engage
description: Reply publicly to collected leads on Threads, TikTok or Instagram in brand voice, with no links, under a strict daily budget, human pacing and a dedupe gate. The only social skill that speaks in public to strangers.
when_to_use: When asked to engage, reply to leads, or run an engagement session for a brand after prospecting has collected leads. Never for DMs (Social DM) or comments on the brand's own posts (Social Comments).
version: 1.0.0
requires_skills: [social-core, social-prospect]
---

# Social Engage

Turn collected leads into conversations. This is the only skill that speaks in public to
strangers, so it has real teeth.

## Preflight, all three, in order

1. Ensure `social.py` is in the working folder (see Social Core).
2. `shell("python3 social.py guard <brand> <platform>")`. Exit 1: stop. The JSON it prints
   carries `replies_remaining_today`. If that is **0**, stop: usually `ramp_start` is
   still null, meaning the account has never been cleared for public replies. Clearing
   it is the owner's decision; say so and stop. Do not look for a way around it.
3. Read `brands/<brand>/BRAND.md` and `REPLIES.md`.

## Pick who to answer

`read_file("data/leads.jsonl")`, filter to this brand and platform with
`"status":"collected"`. HIGH intent first, then MEDIUM if budget remains. Prefer the
newest. **`collected` is the whole filter.** Leads with `status: research` belong to a
product the kit cannot answer; replying would pitch something you cannot describe and
burn that person for the dedupe window. Check `product` before writing: a reply that
answers the wrong line of business reads worse than no reply.

## The loop, per lead

1. `shell("python3 social.py seen <platform> <post_id> <author>")`. Exit 1: skip entirely.
2. `shell("python3 social.py pace <brand> <platform>")`. It waits up to 50 s and prints
   what is still owed; call it again until it owes 0 s. The randomness is the point: a
   metronomic cadence is a stronger bot signal than acting quickly.
3. `browser_open` the post URL, `browser_snapshot`, read the post properly.
4. `browser_click` the reply box, `browser_type` the reply.
5. **`browser_screenshot` and read it before submitting. Not a formality.** Check: right
   post, right account, right text, no link. Some composers open a *new post* dialog on
   top of the reply box; submitting blind there publishes an original post from the
   brand account. If a new-post dialog is showing, cancel it and use the reply composer
   underneath. Per-platform recovery steps are in Social Core's platform notes.
6. `browser_click` submit, then confirm it landed (the post's reply count moved).
7. Immediately, before anything else:
   `shell("python3 social.py seen-add <platform> <post_id> <author> <brand>")` and
   `shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"public_reply\",\"author\":\"@…\",\"post_url\":\"…\",\"template\":\"rec-area\",\"text\":\"<what you actually sent>\"}'")`.
   If the run dies between the reply and the record, that person becomes eligible to be
   replied to again, which is the worst failure this system can produce.

Never batch click, type and submit into one step.

## What a reply must be

1. **Specific to their post.** Name the thing they mentioned: the place, the dates, the
   budget. A reply that could be pasted under any post reads as a bot, and people say so
   in public.
2. **Short.** One or two sentences; match the length of other replies there.
3. **In the brand's voice and the person's language**, per `BRAND.md`.
4. **Genuinely useful on its own.** Answer their question even if they never contact you.
   This is the difference between helpful and spam, and what stops people reporting the account.
5. **No link, no phone number, no WhatsApp number.** Close with a soft invite: "happy to
   help in DM" or "details in the bio".
6. **A different opening from the last reply.** Check recent `template` values in
   `data/actions.jsonl`; never the same shape twice in a row, and vary the wording.

Never claim a price, an item, availability or a policy that is not in `BRAND.md`. If you
do not know, invite them to DM.

## When the platform pushes back

An action-block modal, a captcha, a login challenge, a reply that submits but does not
appear, or a reply box that refuses to open:
`shell("python3 social.py cooldown <brand> <platform> 24 \"<exactly what you saw>\"")`.
Stop that account for the run. Do not retry, do not switch to another post, do not test
whether it still works. Report it.

## Finish

Report: replies sent versus budget, which leads you skipped and why, anything that felt
off in tone, and any sign of suppression worth a Social Health check.
