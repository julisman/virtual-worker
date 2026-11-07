#!/usr/bin/env python3
"""social.py — guardrails and ledgers for the social lead-gen skills.

The rules are code, not prompts: a model can forget an instruction, it cannot
forget an exit code. Every social-* skill runs this before touching a browser
tab and records every action that touched a human through it.

Usage (from the worker's working folder):
  python3 social.py init [--brand <slug>]        scaffold config (human-edited) + brand kit + data/
  python3 social.py check                         validate config, show each account's state
  python3 social.py guard <brand> <platform>      exit 0 = may act (prints JSON incl. budget); 1 = stop; 2 = bad config
  python3 social.py budget <brand> <platform>     public replies still allowed today
  python3 social.py seen <platform> <post_id> <author>        exit 0 NEW / 1 SKIP (dedupe gate)
  python3 social.py seen-add <platform> <post_id> <author> <brand>
  python3 social.py lead-add --json '<object>'    append a lead (status defaults to collected)
  python3 social.py log --json '<object>'         append to the audit log (brand, platform, kind required)
  python3 social.py cooldown <brand> <platform> <hours> "<reason>"
  python3 social.py pace [--check] <brand> <platform>          human rhythm between public actions
  python3 social.py wa-link <brand> <platform> [topic]
  python3 social.py watch-add --json '<object>'
  python3 social.py due [brand]                   which skills are due now (working hours aware)
  python3 social.py status                        leads, replies today, cooldowns, per brand
  python3 social.py --self-test

Layout:
  <config dir>/config.json   human-only control panel: handles, caps, ramp dates, WhatsApp number.
                             Kept OUTSIDE the working folder so the worker's file tools cannot
                             reach it; only a person clears an account to speak (ramp_start).
                             Dir = $VW_SOCIAL_HOME, else $VW_HOME/social, else ~/.virtual-worker/social
  <config dir>/PAUSED        global kill switch (./PAUSED in the working folder also counts)
  ./data/*.jsonl             append-only ledgers: leads, seen, actions, watch
  ./brands/<slug>/           the brand kit (BRAND.md, KEYWORDS.md, REPLIES.md), prose, read by the worker
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

PLATFORMS = ("threads", "tiktok", "instagram")
KINDS_PUBLIC = ("public_reply",)                      # the only kind the daily budget counts
KINDS_PACED = ("public_reply", "dm_reply")

DEFAULT_CONFIG = {
    "_readme": [
        "Human-edited control panel for the social lead-gen skills. The worker reads it through",
        "social.py and cannot write it: it lives outside the working folder on purpose.",
        "ramp_start = null means 0 public replies per day, forever, until you set a date.",
        "Run `python3 social.py check` in the worker's folder after editing.",
    ],
    "timezone": "UTC",
    "defaults": {
        "daily_cap": 20, "author_dedupe_days": 30, "pace_seconds_min": 40, "pace_seconds_max": 120,
        "lead_max_age_days": 7, "saturated_comment_count": 20,
        "ramp": [{"through_day": 7, "replies_per_day": 5}, {"through_day": 14, "replies_per_day": 10}],
    },
    "schedule": {
        "working_hours": {"start": 8, "end": 21, "days": [1, 2, 3, 4, 5, 6, 7]},
        "every_minutes": {"social-dm": 30, "social-prospect": 60, "social-comments": 180, "social-health": 1440},
    },
    "brands": {},
}

BRAND_TEMPLATE = {
    "display_name": "", "sells": "", "wa_number": "", "service_areas": [], "excluded_areas": [],
    "accounts": {p: {"handle": "", "enabled": False, "ramp_start": None, "daily_cap": 20} for p in PLATFORMS},
}

BRAND_MD = """# {name}

Sells: {sells}
WhatsApp: in the control panel (`social.py wa-link {slug} <platform>` builds the link); never paste the number here.

## What we sell

The worker answers from this file and nothing else. If it is not written here, it says
"happy to help in DM" instead of guessing. Fill it in: products / units, rates, what is
included, delivery areas and fees, minimum period, deposit or ID requirements, payment.

## Service area

**We serve:** (list places)
**We do NOT serve:** (list places people ask about that you decline)

## Voice

Write this yourself; it is the whole game. Describe how a real person at this business
types: register, sentence length, capitalisation, how much they hedge. Give two contrasting
examples, because "casual and friendly" alone reads as marketing to a model.

