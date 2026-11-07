#!/usr/bin/env python3
"""Summarize a log file: per-level counts, top error signatures, busiest time
buckets, first/last timestamp. Usage:
  python3 log_summary.py <file.log> [--top N]
  python3 log_summary.py --self-test
"""
import io, re, sys
from collections import Counter

TS = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})")
LEVEL = re.compile(r"\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\b", re.I)
HEXID = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
NUM = re.compile(r"\b\d+\b")


def signature(line):
    """Normalize a message: strip timestamp/level, mask ids and numbers."""
    line = TS.sub("", line)
    line = LEVEL.sub("", line, count=1)
    line = HEXID.sub("<id>", line)
    line = NUM.sub("<n>", line)
    return " ".join(line.replace("-", " ").split())[:120]


def summarize(lines, top=5):
    levels, sigs, buckets = Counter(), Counter(), Counter()
    first = last = None
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        m = TS.search(line)
        if m:
            stamp = f"{m[1]} {m[2]}:{m[3]}"
            first = first or stamp
            last = stamp
            buckets[f"{m[1]} {m[2]}:00"] += 1
        lv = LEVEL.search(line)
        name = lv[1].upper().replace("WARNING", "WARN") if lv else "OTHER"
        levels[name] += 1
        if name in ("ERROR", "CRITICAL", "FATAL"):
            sigs[signature(line)] += 1
    out = [f"Time range: {first or '?'} .. {last or '?'}",
           "Levels: " + (", ".join(f"{k}={v}" for k, v in levels.most_common()) or "none")]
    out.append(f"Top {top} error signatures:")
    out += [f"  {c:>5}x {s}" for s, c in sigs.most_common(top)] or ["  (no errors)"]
    out.append("Busiest hour buckets:")
    out += [f"  {c:>5}  {b}" for b, c in buckets.most_common(3)]
    return "\n".join(out)


SAMPLE = """2026-01-05 10:01:02 INFO started worker 17
2026-01-05 10:02:10 ERROR timeout fetching order 12345 from host-2
2026-01-05 10:12:11 ERROR timeout fetching order 99881 from host-7
2026-01-05 11:00:00 WARN queue depth 90
2026-01-05 11:00:05 ERROR db conn deadbeef1234 dropped
2026-01-05 11:00:09 INFO done
"""


def self_test():
    rep = summarize(io.StringIO(SAMPLE), top=3)
    assert "Time range: 2026-01-05 10:01 .. 2026-01-05 11:00" in rep, rep
    assert "ERROR=3" in rep and "INFO=2" in rep and "WARN=1" in rep, rep
    assert "timeout fetching order <n> from host <n>" in rep and "2x" in rep, rep
    assert "3  2026-01-05 11:00" in rep and "3  2026-01-05 10:00" in rep, rep
    print(rep)
    print("SELF-TEST OK")


def main(argv):
    if "--self-test" in argv:
        self_test()
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 5
    with open(argv[1], encoding="utf-8", errors="replace") as f:
        print(summarize(f, top=top))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
