"""resolve_model() precedence."""

import asyncio

import config
import gateway.models as models


def test_model_map_wins(monkeypatch):
    monkeypatch.setattr(config, "MODEL_MAP", {"claude-3-5-sonnet": "local-qwen"})
    monkeypatch.setattr(config, "DEFAULT_MODEL", "ignored")
    assert asyncio.run(models.resolve_model("claude-3-5-sonnet")) == "local-qwen"


def test_default_model_fallback(monkeypatch):
    monkeypatch.setattr(config, "MODEL_MAP", {})
    monkeypatch.setattr(config, "DEFAULT_MODEL", "local-qwen")
    assert asyncio.run(models.resolve_model("gpt-4o")) == "local-qwen"


def test_first_upstream_model(monkeypatch):
    monkeypatch.setattr(config, "MODEL_MAP", {})
    monkeypatch.setattr(config, "DEFAULT_MODEL", None)

    async def fake_first():
        return "served-model"
    monkeypatch.setattr(models, "_first_upstream_model", fake_first)
    assert asyncio.run(models.resolve_model("gemini-2.0-flash")) == "served-model"


def test_passthrough_when_nothing(monkeypatch):
    monkeypatch.setattr(config, "MODEL_MAP", {})
    monkeypatch.setattr(config, "DEFAULT_MODEL", None)

    async def none_first():
        return None
    monkeypatch.setattr(models, "_first_upstream_model", none_first)
    assert asyncio.run(models.resolve_model("whatever")) == "whatever"