## Common questions

Add the questions that come up repeatedly, with the answer you want given every time.
"""

KEYWORDS_MD = """# {name}: keywords

## Search queries

Few broad queries beat many specific ones (most platforms token-match loosely, so extra
words widen the net). Start with two or three of the plainest phrases a customer would type.

- `<plain two-to-three word query>`
- `<the same thing, other common wording>`

## Negative filters: reject on sight

- Words that mean *selling* rather than *buying* in this market
- Job posts, unrelated products that share a keyword
- Anyone **offering** what you offer: price lists, "DM fast response", slot counts

## Seeker vs seller

| Phrase | Who says it |
|---|---|
| "any recommendation for …?" | seeker |
| "open slot for …" | seller |

## Session log

Record which queries produced leads and which produced nothing, with dates.
"""

REPLIES_MD = """# {name}: reply shapes

These are *shapes*, not text to paste. Every reply names something specific from the
person's post. Log which shape you used; never the same shape twice in a row.

**No links. No phone numbers. The WhatsApp link lives in the bio.** (DMs are different.)

### `rec-area`: they asked for a recommendation in a place
> {{direct answer about that place}}. {{one concrete detail}}. {{soft invite}}

### `rec-timing`: they mentioned dates, arrival or urgency
> {{acknowledge the date}}. {{what you can do by then}}. {{soft invite}}

### `answer-only`: a question you can answer fully from BRAND.md
> {{the answer}}. {{soft invite}}

