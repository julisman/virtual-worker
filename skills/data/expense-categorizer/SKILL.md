---
name: Expense Categorizer
description: Categorize a transactions CSV into spending categories using an editable keyword ruleset (incl. Indonesian merchants), produce a monthly rollup per category, and list uncategorized rows for the user.
when_to_use: When the user has bank/e-wallet/card transaction exports as CSV and wants to know where the money went — categorized totals per month rather than raw rows.
version: 1.0.0
---

# Expense Categorizer

You classify each transaction row by keyword rules, roll spending up per
category per month, and hand uncategorized rows back to the user instead of
guessing.

## Inputs

- Path to a transactions `.csv` in the working folder (if unnamed, `glob` for
  `*.csv` and confirm). Identify the date, description/merchant, and amount
  columns from the header; ask if ambiguous.
- Optional: a period to focus on; whether to save the report
  (`write_file` as `expense-report.md`) or keep it chat-only (default).

## Steps

1. Load the ruleset:
   `load_skill_resource({"slug": "expense-categorizer", "path": "references/category-rules.md"})`
   — categories with keyword lists (incl. Indonesian merchants: indomaret,
   gojek, grab, tokopedia, ...), ordering rules (specific before generic, e.g.
   grabfood before grab), and the transfers/income notes.
2. Check stored user rules: `memory_search("expense category rule")` — apply any
   saved personal rules ON TOP of the reference ruleset (user rules win).
3. Read the CSV with `document_read(path)`. Normalize dates enough to bucket by
   month (`YYYY-MM`); normalize amounts (decimal commas, negative = spend if the
   export uses signed amounts).
4. Categorize each row: lowercase the description, first matching keyword wins,
   respecting the ruleset's ordering and the transfers/income exclusions. No
   keyword match → `uncategorized`. Never assign a category by vibes.
5. Build the rollup: per month x category totals, category share of total spend,
   and the biggest single transaction per category as an anchor example.
6. List every uncategorized row (date, description, amount) and ask the user to
   classify them. When they answer, restate each as a rule
   ("kopi kenangan → dining") and offer to persist it with
   `memory_write("expense category rule: <keyword> → <category>")` so future
   runs pick it up in step 2.
7. Recompute totals after the user classifies, if they do.

## Output format

```
## Spend by category — <file>
| Month | groceries | transport | dining | ... | uncategorized | Total |
|---|---|---|---|---|---|---|

Top categories: 1. dining 32% (Rp X) ...
Largest transactions: dining — Rp 450.000 "GRABFOOD*..." (2026-05-03)

## Uncategorized (N rows) — please classify
- 2026-05-11 · "KOPI KENANGAN JKT" · 28.000
Offer: save your answers as personal rules (memory_write)?
```

## Rules

- Totals must add up: sum of category totals + uncategorized = file total per
  month; state both so the user can verify.
- Transfers between own accounts / e-wallet top-ups are shown separately, never
  inside spending categories (ruleset note) — double counting is the classic bug.
- `income` rows only from credit-direction amounts, never keyword-only.
- Never invent a category for an unknown merchant — uncategorized is the honest
  answer, and the user's classification becomes a saved rule only with their OK.
- Only write memory when the user agrees; keep rules short and keyword-based so
  they audit well in `memory_search`.
- Financial data stays local: no delivery off-machine unless explicitly requested.
- Reply in the user's language; keep merchant strings verbatim in listings.
