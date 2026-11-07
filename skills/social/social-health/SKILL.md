---
name: Social Health
description: Check whether a brand's social accounts are being suppressed or shadowbanned — verify recent replies are visible logged out, watch the reply-to-response ratio, scan for platform friction — and park unhealthy accounts with a cooldown.
when_to_use: When asked to run a health check, check for a shadowban, or investigate why leads or responses dried up; also as a daily scheduled run per account.
version: 1.0.0
requires_skills: [social-core]
---

# Social Health

The failure this catches: **a suppressed account looks exactly like a working one from
the inside.** Replies post, appear in your own feed, the logs look clean, and nobody else
can see any of it. Without this check the system can spend weeks talking to an empty
room. Run daily, per account.

## Preflight

1. Ensure `social.py` is in the working folder (see Social Core).
2. `shell("python3 social.py guard <brand> <platform>")`. A parked account stays parked;
   note it and move to the next.

## 1. Logged-out visibility, the decisive test

Take three `public_reply` entries from `data/actions.jsonl` posted 24 to 48 hours ago.
Open each post URL in a context with **no session** for that platform: a fresh tab in a
logged-out profile, or use `browser_fetch` on the post URL, which never carries the
brand's cookies. Can you see the reply in the thread?

- All three visible: healthy.
- Some missing: possible per-reply filtering. Note it; check again tomorrow before acting.
- None visible: the account is suppressed. Park it:
  `shell("python3 social.py cooldown <brand> <platform> 72 \"shadowban check: 0/3 replies visible logged out\"")`
  and tell the owner plainly. Do not quietly keep going at lower volume; a suppressed
  account needs to go fully quiet and rebuild, which is a decision for a person.

## 2. Response ratio

From `data/actions.jsonl`, count public replies over the last 7 days per account, and how
many produced any response: a like, a reply back, a profile visit, a DM. A healthy
conversational account gets something back on a meaningful share. A hundred replies in a
week with zero interactions of any kind is almost certainly filtering, even if test 1
looked fine. Compare with the previous week; a sharp drop with no change in behaviour is
the signal.

## 3. Friction signals

Scan the last 7 days of `data/actions.jsonl` for `kind: "blocked"` entries, and note
anything observed during runs: action blocks or "try again later" (one is worth
recording; two in a week means back off hard), captchas or login challenges where there
were none, reply boxes that take longer to open, follower count flat while activity is up.

## 4. Report

Write the result into `reports/<YYYY-MM-DD>.md` and log it:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"health_check\",\"note\":\"3/3 visible logged out, 12% response rate, no blocks\"}'")`
then the run marker, every run, healthy or not:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"run\",\"skill\":\"social-health\",\"note\":\"daily check\"}'")`

State a verdict per account in plain words, healthy, watch, or parked, and what it is
based on. If anything is parked, lead with that.

## If an account is unhealthy

Recommend, in order: stop public replies for that account (DMs can continue; answering
inbound is never the cause), let it sit 3 to 7 days, then resume at week-one ramp volume.
That means resetting `ramp_start` in the control panel to the resume date, which only the
owner can do: propose it, do not do it.
