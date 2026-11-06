"""Per-model token pricing — longest-substring matching + tiered repricing."""

from datetime import datetime, timezone

from gateway.pricing import estimate_cost as _cost


# ─── longest-substring matching ──────────────────────────────────────────────

def test_gpt_4o_mini_uses_mini_rates_not_gpt_4o():
    # gpt-4o is a substring of gpt-4o-mini; longest match must win.
    assert _cost("gpt-4o-mini", 1_000_000, 1_000_000) == round(0.15 + 0.6, 6)
    # sanity: the plain gpt-4o still bills at gpt-4o rates
    assert _cost("gpt-4o", 1_000_000, 1_000_000) == round(2.5 + 10.0, 6)


def test_more_specific_claude_ids_win():
    # claude-sonnet-5 beats the generic claude-sonnet entry
    assert _cost("claude-sonnet-5", 1_000_000, 0) == round(2.0, 6)
    assert _cost("claude-sonnet-4-5", 1_000_000, 0) == round(3.0, 6)
    # claude-opus-4-1 beats claude-opus
    assert _cost("claude-opus-4-1", 1_000_000, 0) == round(15.0, 6)
    assert _cost("claude-opus-4-8", 1_000_000, 0) == round(5.0, 6)


# ─── tiered (OpenAI long-context) repricing ──────────────────────────────────

def test_openai_tier_boundary():
    # At or below the threshold -> short rates.
    assert _cost("gpt-5.6-sol", 272_000, 1_000_000) == round(
        (272_000 / 1_000_000) * 4.0 + 20.0, 6
    )
    # Above the threshold -> BOTH input and output priced at long rates.
    inp = 272_001
    assert _cost("gpt-5.6-sol", inp, 1_000_000) == round(
        (inp / 1_000_000) * 8.0 + 30.0, 6
    )


def test_tier_input_only_rebill_above_threshold():
    # Mirrors debug_db's cache-miss rebill (output_tokens=0). Above the
    # threshold the input rebill uses the long input rate — correct and safe.
    inp = 300_000
    assert _cost("gpt-5.6-sol", inp, 0) == round((inp / 1_000_000) * 8.0, 6)
    assert _cost("gpt-5.6-sol", 100_000, 0) == round((100_000 / 1_000_000) * 4.0, 6)


# ─── flat tuple + unknown ────────────────────────────────────────────────────

def test_flat_tuple_exact_arithmetic():
    pi, po = 1.4, 4.4  # glm-5.3
    inp, out = 123_456, 7_890
    expected = round((inp / 1e6) * pi + (out / 1e6) * po, 6)
    assert _cost("glm-5.3", inp, out) == expected


def test_unknown_and_local_models_are_free():
    assert _cost("LocalFT-Q35-v3", 1_000_000, 1_000_000) == 0.0
    assert _cost("llama-3.1-8b-instruct", 500, 500) == 0.0
    assert _cost(None, 100, 100) == 0.0
    assert _cost("", 100, 100) == 0.0


# ─── DeepSeek time-of-day pricing ────────────────────────────────────────────

def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_deepseek_peak_windows():
    # 2026-08-24 is a Monday. Peak: 01:00–03:59 & 06:00–09:59 UTC.
    peak_early = _utc(2026, 8, 24, 2, 0)    # weekday 02:00 -> peak
    peak_late = _utc(2026, 8, 24, 7, 30)    # weekday 07:30 -> peak
    for now in (peak_early, peak_late):
        assert _cost("deepseek-v4-flash", 1_000_000, 1_000_000, now=now) == round(
            0.44 + 1.32, 6
        )


def test_deepseek_offpeak_windows():
    # 2026-08-24 Monday off-peak hours; 2026-08-29 is a Saturday (always off-peak).
    off_gap = _utc(2026, 8, 24, 5, 0)      # weekday 05:00 -> between windows
    off_day = _utc(2026, 8, 24, 12, 0)     # weekday 12:00 -> after windows
    off_weekend = _utc(2026, 8, 29, 2, 0)  # Saturday 02:00 -> weekend always off-peak
    for now in (off_gap, off_day, off_weekend):
        assert _cost("deepseek-v4-flash", 1_000_000, 1_000_000, now=now) == round(
            0.22 + 0.66, 6
        )


def test_deepseek_vision_exp_longest_match():
    # deepseek-v4-flash-vision-exp must match its own row, not deepseek-v4-flash.
    peak = _utc(2026, 8, 24, 2, 0)
    assert _cost("deepseek-v4-flash-vision-exp", 1_000_000, 0, now=peak) == round(0.44, 6)


def test_deepseek_cost_without_now_is_valid():
    cost = _cost("deepseek-v4-flash", 1_000_000, 1_000_000)
    assert isinstance(cost, float) and cost >= 0.0
