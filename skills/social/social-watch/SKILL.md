---
name: Social Watch
description: Monitor standing watchlists (watch/<slug>.md) on Threads, TikTok or Instagram for new listings or posts that match the owner's hit rules — for example urgent-sale property under a price ceiling — dedupe against watch.jsonl, and report only what is new. Read-only; never contacts anyone.
when_to_use: When asked to run the watchlists, check a watch, or look for new matches for a standing search; or as a scheduled run every few hours. Not for leads with buying intent (Social Prospect).
version: 1.0.0
requires_skills: [social-core]
---

# Social Watch

A watch is a standing search the owner wrote down once: what they are hunting, where,
and what counts as a hit. Each run surfaces only what is new. Contacting a seller is the
owner's decision, made from the report; this skill never replies, likes, follows or DMs.

## Preflight

1. Ensure `social.py` is in the working folder (see Social Core).
2. Read every `watch/*.md` (skip `_example.md`). Each names the brand and platform whose
   logged-in session to browse from.
3. For that account: `shell("python3 social.py guard <brand> <platform>")`. Exit 1: skip
   this watch for the run.

## A watch file

```
# Watch: <what you're hunting, in one line>
**Looking for:** one concrete sentence.
**Account:** <brand-slug> / <platform>
## Queries            broad phrases first; tag feeds if known; known noise recorded here
## Counts as a hit    rules that must ALL hold: category, location named, selling not hunting, the signal that matters
## Capture            extra fields to extract (asking price as written, size, area, owner-direct vs agent)
```

Keyword match alone is never a hit. Apply every rule. When a rule is unclear, do not
count it; report the ambiguity instead so the owner can sharpen the file.

## Run

For each query: `browser_open` the search (recent sort), `browser_snapshot`, scroll two
or three times with a pause, capture again. For each candidate that passes the hit rules,
record it; the script rejects duplicates by platform and post id:

`shell("python3 social.py watch-add --json '{\"watch\":\"<slug>\",\"platform\":\"threads\",\"author\":\"@handle\",\"author_url\":\"https://…\",\"post_url\":\"https://…\",\"post_id\":\"abc123\",\"text\":\"<their exact words>\",\"price\":\"26jt nego\",\"location\":\"Nusa Dua\",\"posted_at\":\"…\"}'")`

A duplicate that was re-posted (bumped) is worth one line in the report, not a new hit.

## Report

Maintain one rolling file per watch, `reports/watch/<slug>.md`, newest run at the top:
the date, then each new hit as item, price, location, author, link, their exact words. A
run with nothing new gets one line, `<date> <time>: no new listings (checked N queries)`,
so silence reads as "checked, quiet" rather than "did not run".

Then the run marker, every run, hits or not:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"run\",\"skill\":\"social-watch\",\"note\":\"<watch>: 2 new, 1 bump, 14 dupes\"}'")`

Tell the user only when there is something to say. New hits: lead with them. Nothing
new: one line. If a query has produced nothing across many runs, say so and propose
changing it in the watch file; a watch that never fires is either early or mis-aimed, and
only the owner knows which.

## Cadence

Listings move slowly; every few hours is plenty. More frequent runs just spend the
account's search activity for zero new posts.
