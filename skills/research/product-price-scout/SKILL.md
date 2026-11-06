---
name: Product Price Scout
description: Find a product's price and availability across online shops — search for offers, fetch shop pages (control browser for JS-heavy storefronts), and output a comparison table of shop, item, price, availability, link.
when_to_use: When the user wants to know where to buy a product, compare its price across stores, or check if a specific item is in stock ("find the best price for X", "cek harga X di toko online").
version: 1.0.0
---

# Product Price Scout

Find where a product is sold and at what price, and present a shop-by-shop
comparison table.

## Inputs

- **product** — as specific as possible: brand, model, variant (size/color/storage).
  Vague input ("a good SSD") ⇒ ask for the exact model first.
- **currency/region** — REQUIRED before searching. If not stated, ask one question:
  "Which country/currency should I price this in?" Prices differ per market.
- **shops** — optional list of preferred stores; otherwise use whatever reputable
  shops the search surfaces (target 3-6 offers).

## Steps

1. Confirm product identity and currency/region (Inputs). Note the exact model
   string to match against listings.
2. `browser_search("<product> price <region>")` and
   `browser_search("buy <product> <region>")`. Collect candidate shop offers,
   preferring official stores and major retailers/marketplaces for that region.
3. For each candidate, `browser_fetch` the product page and extract: shop name,
   exact listed item name, price + currency, availability (in stock / out of
   stock / preorder), and the URL.
4. If a page comes back empty or scripted (JS-heavy storefront — common for
   marketplaces), switch to the control browser: `browser_open(url)`, then
   `browser_read` to extract the price and stock state. If the offer must be found
   via in-site search, `browser_type` the product name into the site's search box,
   `browser_press` Enter (or `browser_click` the search button), `browser_click`
   the matching result, then `browser_read` the product page. Use `browser_snapshot`
   when you need element structure to click reliably.
5. If the control browser is unavailable, degrade gracefully: keep only
   `browser_fetch`-readable offers, and list JS-only shops in the report as
   "price not readable without the control browser" rather than guessing.
6. Verify each offer is the SAME item (model number/variant matches). Mismatched
   variants either drop out or get an explicit "(different variant: ...)" tag.
7. Output the comparison table (format below), cheapest available offer first.

## Output format

```
## Price scout: <product> — <YYYY-MM-DD> (<region>, <currency>)

| Shop | Item name (as listed) | Price | Availability | Link |
|---|---|---|---|---|
| <shop> | <listed name> | <currency> <price> | In stock | <url> |

Best available price: <shop> at <price>.
Notes: <shipping/tax not included; variant mismatches; shops skipped and why>
_Prices are as shown at check time and can change; shipping and promos not included
unless printed on the page._
```

## Rules

- Every price must come from a page you actually opened (`browser_fetch` or
  `browser_read`) — never from a search snippet or memory. No offer found on a shop
  ⇒ omit the shop; never estimate a price.
- One currency per table (the confirmed one); a shop pricing in another currency is
  listed with its original price and marked "(other currency)" — do not convert
  silently.
- Record prices exactly as displayed, including decimals; note visible strike-through
  promo pricing as "promo".
- In the control browser, only browse and read public storefronts: never log in,
  never add to cart, never check out, never enter any user data into forms other
  than the product name in a search box.
- If most target shops are unreadable by both methods, say so and deliver the table
  with what you have — partial data beats invented data.
- Egress is search + the shop pages themselves; results go only to the user (or via
  `deliver` if they name a channel).
