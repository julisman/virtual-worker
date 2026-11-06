# arXiv source URLs

All fetchable with `browser_fetch`. Prefer RSS (structured, includes abstracts);
use HTML listings as fallback; use search URLs to widen a thin scan.

## RSS feeds (preferred)

Pattern: `https://rss.arxiv.org/rss/<category>` — new submissions for the category,
each item = title, authors, abstract, abs link.

| Category | Feed | Covers |
|---|---|---|
| cs.AI | https://rss.arxiv.org/rss/cs.AI | AI broadly: agents, planning, reasoning |
| cs.CL | https://rss.arxiv.org/rss/cs.CL | NLP / language models |
| cs.LG | https://rss.arxiv.org/rss/cs.LG | machine learning methods |
| stat.ML | https://rss.arxiv.org/rss/stat.ML | ML theory / statistics |
| cs.CV | https://rss.arxiv.org/rss/cs.CV | computer vision |
| cs.RO | https://rss.arxiv.org/rss/cs.RO | robotics |
| cs.SE | https://rss.arxiv.org/rss/cs.SE | software engineering |
| cs.CR | https://rss.arxiv.org/rss/cs.CR | security/crypto |

Multiple categories in one feed: join with `+`, e.g.
`https://rss.arxiv.org/rss/cs.AI+cs.CL`.

## HTML listing pages (fallback)

Pattern: `https://arxiv.org/list/<category>/recent` — recent submissions, HTML.
Examples: `https://arxiv.org/list/cs.AI/recent`, `https://arxiv.org/list/cs.CL/recent`,
`https://arxiv.org/list/cs.LG/recent`, `https://arxiv.org/list/stat.ML/recent`.

A specific month: `https://arxiv.org/list/<category>/<YYYY-MM>`
(e.g. `https://arxiv.org/list/cs.CL/2026-07`).

## Search URLs (to widen a scan)

Keyword search over all fields:
`https://arxiv.org/search/?query=<url-encoded+terms>&searchtype=all&start=0`

Restrict to a category by adding the category as a term, or use the field syntax:
`https://arxiv.org/search/?query=cat:cs.CL+AND+all:%22speculative+decoding%22&searchtype=all`

URL-encode spaces as `+` and quotes as `%22`. Results are HTML; each hit links to
its abs page.

## Per-paper pages

- Abstract page (the link to report): `https://arxiv.org/abs/<id>`, e.g.
  `https://arxiv.org/abs/2401.00001`. Contains title, authors, full abstract.
- Ids look like `YYMM.NNNNN` (optionally `vN`). Strip the version when
  deduplicating.
