"""PRD-17 FR-10 — persona cache-prefix volatility lint (detector only)."""

from agent.prefix_lint import find_volatile


def test_clean_persona_has_no_findings():
    persona = ("You are an SDET for a Warehouse Management System. Write "
               "deterministic, isolated tests. Daily standup is at our usual slot; "
               "escalate blockers early.")
    assert find_volatile(persona) == []


def test_literal_datetime_is_flagged():
    persona = "You are helpful. Current date & time: 2026-07-18 11:51 (Saturday)."
    kinds = {f["kind"] for f in find_volatile(persona)}
    assert "date" in kinds and "time-phrase" in kinds


def test_bare_time_without_date_context_is_not_flagged():
    # "standup at 09:30" is stable content, not volatility
    assert find_volatile("Join standup at 09:30 every day.") == []


def test_uuid_and_hash_are_flagged():
    persona = ("Track run 3f2504e0-4f89-11d3-9a0c-0305e82c3301 and artifact "
               "a3f5c9d001b2e8f4a3f5c9d001b2e8f4.")
    kinds = {f["kind"] for f in find_volatile(persona)}
    assert kinds == {"uuid", "hash"}


def test_plain_words_and_numbers_not_hash_flagged():
    assert find_volatile("Use billing account 1234567890123456 carefully.") == []
    assert find_volatile("The abcdefabcdefabcdef style name is fine.") == []


def test_findings_are_bounded_and_deduped():
    persona = "date 2026-01-01 " * 50
    fs = find_volatile(persona)
    assert len(fs) == 1                       # duplicates collapse


def test_agents_route_attaches_lint(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    from routes.agents import _with_lint
    a = _with_lint({"id": "x", "persona": "Today is 2026-07-18, be helpful."})
    assert a["personaCacheWarnings"] and a["personaCacheWarnings"][0]["kind"] == "date"
    b = _with_lint({"id": "y", "persona": "Be helpful."})
    assert b["personaCacheWarnings"] == []
