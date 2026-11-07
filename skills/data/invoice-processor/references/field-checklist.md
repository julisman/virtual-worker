# Invoice field checklist

The seven output columns, in order, and how to normalize each one.

## Field definitions

| Column | Meaning | Format |
|---|---|---|
| `vendor` | The party issuing the invoice (seller), not the recipient | verbatim company name, trimmed |
| `invoice_no` | The vendor's invoice identifier | verbatim, keep leading zeros, no spaces |
| `date` | Invoice issue date (NOT due date, NOT delivery date) | ISO `YYYY-MM-DD` |
| `currency` | 3-letter ISO code | `IDR`, `USD`, `SGD`, `EUR`, … |
| `subtotal` | Sum before tax | plain number, dot decimal, no thousands separators |
| `tax` | Tax amount (VAT/PPN/GST). 0 if explicitly zero; empty if absent | plain number |
| `total` | Grand total payable | plain number |

## Label synonyms (what to look for on the page)

| Field | English labels | Indonesian labels |
|---|---|---|
| invoice | Invoice, Invoice No., Inv #, Bill No. | Faktur, No. Faktur, Nomor Invoice, Kwitansi |
| date | Date, Invoice Date, Issued | Tanggal, Tgl., Tanggal Faktur |
| subtotal | Subtotal, Net Amount, Amount before tax | Subtotal, Jumlah, DPP (Dasar Pengenaan Pajak) |
| tax | VAT, GST, Sales Tax, Tax | PPN, Pajak, PPN 11% |
| total | Total, Grand Total, Amount Due, Balance Due | Total, Jumlah Total, Total Tagihan, Terbilang (spelled-out total) |
| vendor | From, Seller, Supplier, Billed by | Penjual, Dari, Penerbit |

## Date normalization

- `31/12/2024`, `31-12-2024` → assume day-first (common in ID/EU) → `2024-12-31`.
- `12/31/2024` (month > 12 impossible in day slot) → US order → `2024-12-31`.
- Ambiguous like `05/04/2024` with no other cue: prefer day-first, and FLAG the
  row in the mismatch notes as `date ambiguous (DD/MM assumed)`.
- Month names: `31 Desember 2024`, `Dec 31, 2024` → `2024-12-31`.
  Indonesian months: Januari, Februari, Maret, April, Mei, Juni, Juli, Agustus,
  September, Oktober, November, Desember.

## Currency & amount normalization

- `Rp` / `Rp.` / `IDR` → `IDR`. `$` alone → `USD` unless the document says
  otherwise (e.g. `S$` → `SGD`). `€` → `EUR`.
- Indonesian number format uses dots for thousands and comma for decimals:
  `Rp 1.234.567,89` → `1234567.89`. English format `1,234,567.89` → `1234567.89`.
- Decide the convention per document from the largest amount, apply it to every
  amount in that document — never mix conventions within one invoice.
- IDR amounts are usually whole numbers; a parsed IDR value with a tiny fraction
  like `1234.567` most likely mis-read thousands dots — re-check.

## Validation

- Check `subtotal + tax ≈ total` with tolerance `0.05` (or `1` for IDR).
- If only two of the three are present, derive the third and note `derived:<field>`.
- If the check fails, keep the values as printed and flag
  `sum mismatch: subtotal+tax=<x> but total=<y>` — never "correct" a printed total.
