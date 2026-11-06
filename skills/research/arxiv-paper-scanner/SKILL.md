---
name: Arxiv Paper Scanner
description: Scan arXiv listing/RSS pages for new papers in a category, filter by the user's keywords, and report title, authors, one-line abstract summary, and the abs link per paper.
when_to_use: When the user asks for new/recent arXiv papers on a topic (e.g. new LLM papers, recent cs.CL submissions, papers about X this week), or wants a recurring paper watch.
version: 1.0.0
---

# Arxiv Paper Scanner

Find new arXiv papers matching a topic: fetch category listing/RSS pages, filter by
keywords, and report each hit as title + authors + one-line abstract + abs link.

## Inputs

- **topic / keywords** — what to match (e.g. "LoRA fine-tuning", "speculative
  decoding"). Derive 3-6 keyword variants (synonyms, abbreviations, spelled-out forms).
- **categories** — arXiv categories to scan. Default: infer from the topic
  (LLM/agents → cs.CL + cs.AI + cs.LG); if unclear, ask or default to cs.AI.
- **max papers** — default 10.

## Steps

1. Pick source URLs from
   `load_skill_resource({"slug": "arxiv-paper-scanner", "path": "references/arxiv-sources.md"})` —
   it lists the listing/RSS URL patterns per category and how to build search URLs.
2. For each chosen category, `browser_fetch` the RSS feed first
   (e.g. `https://rss.arxiv.org/rss/cs.AI`) — it carries titles, authors, and
   abstracts in one page. If the RSS fetch fails or is empty, fall back to the HTML
   listing (e.g. `https://arxiv.org/list/cs.AI/recent`).
3. Filter entries: keep a paper if its title or abstract contains any keyword
   variant (case-insensitive). Match substrings generously ("finetuning" vs
   "fine-tuning").
4. Deduplicate across categories by arXiv id (papers are often cross-listed).
5. If fewer than 3 hits, additionally `browser_fetch` an arXiv search URL built per
   the patterns in `references/arxiv-sources.md`, and merge results.
6. For each kept paper (up to max), extract: title, authors (first 3 + "et al." if
   more), arXiv id, and compress the abstract to ONE sentence stating the
   contribution. If the abstract is not in the feed entry, `browser_fetch` the
   paper's `https://arxiv.org/abs/<id>` page.
7. Output the report (format below). If the user wants ongoing monitoring, offer
   `schedule_create` (e.g. weekdays 09:00) re-running this scan, and offer
   `memory_write` of the reported arXiv ids so a later run can skip papers already
   shown (check with `memory_search` at the start of scheduled runs).

## Output format

```
## New arXiv papers — <topic> (<YYYY-MM-DD>)
Scanned: <categories> · matched <n> of <total> entries

1. **<Title>** — <Authors>
   <One-line contribution summary.>
   https://arxiv.org/abs/<id>
```
One numbered block per paper, newest first. End with a line noting keyword variants
used, and — if any category fetch failed — which one.

## Rules

- Links are always the `abs` page (`https://arxiv.org/abs/<id>`), never the PDF.
- Never invent a paper, author list, or arXiv id — every field comes from a fetched
  page. If a field is missing, write "n/a", don't guess.
- The one-line summary must restate the paper's own claim, not your opinion of it.
- Zero matches is a valid result: report it, name the categories and keywords
  scanned, and suggest broader keywords or another category from the reference file.
- arXiv listing pages show recent submissions, not a guaranteed date window — state
  the listing's own date range in the report rather than promising "today's papers".
- Egress is arxiv.org (and rss.arxiv.org) only; do not fetch third-party paper sites
  unless the user asks.
