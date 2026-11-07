#!/usr/bin/env python3
"""Transform JSON files. Usage:
  python3 json_tool.py flatten <in.json> [-o out.json]     # nested -> dot-key flat
  python3 json_tool.py tocsv   <in.json> [-o out.csv]      # array of objects -> CSV
  python3 json_tool.py filter  <in.json> --path a.b --equals X [-o out.json]
  python3 json_tool.py --self-test
"""
import csv, json, sys


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        items = obj.items()
    elif isinstance(obj, list):
        items = ((str(i), v) for i, v in enumerate(obj))
    else:
        return {prefix or "value": obj}
    for k, v in items:
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, (dict, list)):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def get_path(obj, path):
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def tocsv_rows(data):
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise SystemExit("tocsv needs a JSON array of objects")
    flat = [flatten(r) for r in data]
    cols = []
    for r in flat:
        cols += [k for k in r if k not in cols]
    return [cols] + [[("" if r.get(c) is None else r.get(c)) for c in cols] for r in flat]


def filt(data, path, equals):
    if not isinstance(data, list):
        raise SystemExit("filter needs a JSON array")
    return [r for r in data if str(get_path(r, path)) == equals]


SAMPLE = [{"id": 1, "user": {"name": "Ana", "tags": ["a", "b"]}, "ok": True},
          {"id": 2, "user": {"name": "Budi"}, "ok": False}]


def self_test():
    f = flatten(SAMPLE[0])
    assert f == {"id": 1, "user.name": "Ana", "user.tags.0": "a", "user.tags.1": "b", "ok": True}, f
    rows = tocsv_rows(SAMPLE)
    assert rows[0] == ["id", "user.name", "user.tags.0", "user.tags.1", "ok"], rows
    assert rows[2] == [2, "Budi", "", "", False], rows
    kept = filt(SAMPLE, "user.name", "Budi")
    assert len(kept) == 1 and kept[0]["id"] == 2, kept
    assert filt(SAMPLE, "id", "1")[0]["id"] == 1
    print("SELF-TEST OK")


def main(argv):
    if "--self-test" in argv:
        self_test()
        return 0
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, src = argv[1], argv[2]
    out = argv[argv.index("-o") + 1] if "-o" in argv else None
    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)
    if cmd == "flatten":
        result, default = flatten(data), "out.json"
    elif cmd == "tocsv":
        rows = tocsv_rows(data)
        out = out or "out.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        print(f"Wrote {out}: {len(rows) - 1} rows, {len(rows[0])} columns")
        return 0
    elif cmd == "filter":
        result = filt(data, argv[argv.index("--path") + 1], argv[argv.index("--equals") + 1])
        default = "out.json"
        print(f"Kept {len(result)} of {len(data)} items")
    else:
        print(__doc__)
        return 2
    out = out or default
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
