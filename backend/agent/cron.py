"""A tiny, dependency-free cron evaluator (5-field, local time).

Fields:  minute(0-59) hour(0-23) day-of-month(1-31) month(1-12) day-of-week(0-6)
Day-of-week is 0=Sunday … 6=Saturday (7 also accepted as Sunday), matching the
crontab convention agents are most likely to produce.

Each field supports ``*``, lists ``1,2,3``, ranges ``1-5``, and steps ``*/15``
or ``0-30/10``. A handful of macros are also accepted (``@hourly``, ``@daily``,
``@weekly``, ``@monthly``). Resolution is one minute. We deliberately keep this
small instead of pulling in croniter — schedules here are simple ("2pm daily").
"""

from __future__ import annotations

from datetime import datetime, timedelta

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

# day-of-week accepts 0-7 (both 0 and 7 = Sunday); 7 is normalized to 0 below.
_FIELD_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _expand(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the concrete set of values it matches."""
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("step must be positive")
        else:
            base = part
        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        if start < lo or end > hi or start > end:
            raise ValueError(f"value out of range for field ({lo}-{hi}): {part}")
        out.update(range(start, end + 1, step))
    if not out:
        raise ValueError("empty field")
    return out


def _fields(expr: str) -> list[set[int]]:
    expr = expr.strip()
    expr = _MACROS.get(expr.lower(), expr)
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron must have 5 fields (min hour dom month dow) or a @macro")
    fields = [_expand(p, lo, hi) for p, (lo, hi) in zip(parts, _FIELD_BOUNDS)]
    # normalize day-of-week 7 → 0 (both are Sunday) so the set is 0-6
    if 7 in fields[4]:
        fields[4] = (fields[4] - {7}) | {0}
    return fields


def validate(expr: str) -> bool:
    """True if ``expr`` is a parseable cron expression, False otherwise."""
    try:
        _fields(expr)
        return True
    except (ValueError, AttributeError):
        return False


def matches(expr: str, dt: datetime) -> bool:
    """Does ``dt`` (minute resolution) satisfy the cron expression?"""
    minute, hour, dom, month, dow = _fields(expr)
    # crontab dow: 0/7 = Sunday. Python weekday(): Mon=0..Sun=6.
    py_dow = (dt.weekday() + 1) % 7  # → Sun=0 .. Sat=6
    dow_ok = py_dow in dow or (7 in dow and py_dow == 0)
    # crontab quirk: when BOTH dom and dow are restricted, either may match.
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(0, 7))
    if dom_restricted and dow_restricted:
        day_ok = dt.day in dom or dow_ok
    else:
        day_ok = (dt.day in dom) and dow_ok
    return dt.minute in minute and dt.hour in hour and dt.month in month and day_ok


def next_run(expr: str, after: datetime, horizon_days: int = 366) -> datetime | None:
    """The next minute strictly after ``after`` that matches, or None within the
    horizon. Brute-force minute scan — fine at this resolution for simple crons."""
    cur = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = cur + timedelta(days=horizon_days)
    while cur <= end:
        if matches(expr, cur):
            return cur
        cur += timedelta(minutes=1)
    return None


def describe(expr: str) -> str:
    """Best-effort human phrasing for the common shapes; falls back to the expr."""
    raw = expr.strip()
    norm = _MACROS.get(raw.lower(), raw)
    try:
        parts = norm.split()
        if len(parts) != 5:
            return raw
        mn, hr, dom, mon, dow = parts
        days = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}
        if mn.isdigit() and hr.isdigit() and dom == "*" and mon == "*":
            t = f"{int(hr):02d}:{int(mn):02d}"
            if dow == "*":
                return f"Daily at {t}"
            if dow in days:
                return f"Every {days[dow]} at {t}"
            if "," in dow and all(d in days for d in dow.split(",")):
                return f"{', '.join(days[d] for d in dow.split(','))} at {t}"
        if mn.startswith("*/") and hr == "*" and dom == "*" and mon == "*" and dow == "*":
            return f"Every {mn[2:]} minutes"
        if mn.isdigit() and hr == "*" and dom == "*" and mon == "*" and dow == "*":
            return f"Hourly at :{int(mn):02d}"
    except (ValueError, KeyError):
        pass
    return raw
