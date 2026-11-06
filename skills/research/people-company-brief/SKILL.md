---
name: People Company Brief
description: Prepare a pre-meeting brief on a person or company from public sources only — role, org, recent news and writing, talking points, and things in common with the user drawn from memory.
when_to_use: When the user is about to meet, call, interview, or pitch someone (or a company) and wants background — "brief me on X", "who am I meeting", "prep me for the call with Acme".
version: 1.0.0
---

# People Company Brief

Build a pre-meeting brief on a person or company from PUBLIC sources: who they are,
what they've done recently, talking points, and common ground with the user.

## Inputs

- **subject** — the person (name + org/role if known) or company.
- **meeting context** — optional but valuable: why the user is meeting them
  (sales call, interview, partnership). Shapes the talking points.
- **disambiguators** — for common names, ask for one anchor (company, city, title)
  before searching.

## Steps

1. Identify the subject. `browser_search("<name> <org/anchor>")` (person) or
   `browser_search("<company> about")` (company). Confirm you have ONE consistent
   identity across results — same person across pages, not a namesake. If results
   plausibly mix two people, either ask the user or clearly brief only the
   best-match identity and flag the uncertainty prominently.
2. `browser_fetch` the core public pages: for a person — their org bio page,
   personal site/blog, public professional profile, recent talks/interviews; for a
   company — the about/product pages, newsroom, and recent coverage found via
   `browser_search("<company> news")`. 4-6 fetches is usually enough.
3. Collect recent output: articles or posts they wrote, talks given, product
   launches, funding/press events — prefer the last 12 months.
4. Find common ground with the user: `memory_search` for the user's role, projects,
   interests, past companies, and any prior contact with this subject; if a
   knowledge base is attached, `kb_search("<subject>")` for prior internal notes.
   Match against the subject's background (shared industry, technology, city,
   alma mater, mutual topics). If memory is empty or nothing overlaps, say "none
   found" — do not manufacture common ground.
5. Draft talking points tied to the meeting context: 3-5 openers or questions
   grounded in something specific and recent from your research.
6. Output the brief (format below). Offer `write_file` to save it in the working
   folder for offline reading before the meeting.

## Output format

```
## Brief: <Subject> — <YYYY-MM-DD>
Identity confidence: <confirmed | uncertain — see flag>

**Who**: <role, org, one-line background> [url]
**The org** (person briefs only): <what the company does, size/stage if public> [url]
**Recent** (last ~12 months):
- <news/writing/talk item, dated> [url]
**Talking points**:
1. <specific, grounded opener or question>
**In common with you**: <matches from memory, each with its basis; or "none found">
**Watch out**: <sensitivities visible in public record — recent layoffs, disputes —
or "nothing notable">
```

## Rules

- **Public sources only.** Never scrape or report private contact details (personal
  email, phone, home address) even when findable; work email published on an
  official page may be included only if the user explicitly asks for contact info.
- No pretexting: never impersonate anyone or sign up anywhere to obtain access.
- **Flag uncertain identity matches** — if any fetched fact might belong to a
  namesake, mark that line "(unverified — possible different <Name>)" and set
  identity confidence accordingly. A wrong-person brief is worse than a thin one.
- Everything factual carries a source URL from a page you fetched; keep opinions
  out — "Watch out" items must be publicly reported facts, not speculation.
- This brief is for meeting prep, not surveillance: refuse requests to track a
  private individual's location, routine, or private life.
- The brief goes only to the user (or their named `deliver` target). Do not store
  the subject's details with `memory_write` unless the user asks you to remember
  the meeting/contact.
