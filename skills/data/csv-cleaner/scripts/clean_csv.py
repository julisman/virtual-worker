#!/usr/bin/env python3
"""Clean a messy CSV. Writes <name>.clean.csv next to the input and prints a
change report. Usage:
  python3 clean_csv.py <file.csv>
  python3 clean_csv.py --self-test
Fixes: trim whitespace, drop blank rows, dedupe exact rows, normalize common
date formats to ISO (YYYY-MM-DD), normalize decimal commas (1.234,56 -> 1234.56).
"""
import csv, io, re, sys

DATE_PATS = [
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), lambda m: (m[3], m[2], m[1])),  # DD/MM/YYYY
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$"), lambda m: (m[3], m[2], m[1])),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"), lambda m: (m[1], m[2], m[3])),
]
DEC_COMMA = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d+$|^-?\d+,\d+$")


def clean(rows):
    stats = {"trimmed": 0, "blank_rows": 0, "dupes": 0, "dates": 0, "decimals": 0}
    out, seen = [], set()
    for row in rows:
        cells = []
        for c in row:
            v = c.strip()
            if v != c:
                stats["trimmed"] += 1
            for pat, fn in DATE_PATS:
                m = pat.match(v)
                if m:
                    y, mo, d = fn(m)
                    v = f"{y}-{int(mo):02d}-{int(d):02d}"
                    stats["dates"] += 1
                    break
            if DEC_COMMA.match(v):
                v = v.replace(".", "").replace(",", ".")
                stats["decimals"] += 1
            cells.append(v)
        if not any(cells):
            stats["blank_rows"] += 1
            continue
        key = tuple(cells)
        if key in seen:
            stats["dupes"] += 1
            continue
        seen.add(key)
        out.append(cells)
    return out, stats


def report(stats, kept):
    return (f"Change report: trimmed cells={stats['trimmed']}, blank rows removed="
            f"{stats['blank_rows']}, exact duplicates removed={stats['dupes']}, "
            f"dates normalized={stats['dates']}, decimal commas fixed={stats['decimals']}. "
            f"Rows kept: {kept}.")


SAMPLE = 'name,date,amount\n Ana ,31/12/2024,"1.234,56"\n,,\nBudi,2024/1/5,"7,5"\nBudi,2024-01-05,7.5\n Ana ,31/12/2024,"1.234,56"\n'


def self_test():
    rows, stats = clean(csv.reader(io.StringIO(SAMPLE)))
    assert rows[1] == ["Ana", "2024-12-31", "1234.56"], rows
    assert rows[2] == ["Budi", "2024-01-05", "7.5"], rows
    assert len(rows) == 3 and stats["blank_rows"] == 1, (rows, stats)
    assert stats["dupes"] == 2 and stats["dates"] == 3 and stats["decimals"] == 3, stats
    assert stats["trimmed"] == 2, stats
    print(report(stats, len(rows)))
    print("SELF-TEST OK")


def main(argv):
    if len(argv) == 2 and argv[1] == "--self-test":
        self_test()
        return 0
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = argv[1]
    with open(src, newline="", encoding="utf-8-sig") as f:
        rows, stats = clean(csv.reader(f))
    dst = re.sub(r"\.csv$", "", src, flags=re.I) + ".clean.csv"
    with open(dst, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    print(f"Wrote {dst}")
    print(report(stats, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
