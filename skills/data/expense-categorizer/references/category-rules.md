# Expense category rules (editable)

Match case-insensitively against the transaction description/merchant field.
First matching category wins; rules are checked top to bottom. Edit this file
(or store user-specific additions with `memory_write`) to tune the ruleset.

## Categories and keywords

| Category | Keywords (substring match) |
|---|---|
| groceries | indomaret, alfamart, alfamidi, supermarket, hypermart, superindo, ranch market, grand lucky, grocery, minimarket, sayurbox, segari |
| transport | gojek, goride, gocar, grab, bluebird, blue bird, mrt, transjakarta, krl, commuterline, kai, parking, parkir, toll, e-toll, tol, fuel, pertamina, shell, spbu, bensin |
| dining | gofood, grabfood, restaurant, resto, warung, kopi, coffee, starbucks, kfc, mcd, mcdonald, cafe, bakery, padang, bakso, ayam |
| shopping | tokopedia, lazada, blibli, bukalapak, amazon, uniqlo, zara, ikea, ace hardware, mall |
| subscriptions | netflix, spotify, youtube premium, disney, vidio, icloud, google one, apple.com/bill, adobe, canva, chatgpt, openai, github, notion, subscription, langganan |
| utilities | pln, listrik, electricity, pdam, air, water, indihome, first media, biznet, myrepublic, telkomsel, xl axiata, indosat, im3, smartfren, by.u, pulsa, token, internet, wifi |
| health | apotek, apotik, pharmacy, guardian, watsons, kimia farma, klinik, clinic, hospital, rumah sakit, rs , dokter, doctor, bpjs, halodoc, alodokter |
| housing | rent, sewa, kost, kos-kosan, mortgage, kpr, ipl, service charge, maintenance fee |
| entertainment | cinema, cgv, xxi, cinepolis, concert, tiket.com, ticket, karaoke, steam, playstation, nintendo, game |
| travel | hotel, airbnb, traveloka, agoda, booking.com, garuda, lion air, citilink, airasia, batik air, whoosh, train, kereta, flight |
| education | udemy, coursera, ruangguru, zenius, course, kursus, sekolah, school, university, buku, gramedia, book |
| fees | admin fee, biaya admin, bank fee, transfer fee, biaya transfer, interest, bunga, tax, pajak, materai |
| income | salary, gaji, payroll, refund, cashback, bonus, dividend, interest received |

## Notes

- `income` should only match credit/positive rows if the CSV distinguishes
  direction — do not classify a debit as income on keyword alone.
- Transfers between the user's own accounts (keywords: "transfer to own",
  "top up gopay", "top up ovo", "topup e-wallet", "dana top") are `transfers`,
  not spending — exclude them from category totals but show them separately.
- Ambiguous merchants (e.g. "grab" could be transport or food): `grabfood` must
  be tested BEFORE `grab`; keep more specific keywords earlier in the table.
- Anything unmatched → `uncategorized`; never guess a category without a keyword
  or explicit user confirmation.
