#!/usr/bin/env python3
"""Dedupe a contacts CSV. Groups rows that share a normalized email OR phone OR
a name with >=0.85 difflib similarity; keeps the first row of each group and
writes <name>.deduped.csv plus a merge report to stdout. Nothing is lost
silently: every merged group and every conflicting field is listed. Usage:
  python3 dedupe_contacts.py <contacts.csv>
  python3 dedupe_contacts.py --self-test
Expects columns containing 'name', 'email', 'phone' (case-insensitive).
"""
import csv, difflib, io, re, sys


def col(header, want):
    for i, h in enumerate(header):
        if want in h.lower():
            return i
    return None


def norm_email(v):
    return v.strip().lower()


def norm_phone(v):
    d = re.sub(r"\D", "", v)
    return d[-10:] if len(d) >= 8 else d if d else ""


def norm_name(v):
    return " ".join(v.lower().split())


def group_rows(header, data):
    ie, ip, iname = col(header, "email"), col(header, "phone"), col(header, "name")
    get = lambda r, i: r[i].strip() if i is not None and i < len(r) else ""
    groups = []  # each: dict(rows=[...], emails=set, phones=set, names=set)
    for row in data:
        e, p = norm_email(get(row, ie)), norm_phone(get(row, ip))
        n = norm_name(get(row, iname))
        hit = None
        for g in groups:
            if (e and e in g["emails"]) or (p and p in g["phones"]) or (
                n and any(difflib.SequenceMatcher(None, n, x).ratio() >= 0.85
                          for x in g["names"])):
                hit = g
                break
        if hit is None:
            hit = {"rows": [], "emails": set(), "phones": set(), "names": set()}
            groups.append(hit)
        hit["rows"].append(row)
        if e: hit["emails"].add(e)
        if p: hit["phones"].add(p)
        if n: hit["names"].add(n)
    return groups


def merge_report(header, groups):
    lines = []
    for gi, g in enumerate((g for g in groups if len(g["rows"]) > 1), 1):
        lines.append(f"Group {gi}: kept row 1 of {len(g['rows'])}")
        for r in g["rows"]:
            lines.append("  - " + ", ".join(r))
        keep = g["rows"][0]
        for i, h in enumerate(header):
            vals = {r[i].strip() for r in g["rows"] if i < len(r) and r[i].strip()}
            if len(vals) > 1:
                lines.append(f"  ! conflict in '{h}': kept {keep[i]!r}, also saw "
                             + ", ".join(repr(v) for v in sorted(vals) if v != keep[i]))
    return lines or ["No duplicate groups found."]


SAMPLE = """Name,Email,Phone
Agus Salim,agus@mail.com,0812-3456-7890
AGUS SALIM ,Agus@Mail.com,+62 812 3456 7890
Agoes Salim,,081299999999
Rina Wati,rina@mail.com,555
"""


def self_test():
    rows = list(csv.reader(io.StringIO(SAMPLE)))
    groups = group_rows(rows[0], rows[1:])
    sizes = sorted(len(g["rows"]) for g in groups)
    assert sizes == [1, 3], sizes  # Agus x3 (email+name match), Rina alone
    rep = "\n".join(merge_report(rows[0], groups))
    assert "kept row 1 of 3" in rep and "conflict in 'Phone'" in rep, rep
    print(rep)
    print("SELF-TEST OK")


def main(argv):
    if "--self-test" in argv:
        self_test()
        return 0
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[1], newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        print("Empty file.")
        return 1
    groups = group_rows(rows[0], rows[1:])
    dst = re.sub(r"\.csv$", "", argv[1], flags=re.I) + ".deduped.csv"
    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(rows[0])
        w.writerows(g["rows"][0] for g in groups)
    print(f"Wrote {dst}: {len(groups)} unique contacts from {len(rows) - 1} rows.")
    print("\n".join(merge_report(rows[0], groups)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
