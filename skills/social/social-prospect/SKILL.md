---
name: Social Prospect
description: Find people publicly asking for what a brand sells on Threads, TikTok or Instagram, classify their intent, and write them to leads.jsonl with a link back to their post. Read-only; never replies.
when_to_use: When asked to prospect, find leads, scan keywords, or run a prospecting session for a brand. Not for replying (Social Engage) and not for market research (Social Insight).
version: 1.0.0
requires_skills: [social-core]
---

# Social Prospect

Find humans with buying intent. Write them down. **Do not reply to anyone.** Replying is
Social Engage's job; keeping the two separate lets prospecting run freely while replies
stay under a budget.

## Preflight

1. Ensure `social.py` is in the working folder (see Social Core).
2. `shell("python3 social.py guard <brand> <platform>")`. Exit 1: stop this account, no exceptions.
3. Read `brands/<brand>/KEYWORDS.md` and `BRAND.md`: you need the service area and what the
   business actually offers to judge intent.
4. Read `references/platform-notes.md` in Social Core for search URLs and extraction
   recipes before opening a tab.

## Where to search

**Home feed first, briefly.** `browser_open` the platform's home feed and read it for a
few minutes: a feed is fresh by construction and shows how people actually phrase
requests. It skews toward sellers, so apply the seeker-vs-seller test and time-box it.

**Then search.** Threads is the primary platform: it has keyword search with a recent
sort and text-first posts. Open the sorted search URL directly (see platform notes),
`browser_snapshot` or `browser_read` the results, scroll two or three times with a pause,
and capture again. Use few broad queries, not many specific ones; extra words widen the
net on loose token matching. Check the session log in `KEYWORDS.md` for queries already
known to be dead.

TikTok caption search returns sellers and old videos; skip it for prospecting.
Instagram keyword search barely works; use hashtag pages and comments on recent Reels
only if time remains.

## What to capture

For each candidate: author handle, author URL, the post permalink (mandatory; a lead you
cannot click back to is worthless), the full post text, and the posted-at time.

## Classification

Keyword match is not intent. Judge each post into one of three tiers.

- **HIGH**: first person, present tense, an actual request. They want this now.
- **MEDIUM**: a real person with real interest, but planning or vague. Collect; reply
  only if the daily budget has room after the HIGHs.
- **REJECT** (never written): older than the freshness window (default 7 days; a late
  reply reads as a bot); the poster is **selling**, not buying (read the direction of
  the sentence, not the keyword); a business or creator posting promo; saturated (more
  than ~20 comments); wrong place or outside the service area; a language the brand does
  not serve. When genuinely ambiguous, reject. A small clean list beats a big one.

## Writing leads

```
shell("python3 social.py lead-add --json '{\"platform\":\"threads\",\"brand\":\"<slug>\",\"author\":\"@handle\",\"author_url\":\"https://…\",\"post_url\":\"https://…\",\"post_id\":\"abc123\",\"text\":\"<their exact words>\",\"posted_at\":\"2026-08-09T03:12:00+08:00\",\"query\":\"<query>\",\"intent\":\"high\",\"language\":\"id\",\"product\":\"<product>\"}'")
```

Always set `product` when a brand sells more than one thing; they convert differently.
If `BRAND.md` cannot yet answer what the lead is asking (no price, no turnaround), write
it with `"status":"research"`: Social Engage only replies to `collected` leads, so a
research lead cannot be replied to by accident.

Run `seen` for candidates as you go and write already-seen ones with
`"status":"skipped"` rather than dropping them: overlap with the last run is a signal
that the keywords are exhausted.

## Finish

Log the run even if you found nothing; `due` uses it to know when this skill last ran:

```
shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"threads\",\"kind\":\"run\",\"skill\":\"social-prospect\",\"note\":\"12 high, 8 medium, 31 rejected\"}'")
```

Report: counts by tier, which queries produced the most and which produced nothing
(those should be retired), and new phrasings worth adding to `KEYWORDS.md`. The keyword
set is supposed to evolve; that is the main product of early runs.
