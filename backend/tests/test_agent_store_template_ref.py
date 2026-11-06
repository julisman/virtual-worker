"""Agent template provenance and managed-update CAS behavior."""

import config


def test_template_ref_round_trip_and_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    from storage import agent_store

    template_ref = {"sourceId": "s", "templateId": "t", "version": "1.0.0"}
    created = agent_store.create_agent({"name": "Managed", "templateRef": template_ref})
    plain = agent_store.create_agent({"name": "Plain"})

    assert agent_store.get_agent(created["id"])["templateRef"] == template_ref
    assert agent_store.get_agent(plain["id"])["templateRef"] is None


def test_apply_managed_update_compare_and_swap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    from storage import agent_store

    created = agent_store.create_agent({"name": "Managed", "persona": "old"})
    expected_updated_at = created["updatedAt"]
    managed = {
        "persona": "new",
        "role": "r",
        "description": "d",
        "temperature": 0.3,
        "topP": None,
        "topK": None,
        "tools": ["browser_search"],
        "skills": ["inventory"],
        "guardrails": {"dailyCap": 10},
    }
    updated = agent_store.apply_managed_update(
        created["id"], managed, {"sourceId": "s"}, expected_updated_at
    )

    assert updated is not None
    assert updated["persona"] == "new"
    assert updated["tools"] == ["browser_search"]
    assert updated["skills"] == ["inventory"]
    assert updated["templateRef"] == {"sourceId": "s"}
    assert updated["updatedAt"] != expected_updated_at
    assert updated["guardrails"]["dailyCap"] == 10
    assert "shell" in updated["guardrails"]
    assert "verify" in updated["guardrails"]

    assert agent_store.apply_managed_update(
        created["id"], {**managed, "persona": "stale"}, {"sourceId": "stale"}, expected_updated_at
    ) is None
    assert agent_store.get_agent(created["id"])["persona"] == "new"
