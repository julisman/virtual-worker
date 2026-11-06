---
name: News Digest
description: Build a date-stamped news roundup for a topic, company, or region — search recent coverage, dedupe near-identical stories, group by theme, 1-2 lines plus link per story, with a source-diversity note.
when_to_use: When the user asks for the news, headlines, or a press roundup about a topic, company, industry, or region ("what's the news on X", "berita terbaru tentang Y"), or wants such a roundup on a schedule.
version: 1.0.0
---

# News Digest

Produce a date-stamped news roundup: recent stories on one subject, deduplicated,
grouped by theme, 1-2 lines + link each, with a note on source diversity.

## Inputs

- **subject** — topic, company, or region.
- **window** — how recent. Default: last 7 days (or since the last digest, if the
  user says "since last time").
- **size** — default 8-12 stories; honor "short" (~5) or "full" (~15).

## Steps

1. Run 3-4 `browser_search` queries from different angles:
   `"<subject> news"`, `"<subject> latest"`, plus one narrowed by the window (e.g.
   `"<subject> this week"`) and one on the subject's most active sub-story if known.
2. Collect candidate stories (title, outlet, URL, apparent date). Discard items
   clearly outside the window when a date is visible.
3. **Dedupe**: cluster items covering the same underlying event (same announcement,
   filing, incident) even when headlines differ. Keep one representative per
   cluster — prefer the primary source or most detailed original report — and note
   how many outlets covered it ("also covered by N others").
4. For each kept story, `browser_fetch` the chosen URL and write 1-2 lines: what
   happened + the key concrete fact. If a page won't load, fall back to the next
   outlet in its cluster.
5. Group the stories under 2-5 theme headings that emerge from the material (e.g.
   Product, Regulation, Funding, People). No "Miscellaneous" bucket of one item —
   fold strays into the nearest theme.
6. Assess source diversity: how many distinct outlets, and whether any single
   outlet or one press release dominates the digest.
7. Output the digest (format below). If asked to deliver, send the full text via
   `deliver(channel, target, text)`. For a recurring digest, offer
   `schedule_create` (cron, e.g. daily 07:30) re-running this skill with the same
   subject and window.

## Output format

```
# News digest: <Subject> — <YYYY-MM-DD>
Window: <e.g. last 7 days> · <n> stories from <k> outlets

## <Theme 1>
- **<Story headline (your own concise phrasing)>** — <1-2 lines>. <url>
  (also covered by <N> other outlets)   <- only when a cluster had duplicates
## <Theme 2>
- ...

---
Source diversity: <k> outlets; <balanced | leans on <outlet> | mostly one press
release>. Not fetched: <any story included from snippet only, or "none">.
```

## Rules

- Every story link must be a page you fetched with `browser_fetch`; if you must
  include a story from a snippet only, flag it in the "Not fetched" line.
- Dedupe is mandatory — two headlines about the same event must never appear as two
  stories.
- Report events as the sources state them; attribute contested claims ("according
  to <outlet>"). Keep summaries neutral — no editorializing.
- Undated stories: include only if clearly current from content, and mark "date
  unclear".
- Thin news weeks are fine: fewer stories beats padding with off-topic or stale
  items; say the window was quiet.
- Date-stamp with the real current date. Egress is search/fetch plus the named
  delivery target only.
- Write in the user's language; keep proper nouns and outlet names as-is.
