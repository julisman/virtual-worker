---
name: Web Scraper
description: Polite multi-page data extraction — check robots.txt first, fetch listing pages one at a time with browser_fetch, extract agreed fields, normalize into a CSV in the working folder, stop at a page cap, and report gaps.
when_to_use: When the user asks to collect structured data from a public website across one or more pages (listings, tables, directories, search results) into a CSV or dataset.
version: 1.0.0
---

# Web Scraper

Extract an agreed set of fields from a public site's pages into one clean CSV — politely, within an agreed cap, and never from protected or personal data.

## Inputs

- **Start URL(s)** — the listing/index page(s) (required).
- **Fields** — the exact columns to extract; agree them with the user before fetching data pages.
- **Page cap** — maximum pages to fetch. Default: 5; hard-stop at whatever is agreed.
- **Output file** — default: `scrape-<site>-<YYYY-MM-DD>.csv` in the working folder.

## Steps

1. Confirm the plan with the user: fields, page cap, output file. No data fetches before this is agreed.
2. Check permission to scrape: `browser_fetch` the site's `/robots.txt`. If the target path is disallowed for generic crawlers (`User-agent: *` with a matching `Disallow`), stop and tell the user the site disallows it — do not proceed. If robots.txt is absent or unreadable, note that and continue only for clearly public content.
3. `browser_fetch` the first listing page. Identify how records appear in the page text and locate each agreed field on a sample record; show the user one extracted sample row and confirm the mapping looks right.
4. Fetch remaining pages **one at a time** with `browser_fetch` (follow the site's own pagination links/URL pattern), extracting all records per page as you go. Never fire fetches in parallel and never exceed the cap.
5. Normalize rows: consistent column order matching the agreed fields; empty string for a missing value (never invent one); strip currency/thousands separators into plain numbers where the user asked for numbers; dates as `YYYY-MM-DD`.
6. Build the CSV: header row = agreed fields; quote any value containing commas, quotes, or newlines (double the inner quotes). `write_file` it to the output file.
7. Verify: row count matches records extracted; spot-check 2 rows against their source pages. Report per the Output format, including every gap (pages that failed, fields often missing).

## Output format

```
Scrape complete — <site>
Robots.txt: allowed (checked <date>)
Pages fetched: <n>/<cap> · Records: <n> · File: <output path>
Columns: <field, field, ...>
Gaps: <"page 4 failed to fetch", "price missing on 12 records", or "none">
Sample row: <first data row>
```

If stopped early (robots disallow, cap hit with more pages remaining, repeated fetch errors), say exactly where and why.

## Rules

- **Never scrape login-protected pages** — this skill uses read-only `browser_fetch`, not the logged-in control browser; if content needs a session, decline and say so.
- **Never collect personal data** (emails, phone numbers, home addresses, private profiles), even if technically visible. Public business listings are fine; people are not.
- Robots.txt disallow is a refusal, not an obstacle to route around (no mirror sites, no cache tricks).
- One page per fetch, sequential; if a fetch fails, retry once, then record it as a gap and move on.
- Hard-stop at the page cap; if more data exists, report how much and let the user raise the cap explicitly.
- Local-first: data goes only into the working-folder CSV; no delivery to any external target unless the user asks.
- Report extraction honestly: a field you could not reliably locate is a gap, not a guessed value.
