---
name: Social Insight
description: Research what real people are publicly saying about a topic the user gives in their own words — themes, phrasing, prices, objections, seeker-versus-seller mix — across Threads, TikTok and Instagram, into a sourced report with verbatim quotes and links. Read-only; never replies.
when_to_use: When asked to collect insights, research a topic, check sentiment, size demand, or find out what people are saying about something. On demand only, never scheduled. Not for collecting leads (Social Prospect).
version: 1.0.0
requires_skills: [social-core]
---

# Social Insight

The user gives you wording, a topic, a question, or exact phrases they have heard, and
you find out what real people publicly say about it. The output is a report with verbatim
quotes and links: not replies, not leads.

**Read-only, absolutely.** No replies, no likes, no follows, no DMs. You are borrowing a
brand's logged-in browser to listen. This skill runs only when the user asks.

## Preflight

1. Ensure `social.py` is in the working folder (see Social Core).
2. Pick which brand's browser session to use: the one the user names, otherwise the brand
   closest to the topic. `shell("python3 social.py guard <brand> <platform>")`; a parked
   account stays parked even for reading, so try another brand's session.

## Frame the question first

Before opening a tab, pin down from the user's wording: the exact question the report
must answer, the phrases to search (their words, plus two or three plain variants), and
what would count as a finding (a recurring complaint, a price range, a phrasing people
use). Write it at the top of a scratch file and collect into it as you go, never from
memory at the end.

## Where to look

Threads first (keyword search, text-first). TikTok is useful here, unlike for
prospecting: the comment sections under the top videos for a topic are dense, candid
opinion, so search the topic, open the top handful of videos, read the comments, and
record each video's age. Instagram last, hashtag pages and comments on recent Reels only.

## What is different from prospecting

- **Old posts count.** You are not replying, so a six-month-old thread is evidence, not
  waste; note the date and say how old the evidence skews.
- **Sellers count too.** A competitor's ad is data here: pricing, positioning, offer
  structure, how crowded the market is. Tag every quote **seeker** or **seller**; the
  ratio itself is a finding.

## What to capture

For everything you keep: verbatim quote, permalink, date, platform, seeker or seller tag.
A quote you cannot link back to is an anecdote. Watch for and tally where you can:
recurring themes; phrasing (the actual words people use to ask, the highest-value
by-product: propose them for `KEYWORDS.md`); numbers (prices, fees, "how much?" with
answers); objections and horror stories; the seeker-versus-seller mix per query.

If you hit a post that would be a HIGH lead under Social Prospect's rules (fresh, first
person, a real request), do not waste it: run `seen`, write it with `lead-add`, list it in
the report. Do **not** reply; that stays Social Engage's job, under its budget.

## Report

Write `reports/insights/<YYYY-MM-DD>-<topic-slug>.md`:

1. **Question**, and the answer in two or three sentences up top.
2. **Method**: platforms, queries, roughly how many posts and comments read, date range.
   Say what you did not cover.
3. **Findings by theme**, each with two to four verbatim quotes, linked and dated. Counts
   where you have them ("7 of 21 buyer posts mentioned delivery"), and plain "I only saw
   this once" where you do not. Never round a vibe up into a statistic.
4. **Phrasing worth adding to `KEYWORDS.md`**, proposed, not edited in.
5. **Leads captured**, if any.

Then the run marker, even if you found nothing:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"run\",\"skill\":\"social-insight\",\"note\":\"topic: <slug>; 21 buyer posts, 34 seller, 3 themes\"}'")`

Tell the user the answer first, then where the report is. If the evidence was thin, say
so plainly; "the market is not talking about this" is itself a finding.
