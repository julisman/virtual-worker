"""Hermetic HTTP tests for the agent inbox API."""

import pytest
from fastapi.testclient import TestClient

import config
from storage import inbox_store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    import app

    return TestClient(app.app)


def _create(agent_id: str, title: str) -> dict:
    return inbox_store.create(agent_id, {"payload": {"title": title}})


def test_list_default_returns_pending_and_failed_only(client):
    pending = _create("agent-a", "pending")
    failed = _create("agent-a", "failed")
    inbox_store.mark("agent-a", failed["id"], "failed", error="send failed")
    processed = _create("agent-a", "processed")
    inbox_store.mark("agent-a", processed["id"], "processed")
    dismissed = _create("agent-a", "dismissed")
    inbox_store.mark("agent-a", dismissed["id"], "dismissed")

    response = client.get("/api/agents/agent-a/inbox")

    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {pending["id"], failed["id"]}


@pytest.mark.parametrize("status", ["failed", "processed"])
def test_list_explicit_status_filters(client, status):
    matching = _create("agent-a", status)
    inbox_store.mark("agent-a", matching["id"], status)
    other = _create("agent-a", "other")
    inbox_store.mark("agent-a", other["id"], "pending")

    response = client.get("/api/agents/agent-a/inbox", params={"status": status})

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [matching["id"]]
    assert items[0]["status"] == status


def test_list_all_excludes_dismissed(client):
    visible = _create("agent-a", "visible")
    dismissed = _create("agent-a", "dismissed")
    inbox_store.mark("agent-a", dismissed["id"], "dismissed")

    response = client.get("/api/agents/agent-a/inbox", params={"status": "all"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [visible["id"]]


def test_retry_failed_item_resets_to_pending(client):
    item = _create("agent-a", "retry")
    inbox_store.bump_attempt("agent-a", item["id"])
    inbox_store.mark("agent-a", item["id"], "failed", error="send failed")

    response = client.post(f"/api/agents/agent-a/inbox/{item['id']}/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0


def test_dismiss_sets_status_dismissed(client):
    item = _create("agent-a", "dismiss")

    response = client.delete(f"/api/agents/agent-a/inbox/{item['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "dismissed"


@pytest.mark.parametrize("method", ["post", "delete"])
def test_unknown_item_is_404(client, method):
    path = "/api/agents/agent-a/inbox/missing"
    if method == "post":
        path += "/retry"

    response = getattr(client, method)(path)

    assert response.status_code == 404


def test_mentor_task_close_flows_back(client):
    from storage import agent_store, task_store

    caller = agent_store.create_agent({"name": "Caller", "role": "lead"})
    worker = agent_store.create_agent({"name": "Worker", "role": "intern"})
    task = task_store.create(worker["id"], {
        "title": "Delegated", "status": "in_progress", "resultSummary": "\nFinished it",
        "metadata": {"assignedBy": caller["id"], "assignedByName": "Caller",
                     "replyTo": {"kind": "in_app", "sessionId": "chat:s1"}},
    }, origin="agent")

    response = client.put(f"/api/agents/{worker['id']}/tasks/{task['id']}",
                          json={"status": "done"})
    assert response.status_code == 200

    items = inbox_store.list_items(caller["id"])
    assert len(items) == 1
    assert items[0]["taskId"] == task["id"]
    assert items[0]["payload"]["status"] == "done"