Replace each with a real example in your own voice.
"""


# --------------------------------------------------------------------------- #
# Paths, config, time
# --------------------------------------------------------------------------- #

def config_dir() -> Path:
    for env in ("VW_SOCIAL_HOME",):
        if os.environ.get(env):
            return Path(os.environ[env]).expanduser()
    home = os.environ.get("VW_HOME")
    return (Path(home).expanduser() if home else Path("~/.virtual-worker").expanduser()) / "social"


def config_path() -> Path:
    return config_dir() / "config.json"


def work_dir() -> Path:
    return Path(os.environ.get("SOCIAL_WORKDIR") or os.getcwd())


def data_dir() -> Path:
    d = work_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        die(f"no config at {p}\n  run: python3 social.py init --brand <slug>   (then a person edits it)")
    try:
        cfg = json.loads(p.read_text())
    except ValueError as exc:
        die(f"{p} is not valid JSON: {exc}")
    for k, v in DEFAULT_CONFIG.items():
        if k == "_readme":
            continue
        cfg.setdefault(k, json.loads(json.dumps(v)))
    for k, v in DEFAULT_CONFIG["defaults"].items():
        cfg["defaults"].setdefault(k, v)
    return cfg


def tz(cfg: dict):
    name = cfg.get("timezone") or "UTC"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            pass
    return timezone.utc


def now(cfg: dict) -> datetime:
    return datetime.now(tz(cfg))


def now_iso(cfg: dict) -> str:
    return now(cfg).isoformat(timespec="seconds")


def today(cfg: dict) -> str:
    return now(cfg).strftime("%Y-%m-%d")


def norm_handle(h: str) -> str:
    """One spelling per person: strip the @, lowercase."""
    return (h or "").strip().lstrip("@").lower()


def account(cfg: dict, brand: str, platform: str) -> dict | None:
    b = (cfg.get("brands") or {}).get(brand)
    if not b:
        return None
    return (b.get("accounts") or {}).get(platform)


# --------------------------------------------------------------------------- #
# Ledgers (append-only JSONL)
# --------------------------------------------------------------------------- #

def read_jsonl(name: str) -> list[dict]:
    p = data_dir() / name
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def append_jsonl(name: str, rec: dict) -> None:
    with open(data_dir() / name, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def parse_json_arg(args) -> dict:
    raw = args.json
    if raw is None or raw == "-":
        raw = sys.stdin.read()
    try:
        d = json.loads(raw or "")
    except ValueError as exc:
        die(f"expected a JSON object: {exc}")
    if not isinstance(d, dict):
        die("expected a JSON object")
    return d


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_init(args) -> None:
    cd = config_dir()
    cd.mkdir(parents=True, exist_ok=True)
    p = config_path()
    created = []
    if not p.exists():
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if args.brand:
            cfg["brands"][args.brand] = json.loads(json.dumps(BRAND_TEMPLATE))
            cfg["brands"][args.brand]["display_name"] = args.brand.replace("-", " ").title()
        p.write_text(json.dumps(cfg, indent=2))
        created.append(str(p))
    elif args.brand:
        cfg = load_config()
        if args.brand not in cfg["brands"]:
            cfg["brands"][args.brand] = json.loads(json.dumps(BRAND_TEMPLATE))
            cfg["brands"][args.brand]["display_name"] = args.brand.replace("-", " ").title()
            p.write_text(json.dumps(cfg, indent=2))
            created.append(f"{p} (+brand {args.brand})")
    data_dir()
    if args.brand:
        bdir = work_dir() / "brands" / args.brand
        bdir.mkdir(parents=True, exist_ok=True)
        name = args.brand.replace("-", " ").title()
        for fn, tpl in (("BRAND.md", BRAND_MD), ("KEYWORDS.md", KEYWORDS_MD), ("REPLIES.md", REPLIES_MD)):
            f = bdir / fn
            if not f.exists():
                f.write_text(tpl.format(name=name, sells="(what the business sells)", slug=args.brand))
                created.append(str(f))
    (work_dir() / "reports").mkdir(exist_ok=True)
    print("created:\n  " + "\n  ".join(created) if created else "nothing to create; already set up")
    print(f"\nNext: a PERSON edits {p} (handles, enabled, wa_number, caps; leave ramp_start null for a read-only trial),")
    print("then fills brands/<slug>/BRAND.md, KEYWORDS.md and REPLIES.md. Then run: python3 social.py check")


def cmd_check(args) -> int:
    cfg = load_config()
    fail = 0
    print(f"config: {config_path()}")
    print(f"timezone: {cfg.get('timezone')} · today there: {today(cfg)}")
    paused = paused_reason()
    if paused is not None:
        print(f"PAUSED: all activity halted ({paused or 'no reason given'})")
    brands = cfg.get("brands") or {}
    if not brands:
        print("FAIL no brands defined"); return 1
    for slug, b in brands.items():
        print(f"\n{slug}")
        wa = str(b.get("wa_number") or "")
        if not wa:
            print("  FAIL wa_number missing"); fail = 1
        elif not wa.isdigit():
            print(f"  FAIL wa_number must be digits only with country code, got {wa!r}"); fail = 1
        else:
            print("  wa_number: ok")
        if not b.get("service_areas"):
            print("  WARN no service_areas: the worker cannot judge whether a lead is in range")
        kit = work_dir() / "brands" / slug
        for fn in ("BRAND.md", "KEYWORDS.md", "REPLIES.md"):
            if not (kit / fn).exists():
                print(f"  WARN missing brands/{slug}/{fn}  (python3 social.py init --brand {slug})")
        for p in PLATFORMS:
            a = (b.get("accounts") or {}).get(p)
            if not a:
                print(f"  {p}: not defined"); continue
            if a.get("enabled"):
                if not a.get("handle"):
                    print(f"  WARN {p}: enabled but handle is empty")
                if not a.get("ramp_start"):
                    print(f"  {p}: enabled, READ-ONLY (ramp_start null → 0 public replies)")
                else:
                    print(f"  {p}: enabled, public replies allowed today: {budget(cfg, slug, p)}")
            else:
                print(f"  {p}: disabled")
    print("\nconfig is usable." if not fail else "\nconfig has problems (see FAIL above).")
    return fail


def paused_reason() -> str | None:
    for p in (config_dir() / "PAUSED", work_dir() / "PAUSED"):
        if p.exists():
            try:
                return p.read_text().strip()
            except OSError:
                return ""
    return None


def budget(cfg: dict, brand: str, platform: str) -> int:
    a = account(cfg, brand, platform)
    if not a:
        return 0
    ramp = a.get("ramp_start")
    cap = int(a.get("daily_cap") or cfg["defaults"].get("daily_cap") or 20)
    if not ramp:
        return 0
    try:
        start = datetime.strptime(str(ramp), "%Y-%m-%d").replace(tzinfo=tz(cfg))
    except ValueError:
        die(f"bad ramp_start {ramp!r} for {brand}/{platform} (expected YYYY-MM-DD)")
    days = (now(cfg) - start).days
    if days < 0:
        allowed = 0
    else:
        allowed = cap
        for stage in cfg["defaults"].get("ramp") or []:
            if days < int(stage.get("through_day", 0)):
                allowed = int(stage.get("replies_per_day", 0))
                break
    used = sum(1 for r in read_jsonl("actions.jsonl")
               if r.get("brand") == brand and r.get("platform") == platform
               and r.get("kind") in KINDS_PUBLIC and str(r.get("ts", "")).startswith(today(cfg)))
    return max(0, allowed - used)


def cmd_budget(args) -> None:
    print(budget(load_config(), args.brand, args.platform))


def cooldown_path(brand: str, platform: str) -> Path:
    d = data_dir() / "cooldown"
    d.mkdir(exist_ok=True)
    return d / f"{brand}-{platform}"


def cmd_guard(args) -> int:
    cfg = load_config()
    brand, platform = args.brand, args.platform
    if brand not in (cfg.get("brands") or {}):
        die(f"brand {brand!r} is not defined in {config_path()}")
    paused = paused_reason()
    if paused is not None:
        print(f"BLOCKED: PAUSED ({paused or 'no reason given'})"); return 1
    cp = cooldown_path(brand, platform)
    if cp.exists():
        lines = cp.read_text().splitlines()
        until = int(lines[0]) if lines and lines[0].isdigit() else 0
        if time.time() < until:
            mins = int((until - time.time()) / 60)
            print(f"BLOCKED: {brand}/{platform} in cooldown for {mins} more minutes: {lines[1] if len(lines) > 1 else ''}")
            return 1
        cp.unlink(missing_ok=True)
    a = account(cfg, brand, platform)
    if not a or not a.get("enabled"):
        print(f"BLOCKED: {brand}/{platform} is not enabled in the control panel"); return 1
    print(json.dumps({"status": "ok", "brand": brand, "platform": platform, "handle": a.get("handle") or "",
                      "replies_remaining_today": budget(cfg, brand, platform), "now": now_iso(cfg)}))
    return 0


def cmd_seen(args) -> int:
    cfg = load_config()
    post_key = f"{args.platform}:{args.post_id}"
    author_key = f"{args.platform}:{norm_handle(args.author)}"
    cutoff = time.time() - int(cfg["defaults"].get("author_dedupe_days") or 30) * 86400
    for r in read_jsonl("seen.jsonl"):
        if r.get("kind") == "post" and r.get("key") == post_key:
            print(f"SKIP: already replied to this post on {r.get('ts')} as {r.get('brand')}"); return 1
    for r in read_jsonl("seen.jsonl"):
        if r.get("kind") == "author" and r.get("key") == author_key and float(r.get("epoch") or 0) >= cutoff:
            print(f"SKIP: replied to {author_key} on {r.get('ts')} as {r.get('brand')}: inside the dedupe window"); return 1
    print("NEW"); return 0


def cmd_seen_add(args) -> None:
    cfg = load_config()
    ts, ep = now_iso(cfg), int(time.time())
    append_jsonl("seen.jsonl", {"kind": "post", "key": f"{args.platform}:{args.post_id}", "brand": args.brand, "ts": ts, "epoch": ep})
    append_jsonl("seen.jsonl", {"kind": "author", "key": f"{args.platform}:{norm_handle(args.author)}", "brand": args.brand, "ts": ts, "epoch": ep})
    print(f"recorded {args.platform}:{args.post_id} and {args.platform}:{norm_handle(args.author)}")


def cmd_lead_add(args) -> None:
    cfg = load_config()
    d = parse_json_arg(args)
    for k in ("platform", "brand", "author", "post_url", "text", "intent"):
        if not d.get(k):
            die(f"lead-add: platform, brand, author, post_url, text, intent are all required (missing {k})")
    if d["intent"] not in ("high", "medium"):
        die("lead-add: intent must be high or medium; reject-tier posts are not leads")
    if d.get("status", "collected") not in ("collected", "research", "skipped"):
        die("lead-add: status must be collected, research or skipped")
    rec = {"status": "collected", **d, "author": norm_handle(d["author"]), "collected_at": now_iso(cfg), "epoch": int(time.time())}
    append_jsonl("leads.jsonl", rec)
    print(f"lead recorded: {rec['author']} ({rec['intent']}, {rec['status']})")


def cmd_log(args) -> None:
    cfg = load_config()
    d = parse_json_arg(args)
    for k in ("brand", "platform", "kind"):
        if not d.get(k):
            die(f"log: brand, platform and kind are required (missing {k})")
    append_jsonl("actions.jsonl", {**d, "ts": now_iso(cfg), "epoch": int(time.time())})
    print(f"logged {d['kind']} for {d['brand']}/{d['platform']}")


def cmd_cooldown(args) -> None:
    cfg = load_config()
    until = int(time.time() + float(args.hours) * 3600)
    cooldown_path(args.brand, args.platform).write_text(f"{until}\n{args.reason}\n")
    append_jsonl("actions.jsonl", {"brand": args.brand, "platform": args.platform, "kind": "blocked",
                                   "cooldown_hours": float(args.hours), "note": args.reason,
                                   "ts": now_iso(cfg), "epoch": int(time.time())})
    print(f"{args.brand}/{args.platform} parked for {args.hours}h: {args.reason}")


def cmd_pace(args) -> None:
    cfg = load_config()
    lo = int(cfg["defaults"].get("pace_seconds_min") or 40)
    hi = int(cfg["defaults"].get("pace_seconds_max") or 120)
    gap = random.randint(lo, max(lo, hi))
    last = max((float(r.get("epoch") or 0) for r in read_jsonl("actions.jsonl")
                if r.get("brand") == args.brand and r.get("platform") == args.platform and r.get("kind") in KINDS_PACED),
               default=0.0)
    owed = 0 if last == 0 else max(0, int(gap - (time.time() - last)))
    if args.check:
        print(owed); return
    wait = min(owed, int(args.max_wait))
    if wait > 0:
        time.sleep(wait)
    remaining = max(0, owed - wait)
    print(f"waited {wait}s (target gap {gap}s); still owed {remaining}s" if remaining else f"waited {wait}s (target gap {gap}s)")


def cmd_wa_link(args) -> None:
    cfg = load_config()
    b = (cfg.get("brands") or {}).get(args.brand)
    if not b:
        die(f"brand {args.brand!r} is not defined")
    wa = re.sub(r"[ +\-]", "", str(b.get("wa_number") or ""))
    if not wa or not wa.isdigit():
        die(f"brands.{args.brand}.wa_number must be digits only with country code (got {wa!r})")
    topic = args.topic or "mau tanya-tanya"
    msg = f"Halo, saya dari {args.platform} — {topic}"
    print(f"https://wa.me/{wa}?text={urllib.parse.quote(msg)}")
    print(f"readable: {wa} (wa.me/{wa})")


def cmd_watch_add(args) -> None:
    cfg = load_config()
    d = parse_json_arg(args)
    for k in ("watch", "platform", "author", "post_url", "post_id", "text"):
        if not d.get(k):
            die(f"watch-add: watch, platform, author, post_url, post_id, text are required (missing {k})")
    key = f"{d['platform']}:{d['post_id']}"
    if any(f"{r.get('platform')}:{r.get('post_id')}" == key for r in read_jsonl("watch.jsonl")):
        print("DUPLICATE: already recorded"); return
    append_jsonl("watch.jsonl", {**d, "recorded_at": now_iso(cfg), "epoch": int(time.time())})
    print(f"watch hit recorded: {d['watch']} / {d['author']}")


def cmd_due(args) -> None:
    cfg = load_config()
    sched = cfg.get("schedule") or {}
    wh = sched.get("working_hours") or {}
    n = now(cfg)
    hour, dow = n.hour, n.isoweekday()
    if not (int(wh.get("start", 0)) <= hour < int(wh.get("end", 24))) or dow not in (wh.get("days") or [1, 2, 3, 4, 5, 6, 7]):
        print(f"# outside working hours ({wh.get('start', 0)}:00-{wh.get('end', 24)}:00, day {dow}); nothing due", file=sys.stderr); return
    if paused_reason() is not None:
        print("# PAUSED; nothing due", file=sys.stderr); return
    every = sched.get("every_minutes") or DEFAULT_CONFIG["schedule"]["every_minutes"]
    actions = read_jsonl("actions.jsonl")
    for slug, b in (cfg.get("brands") or {}).items():
        if args.brand and args.brand != slug:
            continue
        if not any((a or {}).get("enabled") for a in (b.get("accounts") or {}).values()):
            continue
        for skill, mins in every.items():
            last = max((float(r.get("epoch") or 0) for r in actions
                        if r.get("brand") == slug and r.get("kind") == "run" and r.get("skill") == skill), default=0.0)
            age = (time.time() - last) / 60
            if last == 0:
                print(f"{skill} {slug}   # never run")
            elif age >= float(mins):
                print(f"{skill} {slug}   # last run {int(age)}m ago, due every {mins}m")


def cmd_status(args) -> None:
    cfg = load_config()
    leads = read_jsonl("leads.jsonl")
    actions = read_jsonl("actions.jsonl")
    print(f"today ({cfg.get('timezone')}): {today(cfg)}  · paused: {'yes' if paused_reason() is not None else 'no'}")
    for slug, b in (cfg.get("brands") or {}).items():
        print(f"\n{slug}")
        by = {}
        for l in leads:
            if l.get("brand") == slug:
                by[l.get("status", "?")] = by.get(l.get("status", "?"), 0) + 1
        print("  leads: " + (", ".join(f"{k} {v}" for k, v in sorted(by.items())) or "none"))
        for p in PLATFORMS:
            a = (b.get("accounts") or {}).get(p)
            if not a or not a.get("enabled"):
                continue
            sent = sum(1 for r in actions if r.get("brand") == slug and r.get("platform") == p
                       and r.get("kind") in KINDS_PUBLIC and str(r.get("ts", "")).startswith(today(cfg)))
            cp = cooldown_path(slug, p)
            cool = ""
            if cp.exists():
                lines = cp.read_text().splitlines()
                if lines and lines[0].isdigit() and time.time() < int(lines[0]):
                    cool = f" · COOLDOWN {int((int(lines[0]) - time.time()) / 60)}m"
            print(f"  {p}: {sent} public replies today, {budget(cfg, slug, p)} remaining{cool}")


# --------------------------------------------------------------------------- #
# Self-test (hermetic: temp config dir + temp working folder)
# --------------------------------------------------------------------------- #

def self_test() -> int:
    import subprocess
    import tempfile
    me = Path(__file__).resolve()
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "VW_SOCIAL_HOME": td + "/cfg", "SOCIAL_WORKDIR": td + "/work"}
        os.makedirs(td + "/work")

        def run(*a, check=True, stdin=None):
            p = subprocess.run([sys.executable, str(me), *a], capture_output=True, text=True, env=env, cwd=td + "/work", input=stdin)
            if check and p.returncode != 0:
                raise SystemExit(f"self-test: {' '.join(a)} failed ({p.returncode}): {p.stdout}{p.stderr}")
            return p

        run("init", "--brand", "demo")
        cfg_p = Path(td) / "cfg" / "config.json"
        cfg = json.loads(cfg_p.read_text())
        assert "demo" in cfg["brands"] and (Path(td) / "work" / "brands" / "demo" / "BRAND.md").exists()
        # not enabled → guard blocks
        p = run("guard", "demo", "threads", check=False); assert p.returncode == 1 and "not enabled" in p.stdout, p.stdout
        cfg["brands"]["demo"]["wa_number"] = "628123456789"
        cfg["brands"]["demo"]["accounts"]["threads"].update(enabled=True, handle="@demo")
        cfg["timezone"] = "Asia/Makassar"
        cfg_p.write_text(json.dumps(cfg))
        # enabled, ramp null → may act, 0 replies
        p = run("guard", "demo", "threads"); g = json.loads(p.stdout); assert g["replies_remaining_today"] == 0, g
        assert run("budget", "demo", "threads").stdout.strip() == "0"
        # ramp today → week-1 stage (5/day)
        cfg["brands"]["demo"]["accounts"]["threads"]["ramp_start"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cfg_p.write_text(json.dumps(cfg))
        assert run("budget", "demo", "threads").stdout.strip() == "5"
        # dedupe gate
        assert run("seen", "threads", "p1", "@Bob").stdout.strip() == "NEW"
        run("seen-add", "threads", "p1", "Bob", "demo")
        p = run("seen", "threads", "p1", "@bob", check=False); assert p.returncode == 1 and "already replied" in p.stdout
        p = run("seen", "threads", "p2", "@BOB", check=False); assert p.returncode == 1 and "dedupe window" in p.stdout
        # lead + log + budget consumption
        run("lead-add", "--json", json.dumps({"platform": "threads", "brand": "demo", "author": "@Ann", "post_url": "https://x/1",
                                              "text": "need a scooter near the beach", "intent": "high", "product": "motor"}))
        p = run("lead-add", "--json", json.dumps({"platform": "threads", "brand": "demo", "author": "a", "post_url": "u", "text": "t", "intent": "reject"}), check=False)
        assert p.returncode == 2
        run("log", "--json", json.dumps({"brand": "demo", "platform": "threads", "kind": "public_reply", "author": "@ann", "text": "hi"}))
        assert run("budget", "demo", "threads").stdout.strip() == "4"
        run("log", "--json", json.dumps({"brand": "demo", "platform": "threads", "kind": "comment_reply", "text": "x"}))
        assert run("budget", "demo", "threads").stdout.strip() == "4"       # comments are uncapped
        assert run("pace", "--check", "demo", "threads").stdout.strip().isdigit()
        # cooldown blocks guard; PAUSED beats everything
        run("cooldown", "demo", "threads", "1", "action block modal")
        p = run("guard", "demo", "threads", check=False); assert p.returncode == 1 and "cooldown" in p.stdout
        (Path(td) / "cfg" / "PAUSED").write_text("maintenance")
        p = run("guard", "demo", "tiktok", check=False); assert p.returncode == 1 and "PAUSED" in p.stdout
        (Path(td) / "cfg" / "PAUSED").unlink()
        # wa link + due + status + check
        p = run("wa-link", "demo", "threads", "sewa motor"); assert "wa.me/628123456789?text=" in p.stdout
        run("watch-add", "--json", json.dumps({"watch": "w", "platform": "threads", "author": "@z", "post_url": "u", "post_id": "9", "text": "t"}))
        assert "DUPLICATE" in run("watch-add", "--json", json.dumps({"watch": "w", "platform": "threads", "author": "@z", "post_url": "u", "post_id": "9", "text": "t"})).stdout
        run("due")
        run("status")
        p = run("check", check=False); assert "config is usable" in p.stdout, p.stdout
        # leads ledger shape
        leads = [json.loads(l) for l in (Path(td) / "work" / "data" / "leads.jsonl").read_text().splitlines()]
        assert leads[0]["author"] == "ann" and leads[0]["status"] == "collected"
    print("social.py self-test OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="social.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("init"); s.add_argument("--brand")
    sub.add_parser("check")
    for name in ("guard", "budget"):
        s = sub.add_parser(name); s.add_argument("brand"); s.add_argument("platform")
    s = sub.add_parser("seen"); s.add_argument("platform"); s.add_argument("post_id"); s.add_argument("author")
    s = sub.add_parser("seen-add"); s.add_argument("platform"); s.add_argument("post_id"); s.add_argument("author"); s.add_argument("brand")
    for name in ("lead-add", "log", "watch-add"):
        s = sub.add_parser(name); s.add_argument("--json", help="JSON object (or '-' for stdin)")
    s = sub.add_parser("cooldown"); s.add_argument("brand"); s.add_argument("platform"); s.add_argument("hours"); s.add_argument("reason", nargs="?", default="no reason given")
    s = sub.add_parser("pace"); s.add_argument("--check", action="store_true"); s.add_argument("--max-wait", default=50); s.add_argument("brand"); s.add_argument("platform")
    s = sub.add_parser("wa-link"); s.add_argument("brand"); s.add_argument("platform"); s.add_argument("topic", nargs="?")
    s = sub.add_parser("due"); s.add_argument("brand", nargs="?")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.cmd:
        ap.print_help(); return 2
    fn = {"init": cmd_init, "check": cmd_check, "guard": cmd_guard, "budget": cmd_budget, "seen": cmd_seen,
          "seen-add": cmd_seen_add, "lead-add": cmd_lead_add, "log": cmd_log, "cooldown": cmd_cooldown,
          "pace": cmd_pace, "wa-link": cmd_wa_link, "watch-add": cmd_watch_add, "due": cmd_due, "status": cmd_status}[args.cmd]
    rc = fn(args)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
