---
name: Trending Topic Digest
description: Research the top N trending topics in a domain via web search and page fetches, then produce a date-stamped digest with a 2-3 sentence summary and reference link per topic.
when_to_use: When the user asks what is trending, hot, or new in a domain (e.g. AI, tech, crypto, gaming) and wants a summarized digest rather than raw links, or wants such a digest delivered on a schedule.
version: 1.0.0
---

# Trending Topic Digest

Research what is trending in a domain right now and deliver a date-stamped digest:
N topics, each with a 2-3 sentence summary and at least one reference link.

## Inputs

- **domain** — the field to scan. Default: AI/tech.
- **count (N)** — number of topics. Default: 10.
- **delivery** — where to send the digest. Default: reply in chat; use `deliver` only
  if the user named a channel or the run is scheduled/unattended.

## Steps

1. Establish today's date (from context or `shell` with `date`; if `shell` is
   unavailable, use the date given in your context). The digest header needs it.
2. Run 3-5 `browser_search` queries angled differently, e.g. for AI/tech:
   `"AI news this week"`, `"trending in <domain> <month year>"`,
   `"<domain> announcements"`, `"biggest <domain> stories today"`.
3. From the combined results, shortlist candidate topics. Merge duplicates that
   cover the same underlying story (same launch, paper, incident, or company event).
4. Rank by how many independent sources mention the topic and how recent it is.
   Keep the top N.
5. For each kept topic, `browser_fetch` the best 1-2 source URLs and write a 2-3
   sentence summary: what happened, why it matters, one concrete detail (number,
   name, date). Record every URL you actually fetched as the topic's reference(s).
6. Assemble the digest using the exact template in
   `load_skill_resource({"slug": "trending-topic-digest", "path": "references/digest-format.md"})`.
7. Present the digest in chat. If a delivery channel was requested, send the full
   digest text with `deliver(channel, target, text)`; confirm what was sent where.
8. If the user wants this recurring, offer `schedule_create` with a cron expression
   (e.g. daily 08:00) whose task re-runs this skill with the same domain and N.

## Output format

Follow `references/digest-format.md` exactly: an H1 with domain + date, a one-line
overview, then N numbered topic blocks (title, 2-3 sentence summary, `Sources:`
links), and a closing `Method` line listing the queries used.

## Rules

- Every topic MUST cite at least one link you actually fetched with `browser_fetch`.
  Never cite a URL you only saw in search results without opening it.
- No fabricated topics: if fewer than N distinct trending topics surface, output the
  ones you found and say the search yielded fewer than N — do not pad.
- Prefer primary or well-known sources; if a topic rests on a single low-credibility
  source, say so in its summary.
- Date-stamp with the real current date; never guess the date from page content alone.
- Network egress is limited to `browser_search`/`browser_fetch` and the one delivery
  target the user named — send the digest nowhere else.
- If a fetch fails, try one alternate source for that topic, then move on; note any
  topic whose summary rests on the search snippet only.
- Reply in the user's language, but keep topic titles in their original language.
