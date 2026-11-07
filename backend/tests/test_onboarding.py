"""First-run status + recommendations."""

from fastapi.testclient import TestClient

import config
from routes import onboarding


def test_recommendations_follow_memory_and_engine():
    mlx_big = onboarding.recommendations(32, True, True)
    assert mlx_big[0]["fmt"] == "mlx" and "9B" in mlx_big[0]["label"] and mlx_big[-1]["q"].startswith("Qwen2.5-3B")
    mlx_small = onboarding.recommendations(8, True, True)
    assert [r["q"] for r in mlx_small] == ["Qwen2.5-3B-Instruct-4bit"]
    gguf = onboarding.recommendations(16, False, False)
    assert gguf[0]["fmt"] == "gguf" and gguf[0]["kind"] == "file" and gguf[0]["quant"] == "Q4_K_M"
    assert onboarding.recommendations(None, False, False)[0]["q"] == "Qwen3-8B-GGUF"   # unknown memory → 16 GB assumption
    no_mlx_mac = onboarding.recommendations(32, True, False)
    assert all(r["fmt"] == "gguf" for r in no_mlx_mac)


def test_status_needed_until_dismissed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(onboarding, "_local_models", lambda: [])
    monkeypatch.setattr(onboarding, "_providers_configured", lambda: 0)

    async def no_runtime():
        return {"healthy": False, "loading": False}
    from runtime import llama_manager
    monkeypatch.setattr(llama_manager, "status", no_runtime)
    from app import app
    c = TestClient(app)
    s = c.get("/api/onboarding/status").json()
    assert s["needed"] is True and s["hasModel"] is False and s["recommendations"]
    assert "memGb" in s["platform"] and isinstance(s["agents"], int)
    assert c.post("/api/onboarding/dismiss").json()["ok"]
    s = c.get("/api/onboarding/status").json()
    assert s["needed"] is False and s["dismissed"] is True and s["completedAt"]
    assert c.post("/api/onboarding/reset").json()["ok"]
    assert c.get("/api/onboarding/status").json()["needed"] is True
    # a configured provider is enough to answer a chat → not needed
    monkeypatch.setattr(onboarding, "_providers_configured", lambda: 1)
    s = c.get("/api/onboarding/status").json()
    assert s["needed"] is False and s["hasModel"] is True
