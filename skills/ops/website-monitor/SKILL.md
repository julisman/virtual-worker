---
name: Website Monitor
description: Watch a specific section of a web page for real content changes — fetch, fingerprint, compare against the last snapshot stored in memory, and alert via deliver() only when something actually changed.
when_to_use: When the user asks to watch/monitor a page, "tell me when this updates", track a price/status/announcement, or when a scheduled page-check run fires with a URL to re-check.
version: 1.0.0
---

# Website Monitor

Watch one page section for meaningful changes and alert only on a real change — never on noise.

## Inputs

- **URL** — the page to watch (required).
- **Watched section** — a plain-language description of what part matters (e.g. "the price next to the product title", "the Changelog list", "the row for Jakarta"). Default: the page's main content.
- **Delivery** — channel + target for alerts. Default: `deliver("in_app", ...)` to the user.
- **Cadence** — optional; only if the user wants recurring checks.

## Steps

1. `browser_fetch` the URL. If the fetch fails, report the error — do NOT alert a change.
2. Extract only the watched section from the page text, guided by the user's description. Ignore everything else on the page.
3. Normalize the extract (apply the noise rules below), then build a **fingerprint record**: the URL, today's date, a short summary of the section, and the key values verbatim (price, version, headline, etc.).
4. `memory_search` for the previous record — query with `"website-monitor <URL>"`.
5. Compare the new normalized extract against the most recent stored record:
   - **No previous record** → this is the baseline. Store it (step 6), tell the user "baseline captured, no comparison yet". Do not alert.
   - **Same** → store nothing new is required; report "no change since <previous date>". Do not call `deliver`.
   - **Changed** → state exactly what changed (old value → new value), then alert via `deliver(channel, target, text)` with a short diff-style message.
6. `memory_write` the new fingerprint record, prefixed `website-monitor <URL> | <YYYY-MM-DD>:` so future runs can find and order it.
7. If the user wants this recurring and no schedule exists yet, offer `schedule_create` with a **self-contained** prompt (URL + watched-section description + delivery target all inside the prompt, no reliance on this chat), then confirm the cadence back to them.

## Output format

One short report per run:

```
Website Monitor — <URL>
Checked: <date> · Previous: <date or "none (baseline)">
Result: NO CHANGE | CHANGED
<if changed> What changed: <old> → <new>
<if changed> Alert sent via <channel> to <target>.
```

## Rules

- **Noise rules — never treat these as a change:** timestamps/"last updated" lines, dates that merely tick forward, view/like/visitor counters, ads and promo banners, cookie notices, rotating "related articles", session IDs or cache-buster query strings in links. Compare *content*, not page chrome.
- Alert **only** on a real change in the watched section. A changed page outside the watched section is not an alert — mention it in one line at most.
- Never alert on a failed or empty fetch; a site being down is reported as an error, not a change.
- Local-first: fetch only the named URL; deliver only to the named target. No other egress.
- If the same change was already alerted (memory shows an identical latest record), do not re-alert.
- Keep memory tidy: one record per check; if the user stops the watch, offer `schedule_delete` and `memory_forget` for the old records.
