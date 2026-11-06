---
name: Stock Price Checker
description: Look up a stock quote from a public quote page — resolve ticker and exchange, report price, day change, and quote time, and flag that data may be delayed. Never gives investment advice.
when_to_use: When the user asks for a stock's current price, how a ticker or listed company is doing today, or a quick quote (e.g. "check SE", "harga saham GOTO", "how is NVDA doing").
version: 1.0.0
---

# Stock Price Checker

Report a stock's latest public quote: price, day change, time of quote — clearly
labeled as possibly delayed, with no investment advice.

## Inputs

- **ticker or company name** — e.g. "SE", "Sea Limited", "GOTO".
- **exchange/market** — optional; needed when the symbol trades on several markets.

## Steps

1. Resolve the symbol. If given a company name, `browser_search("<name> stock
   ticker")` to find it. If the input is ambiguous — the same symbol exists on
   multiple exchanges or matches several companies (e.g. "SE" is Sea Limited on
   NYSE but also a suffix/symbol elsewhere) — STOP and ask, listing the candidates
   as "TICKER — Company — Exchange". Exception: one candidate is a well-known major
   listing and the others are obscure ⇒ proceed with the major one and state the
   assumption in the report.
2. Get the quote: `browser_search("<ticker> <exchange> stock price")` and
   `browser_fetch` a public quote page from the results (major finance portals such
   as Google Finance, Yahoo Finance, or the exchange's own site). Do not use any
   paid API.
3. Extract from the fetched page: last price + currency, day change (absolute and
   %), the as-of timestamp shown on the page (with its timezone), and whether the
   market is open/closed or the quote is pre/post-market if shown.
4. If the page yields no readable numbers, try ONE alternate quote page. If that
   also fails, report that you could not retrieve a quote — never estimate a price.
5. Report using the format below. Multiple tickers requested ⇒ repeat steps 1-4 per
   ticker and output one block each.

## Output format

```
**<Company> (<EXCHANGE>: <TICKER>)**
Price: <currency> <price>
Day change: <+/-amount> (<+/-x.xx%>)
As of: <timestamp + timezone from the page> · Market: <open/closed/after-hours, if shown>
Source: <url>
_Note: quote from a public page; data may be delayed 15-20 minutes._
```
If you assumed an exchange, add one line: "Assumed <exchange> listing — say the
word if you meant another."

## Rules

- **Never give investment advice** — no buy/sell/hold, no "good entry point", no
  price predictions. If asked, decline that part and offer the factual quote,
  history, or company facts instead.
- Always include the delayed-data note and the source URL; report the page's own
  as-of time, never the current wall-clock time as the quote time.
- Report numbers exactly as printed — no rounding beyond what the page shows, and
  never mix numbers from two different pages in one quote block.
- Ambiguous ticker ⇒ ask with candidates (step 1); never silently pick an obscure
  listing.
- If the market is closed, say so and label the price as the last close (or
  pre/post-market where the page says so).
- Egress is search + public quote pages only; nothing is sent anywhere unless the
  user asks for delivery via `deliver`.
- Reply in the user's language; keep ticker, exchange, and currency codes as-is.
