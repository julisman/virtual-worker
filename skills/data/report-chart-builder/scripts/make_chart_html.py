#!/usr/bin/env python3
"""Build a self-contained HTML report with an inline SVG chart (no external
assets, no JS). Input CSV: first column = label, second column = numeric value,
first row = header. Usage:
  python3 make_chart_html.py <data.csv> [--type bar|line] [--title T] [-o out.html]
  python3 make_chart_html.py --self-test
"""
import csv, html, io, sys

W, H, PAD = 640, 360, 50


def read_pairs(reader):
    rows = list(reader)
    header, data = rows[0], []
    for r in rows[1:]:
        if len(r) >= 2 and r[1].strip():
            data.append((r[0].strip(), float(r[1].replace(",", "").strip())))
    if not data:
        raise SystemExit("No numeric rows found (need label,value).")
    return header, data


def svg_chart(data, kind):
    vmax = max(max(v for _, v in data), 0) or 1
    vmin = min(min(v for _, v in data), 0)
    span = (vmax - vmin) or 1
    plot_w, plot_h = W - 2 * PAD, H - 2 * PAD
    y = lambda v: PAD + plot_h * (1 - (v - vmin) / span)
    n = len(data)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" xmlns="http://www.w3.org/2000/svg" '
             f'style="max-width:100%;height:auto;font-family:sans-serif">']
    for frac in (0, 0.5, 1):
        gy = PAD + plot_h * frac
        val = vmax - span * frac
        parts.append(f'<line x1="{PAD}" y1="{gy:.1f}" x2="{W - PAD}" y2="{gy:.1f}" stroke="#ccc"/>')
        parts.append(f'<text x="{PAD - 6}" y="{gy + 4:.1f}" font-size="11" text-anchor="end">{val:g}</text>')
    if kind == "bar":
        bw = plot_w / n * 0.7
        for i, (label, v) in enumerate(data):
            x = PAD + plot_w * (i + 0.15) / n
            parts.append(f'<rect x="{x:.1f}" y="{min(y(v), y(0)):.1f}" width="{bw:.1f}" '
                         f'height="{abs(y(v) - y(0)):.1f}" fill="#2B3C82"/>')
    else:
        pts = " ".join(f"{PAD + plot_w * (i + 0.5) / n:.1f},{y(v):.1f}"
                       for i, (_, v) in enumerate(data))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#2B3C82" stroke-width="2"/>')
    for i, (label, _) in enumerate(data):
        x = PAD + plot_w * (i + 0.5) / n
        parts.append(f'<text x="{x:.1f}" y="{H - PAD + 16}" font-size="11" '
                     f'text-anchor="middle">{html.escape(label[:14])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def page(title, chart, caption):
    t = html.escape(title)
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{t}</title></head><body style='font-family:sans-serif;"
            f"max-width:720px;margin:2rem auto'><h1>{t}</h1>{chart}"
            f"<p><em>{html.escape(caption)}</em></p></body></html>")


SAMPLE = "month,sales\nJan,10\nFeb,25\nMar,18\n"


def self_test():
    _, data = read_pairs(csv.reader(io.StringIO(SAMPLE)))
    assert data == [("Jan", 10.0), ("Feb", 25.0), ("Mar", 18.0)], data
    doc = page("T", svg_chart(data, "bar"), "3 rows")
    assert doc.count("<rect") == 3 and "<script" not in doc and "http" not in doc.split("xmlns")[0], doc[:200]
    line = svg_chart(data, "line")
    assert "<polyline" in line and "Feb" in line
    print("SELF-TEST OK")


def main(argv):
    if "--self-test" in argv:
        self_test()
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    kind = argv[argv.index("--type") + 1] if "--type" in argv else "bar"
    title = argv[argv.index("--title") + 1] if "--title" in argv else "Chart"
    out = argv[argv.index("-o") + 1] if "-o" in argv else "chart.html"
    with open(argv[1], newline="", encoding="utf-8-sig") as f:
        header, data = read_pairs(csv.reader(f))
    caption = (f"{kind.capitalize()} chart of {header[1] if len(header) > 1 else 'value'} "
               f"by {header[0]} — {len(data)} data points.")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page(title, svg_chart(data, kind), caption))
    print(f"Wrote {out} ({len(data)} points, type={kind})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
