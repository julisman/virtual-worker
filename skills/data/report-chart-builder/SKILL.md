---
name: Report Chart Builder
description: Turn a small dataset (CSV file or pasted values) into a self-contained HTML report with an inline SVG bar or line chart — no external assets or JS — saved to the working folder.
when_to_use: When the user wants a quick visual — "chart this", "make a graph of these numbers", "turn this into something I can show" — from a small label/value dataset.
version: 1.0.0
---

# Report Chart Builder

You turn a small label/value dataset into a single HTML file with an inline SVG
chart, built by `scripts/make_chart_html.py`, and tell the user to open it.

## Inputs

- The data: either a CSV path in the working folder, or values pasted in chat.
  Required shape: first column = label, second column = numeric value, first
  row = header. Pasted data: write it to `chart-data.csv` with `write_file` first.
- Chart type: `bar` (default; categories) or `line` (ordered sequence/time).
- Title (ask or derive from the data), output name (default `chart.html`).

## Steps

1. Get the data into a CSV in the working folder. For an existing file, check
   the shape with `read_file` — if there are extra columns, confirm which value
   column to use and write a 2-column copy with `write_file`.
2. Pick the chart type: time/sequence on the label axis → `line`; discrete
   categories → `bar`. One sentence to the user on the choice; obey an explicit
   user preference.
3. Fetch the builder:
   `load_skill_resource({"slug": "report-chart-builder", "path": "scripts/make_chart_html.py"})`,
   save it into the working folder with `write_file` as `make_chart_html.py`.
4. Run it:
   `shell("python3 make_chart_html.py chart-data.csv --type bar --title 'Sales by month' -o chart.html")`.
   It writes a complete HTML page (title, inline SVG, caption) with zero
   external assets and no JavaScript.
5. Verify with `read_file` on the output: the `<title>` is right and the SVG
   contains the expected number of bars/points (the script prints the count).
6. Tell the user the file is ready and to open it (double-click / drag into a
   browser). Do not paste the raw HTML into chat.

## Output format

```
Chart ready: chart.html (in your working folder — open it in a browser)
- type: bar · points: 12 · title: "Sales by month"
- data: chart-data.csv (from <source>)
One-line reading of the chart: <e.g. Feb peaks at 25, dip in Mar>.
```

## Rules

- Small data only: this is for up to a few dozen points. Hundreds of rows →
  aggregate first (offer to sum per month/category) so labels stay readable.
- Non-numeric second column or empty data: the script exits with an error —
  relay it and help fix the CSV; never chart made-up numbers.
- Exactly one series: two value columns means two runs producing two HTML files,
  or the user picks one — the script does not do multi-series.
- The output must stay self-contained: never add external fonts, CDN scripts,
  or tracking to the HTML; that is the point of the inline-SVG approach.
- If `shell` is unavailable: for a handful of points, hand-write the same style
  of self-contained HTML+SVG via `write_file` and say so; otherwise ask the
  user to enable shell.
- Local-first: the chart is a file in the working folder; no publishing, no
  network. Reply in the user's language.
