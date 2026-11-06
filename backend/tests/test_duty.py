"""Duty routes — the intern on/off-duty switch (the kill switch). The worker
manager is mocked so no real background loop is launched."""

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from routes import duty as duty_mod
from storage import agent_store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(agent_store, "_seeded", False)

    calls = {"start": [], "stop": [], "running": {}}

    class FakeManager:
        async def start(self, agent_id):
            calls["start"].append(agent_id)
            calls["running"][agent_id] = True

        async def stop(self, agent_id):
            calls["stop"].append(agent_id)
            calls["running"][agent_id] = False

        def is_running(self, agent_id):
            return calls["running"].get(agent_id, False)

        def current(self, agent_id):
            return None

    monkeypatch.setattr(duty_mod, "manager", FakeManager())
    c = TestClient(app)
    c._calls = calls
    return c


def test_duty_unknown_agent_404(client):
    assert client.get("/api/agents/nope/duty").status_code == 404
    assert client.post("/api/agents/nope/duty", json={"status": "on"}).status_code == 404


def test_duty_requires_a_role(client):
    # Only interns (agents with a job role) go on duty.
    a = agent_store.create_agent({"name": "Plain", "persona": "p"})  # no role
    r = client.post(f"/api/agents/{a['id']}/duty", json={"status": "on"})
    assert r.status_code == 400
    assert client._calls["start"] == []


def test_duty_on_then_off_toggles_worker(client):
    a = agent_store.create_agent({"name": "Hasan", "role": "SWE Intern", "persona": "p"})
    on = client.post(f"/api/agents/{a['id']}/duty", json={"status": "on"})
    assert on.status_code == 200
    assert client._calls["start"] == [a["id"]]
    assert on.json()["running"] is True

    off = client.post(f"/api/agents/{a['id']}/duty", json={"status": "off"})
    assert off.status_code == 200
    assert client._calls["stop"] == [a["id"]]        # kill switch invoked
    assert off.json()["running"] is False


def test_duty_invalid_status_400(client):
    a = agent_store.create_agent({"name": "Hasan", "role": "SWE Intern", "persona": "p"})
    r = client.post(f"/api/agents/{a['id']}/duty", json={"status": "sideways"})
    assert r.status_code == 400
