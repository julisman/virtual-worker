---
name: Invoice Processor
description: Turn a batch of invoice PDFs into one normalized CSV (vendor, invoice_no, date, currency, subtotal, tax, total), validating subtotal+tax against total and flagging mismatches and missing fields.
when_to_use: When the user has invoice/billing PDFs (English or Indonesian) and wants them consolidated into a spreadsheet-ready CSV for bookkeeping or review.
version: 1.0.0
---

# Invoice Processor

You read a batch of invoice PDFs and produce ONE normalized CSV plus a flag list
of everything that did not validate.

## Inputs

- Invoice PDFs in the working folder (if unnamed, `glob` for `*.pdf` and confirm
  the file list before processing).
- Output filename: default `invoices.csv`.
- Fixed columns, always in this order:
  `source_file, vendor, invoice_no, date, currency, subtotal, tax, total, flags`.

## Steps

1. Load the field guide first:
   `load_skill_resource({"slug": "invoice-processor", "path": "references/field-checklist.md"})`
   — it defines each field, the date/currency normalization rules, and label
   synonyms (English + Indonesian: Faktur, Jumlah, PPN, Tanggal, ...). Apply it
   throughout; do not improvise your own normalization.
2. For each PDF, call `document_read(path)`. Extract the seven fields using the
   checklist's synonyms. Values come from the document text only.
3. Normalize per the checklist: dates to ISO `YYYY-MM-DD`, amounts to plain
   dot-decimal numbers, currency to a 3-letter code.
4. Validate each invoice: `subtotal + tax ≈ total` (tolerance per the checklist).
   - Passes → `flags` empty.
   - Fails → keep the printed values, set `flags` to the mismatch note.
   - A field absent → leave its cell empty and add `missing:<field>` to `flags`.
     Never guess a missing value; derive one only when the checklist's
     derivation rule applies, and flag it as derived.
5. Write all rows to the output CSV with `write_file` (header + one row per PDF,
   including failed ones — flags make them visible).
6. Summarize in chat: totals per currency for clean rows only, then every
   flagged row with its reason.

## Output format

- `invoices.csv` in the working folder, exact columns from Inputs.
- Chat summary:

```
Processed <n> invoices → invoices.csv
Clean: <k> · Flagged: <m>
Sum of clean totals: <amount> <CUR> (per currency)

Flagged:
- <file>: <flag> (e.g. sum mismatch: subtotal+tax=110 but total=115)
- <file>: missing:invoice_no, date ambiguous (DD/MM assumed)
```

## Rules

- Never "fix" a printed amount to make the math work — report the mismatch;
  the source document is the truth, the flag is your judgment.
- One row per PDF, always, even unreadable ones (empty fields +
  `flags=unreadable`), so the CSV reconciles 1:1 with the input folder.
- A PDF that is clearly not an invoice (report, contract): row with
  `flags=not_an_invoice`, fields empty; mention it in chat.
- Multi-invoice PDFs (several invoices in one file): one row per invoice, same
  `source_file`, and note it in the summary.
- Do not mix decimal conventions within one document (checklist rule).
- Local-first: no network egress; the CSV stays in the working folder.
- Reply in the user's language; keep vendor names verbatim.
