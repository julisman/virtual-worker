---
name: Competitor Watch
description: Monitor a competitor by fetching their site, news, and changelog, storing a dated snapshot in memory, diffing against the previous snapshot, and reporting what changed since last check.
when_to_use: When the user asks to watch, track, or monitor a competitor (or "what's new at company X since last time"), or wants recurring competitor updates.
version: 1.0.0
---

# Competitor Watch

Check a competitor's public footprint, remember a dated snapshot, and report what
changed since the previous snapshot.

## Inputs

- **competitor** — company/product name; a homepage URL if the user has one.
- **focus** — optional: what matters (pricing, features, hiring, funding). Default:
  products/features, pricing, announcements.
- **cadence** — optional: if the user wants recurring checks (e.g. weekly).

## Steps

1. Retrieve the previous snapshot: `memory_search("competitor watch <competitor>")`.
   Note its date and content. No hit ⇒ this is a baseline run.
2. Resolve sources. If no URL was given, `browser_search("<competitor> official site")`
   to find the homepage. Then `browser_fetch` up to ~5 pages: homepage, pricing page,
   changelog/release-notes or blog (find via `browser_search("<competitor> changelog")`
   if not linked), and one `browser_search("<competitor> news")` pass for recent
   third-party coverage — fetch the 1-2 most substantive articles.
3. Condense what you fetched into a snapshot summary (10-20 bullet facts): current
   headline positioning, named products/plans, prices seen, latest changelog entries
   with dates, notable news items. Only facts read from fetched pages.
4. Diff against the previous snapshot from step 1, bucketing into: **New**,
   **Changed** (state before → after), **Removed/no longer mentioned**, and
   **Unchanged highlights** (1-2 lines max). A page you couldn't fetch this time is
   "not checked", never "removed".
5. Store the new snapshot:
   `memory_write("competitor watch <competitor> <YYYY-MM-DD>: <snapshot bullets>")`.
   Keep it self-contained so a future run can diff from it alone. Do not
   `memory_forget` old snapshots — history enables trend answers.
6. Report (format below). On a baseline run, present the snapshot itself and say
   the first diff will come next run.
7. If a cadence was given or the user agrees when offered, create the recurring
   check: `schedule_create` with a cron (e.g. Mondays 09:00) whose task is "run
   Competitor Watch for <competitor>, focus <focus>". Mention `schedule_list` /
   `schedule_delete` for managing it. Scheduled runs should `deliver` the report to
   the user's preferred channel (default `in_app`).

## Output format

```
## Competitor watch: <Competitor> — <YYYY-MM-DD>
Previous snapshot: <date | none (baseline)>

### New
- <item> (source: <url>)
### Changed
- <thing>: <before> → <now> (source: <url>)
### Removed / no longer mentioned
- <item>
### Unchanged highlights
- <1-2 lines>

Snapshot saved to memory. Next check: <schedule | on request>.
```
Omit an empty section with "- none observed".

## Rules

- Public sources only — the competitor's own site and published coverage. Never log
  in anywhere, scrape private areas, or misrepresent who is asking.
- Diff only against the stored memory snapshot; never claim "changed" from your
  general knowledge of the company.
- Report prices/claims exactly as printed, with currency; changed pricing is the
  single most valuable diff item — check the pricing page every run.
- If all fetches fail, report the failure; do not overwrite the last good snapshot
  with an empty one.
- Egress is the competitor's public pages, search, and the user's delivery target
  only.
