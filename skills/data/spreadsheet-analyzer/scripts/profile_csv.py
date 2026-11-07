#!/usr/bin/env python3
"""Profile a CSV: row count, per-column type guess, stats. Usage:
  python3 profile_csv.py <file.csv>
  python3 profile_csv.py --self-test
"""
import csv, io, sys
from collections import Counter


def is_num(s):
    try:
        float(s.replace(",", "").strip())
        return True
    except ValueError:
        return False


def profile(reader):
    rows = list(reader)
    if not rows:
        return "Empty file: no rows."
    header, data = rows[0], rows[1:]
    out = [f"Rows: {len(data)}  Columns: {len(header)}"]
    for i, col in enumerate(header):
        vals = [r[i].strip() if i < len(r) else "" for r in data]
        nonempty = [v for v in vals if v]
        empty = len(vals) - len(nonempty)
        nums = [float(v.replace(",", "")) for v in nonempty if is_num(v)]
        if nonempty and len(nums) == len(nonempty):
            kind = "numeric"
            mean = sum(nums) / len(nums)
            stats = f"min={min(nums):g} max={max(nums):g} mean={mean:.4g}"
        else:
            kind = "text"
            top = Counter(nonempty).most_common(5)
            stats = "top5: " + ", ".join(f"{v!r} x{c}" for v, c in top) if top else "all empty"
        out.append(f"- {col} [{kind}] empty={empty} | {stats}")
    return "\n".join(out)


SAMPLE = """name,amount,city
Ana,10,Jakarta
Budi,20.5,Bandung
Cici,,Jakarta
Ana,30,Surabaya
"""


def self_test():
    rep = profile(csv.reader(io.StringIO(SAMPLE)))
    assert "Rows: 4" in rep, rep
    assert "amount [numeric] empty=1 | min=10 max=30 mean=20.17" in rep, rep
    assert "'Jakarta' x2" in rep, rep
    assert "name [text] empty=0" in rep, rep
    print(rep)
    print("SELF-TEST OK")


def main(argv):
    if len(argv) == 2 and argv[1] == "--self-test":
        self_test()
        return 0
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[1], newline="", encoding="utf-8-sig") as f:
        print(profile(csv.reader(f)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
