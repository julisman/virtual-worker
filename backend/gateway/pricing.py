# Per-model token pricing — the single source of truth for LLM call cost.

"""Per-model token pricing (USD per 1M tokens).

Local inference is free; unknown models cost 0. A price-table value is one of:
  * a flat ``(input, output)`` tuple;
  * a long-context tiered dict ``{"short": (in, out), "long": (in, out),
    "threshold": N}`` where an input above ``threshold`` reprices BOTH input
    and output at the ``long`` rates (OpenAI's long-context rule);
  * a time-of-day dict ``{"peak": (in, out), "offpeak": (in, out)}`` selected
    by the request time (DeepSeek). Peak = Mon–Fri (UTC) 01:00–04:00 &
    06:00–10:00 UTC; everything else (incl. all weekend hours) is off-peak
    (= half price).

The two dict variants are told apart by key presence (``"threshold"`` vs
``"peak"``). Matching is longest-substring-wins in :func:`estimate_cost`.
Rates verified 2026-08-24 from official provider pages.
"""

from datetime import datetime, timezone

# model substring : (input, output)  OR  tiered dict
PRICE_PER_M = {
    # ── OpenAI (tiered: >272k input tokens reprices the whole request) ──
    "gpt-5.6-sol": {"short": (4.0, 20.0), "long": (8.0, 30.0), "threshold": 272_000},
    "gpt-5.6-terra": {"short": (2.0, 12.0), "long": (4.0, 18.0), "threshold": 272_000},
    "gpt-5.6-luna": {"short": (0.2, 1.2), "long": (0.4, 1.8), "threshold": 272_000},
    "gpt-5.6": {"short": (4.0, 20.0), "long": (8.0, 30.0), "threshold": 272_000},  # bare alias routes to Sol
    "gpt-5.5": {"short": (5.0, 30.0), "long": (10.0, 45.0), "threshold": 272_000},
    "gpt-5.4": {"short": (2.5, 15.0), "long": (5.0, 22.5), "threshold": 272_000},
    # ── OpenAI (flat) ──
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-4o": (2.5, 10.0),        # legacy, keep
    "gpt-4o-mini": (0.15, 0.6),   # legacy, keep
    # ── Anthropic (flat) ──
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-1": (15.0, 75.0),   # retired gen
    "claude-opus-4-2": (15.0, 75.0),   # retired Opus 4 dated id claude-opus-4-20250514
    "claude-opus": (5.0, 25.0),        # Opus 5 / 4.8 / 4.7 / 4.6 / 4.5
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet": (3.0, 15.0),      # Sonnet 4.6 / 4.5 / 4
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku": (0.8, 4.0),        # Haiku 3.5
    # ── Z.AI / Zhipu GLM (flat) ──
    "glm-5.3": (1.4, 4.4),
    "glm-5.2": (1.4, 4.4),
    "glm-5.1": (1.4, 4.4),
    "glm-5-turbo": (1.2, 4.0),
    "glm-5": (1.0, 3.2),
    "glm-4.7-flashx": (0.07, 0.4),
    "glm-4.7-flash": (0.0, 0.0),
    "glm-4.7": (0.6, 2.2),
    "glm-4.6v-flashx": (0.04, 0.4),
    "glm-4.6v-flash": (0.0, 0.0),
    "glm-4.6v": (0.3, 0.9),
    "glm-4.6": (0.6, 2.2),
    "glm-4.5-airx": (1.1, 4.5),
    "glm-4.5-air": (0.2, 1.1),
    "glm-4.5-x": (2.2, 8.9),
    "glm-4.5-flash": (0.0, 0.0),
    "glm-4.5": (0.6, 2.2),
    # ── Moonshot Kimi (flat) ──
    "kimi-k3": (3.0, 15.0),
    "kimi-k2.7-code-highspeed": (1.9, 8.0),
    "kimi-k2.7-code": (0.95, 4.0),
    "kimi-k2.6": (0.95, 4.0),
    # ── DeepSeek (time-of-day: peak = Mon–Fri 01:00–04:00 & 06:00–10:00 UTC,
    #    off-peak = half price; cache-MISS input rates) ──
    "deepseek-v4-pro": {"peak": (1.32, 3.96), "offpeak": (0.66, 1.98)},
    "deepseek-v4-flash-vision-exp": {"peak": (0.44, 1.32), "offpeak": (0.22, 0.66)},
    "deepseek-v4-flash": {"peak": (0.44, 1.32), "offpeak": (0.22, 0.66)},
    "deepseek-reasoner": {"peak": (0.44, 1.32), "offpeak": (0.22, 0.66)},   # retired alias
    "deepseek-chat": {"peak": (0.44, 1.32), "offpeak": (0.22, 0.66)},       # retired alias
}


def deepseek_is_peak(now: datetime) -> bool:
    # Peak = Mon–Fri (UTC weekday) and UTC hour in 01:00–03:59 or 06:00–09:59.
    return now.weekday() < 5 and (now.hour in (1, 2, 3) or now.hour in (6, 7, 8, 9))


def estimate_cost(model: str | None, inp: int, out: int, *, now: datetime | None = None) -> float:
    if not model:
        return 0.0
    m = model.lower()
    # Longest substring wins, so a more specific id (gpt-4o-mini) beats a
    # shorter prefix of it (gpt-4o).
    key = max((k for k in PRICE_PER_M if k in m), key=len, default=None)
    if key is None:
        return 0.0
    price = PRICE_PER_M[key]
    if isinstance(price, dict):
        if "threshold" in price:
            # Long-context: an input above the threshold reprices BOTH sides.
            pi, po = price["long"] if inp > price["threshold"] else price["short"]
        else:
            # Time-of-day (DeepSeek): peak vs off-peak by UTC clock.
            now = now or datetime.now(timezone.utc)
            pi, po = price["peak"] if deepseek_is_peak(now) else price["offpeak"]
    else:
        pi, po = price
    return round((inp / 1_000_000) * pi + (out / 1_000_000) * po, 6)
