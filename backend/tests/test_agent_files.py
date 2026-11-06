"""Agent working-dir file serving — GET /api/agents/{id}/files.

Hermetic: no real model, no network. Mounts the route directly (mirrors
test_channel.py's ``_client()``), isolates agent_store with a tmp APP_HOME.
"""

import pytest

import config
from storage import agent_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(agent_store, "_seeded", False)
    return tmp_path


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.agent_files import router
    app = FastAPI()
    app.include_router(router, prefix="/api/agents")
    return TestClient(app)


def test_serves_file_under_working_dir(home, tmp_path):
    wd = tmp_path / "work"
    (wd / "screenshots").mkdir(parents=True)
    (wd / "screenshots" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
    a = agent_store.create_agent({"name": "Bot", "persona": "x", "workingDir": str(wd)})
    c = _client()
    r = c.get(f"/api/agents/{a['id']}/files", params={"path": "screenshots/shot.png"})
    assert r.status_code == 200
    assert r.content == b"\x89PNG\r\n\x1a\nfakedata"
    assert r.headers["content-type"] == "image/png"


def test_404_unknown_agent(home):
    r = _client().get("/api/agents/ghost/files", params={"path": "x.png"})
    assert r.status_code == 404


def test_400_no_working_dir(home):
    a = agent_store.create_agent({"name": "Bot", "persona": "x"})
    r = _client().get(f"/api/agents/{a['id']}/files", params={"path": "x.png"})
    assert r.status_code == 400


def test_404_missing_file(home, tmp_path):
    wd = tmp_path / "work"
    wd.mkdir()
    a = agent_store.create_agent({"name": "Bot", "persona": "x", "workingDir": str(wd)})
    r = _client().get(f"/api/agents/{a['id']}/files", params={"path": "nope.png"})
    assert r.status_code == 404


def test_path_escape_rejected(home, tmp_path):
    wd = tmp_path / "work"
    wd.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    a = agent_store.create_agent({"name": "Bot", "persona": "x", "workingDir": str(wd)})
    r = _client().get(f"/api/agents/{a['id']}/files", params={"path": "../secret.txt"})
    assert r.status_code == 400
