"""Outbound delivery (PRD-01) — connectors, audit log, explicit-target safety.

Stores are isolated per test by pointing config.APP_HOME at a tmp dir (same
pattern as test_scheduler), so nothing touches the real ~/.virtual-worker.
"""

import asyncio

import pytest

import config


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    from agent import delivery
    from storage import channel_store, chat_store, schedule_store
    return delivery, channel_store, chat_store, schedule_store


# ─── central deliver() ───────────────────────────────────────────────────────

def test_in_app_delivery_writes_chat_message(stores):
    delivery, channel_store, chat_store, _ = stores
    rec = asyncio.run(delivery.deliver("in_app", "daily-summary", "hello there",
                                       subject="Standup", agent_id="a1", source="tool"))
    assert rec["status"] == "sent"
    assert rec["channel"] == "in_app" and rec["target"] == "daily-summary"
    # The session was created and the message appended.
    s = chat_store.get_session("daily-summary")
    assert s is not None and s["messages"][-1]["role"] == "assistant"
    assert "hello there" in s["messages"][-1]["content"]
    assert s["messages"][-1]["meta"]["delivered"] is True
    # And the attempt was audited.
    rows = channel_store.list_deliveries(agent_id="a1")
    assert len(rows) == 1 and rows[0]["status"] == "sent"


def test_explicit_target_required_for_in_app(stores):
    """FR-3 / anti-#648: a recipient is never inferred — empty target fails."""
    delivery, channel_store, _, _ = stores
    rec = asyncio.run(delivery.deliver("in_app", "", "body", agent_id="a1"))
    assert rec["status"] == "failed"
    assert "explicit 'target'" in rec["error"]
    assert channel_store.list_deliveries()[0]["status"] == "failed"


def test_unknown_channel_fails_loudly(stores):
    delivery, _, _, _ = stores
    rec = asyncio.run(delivery.deliver("carrier_pigeon", "x", "hi"))
    assert rec["status"] == "failed" and "unknown channel" in rec["error"]


def test_oversize_payload_dropped_with_record(stores):
    delivery, channel_store, _, _ = stores
    big = "x" * (delivery.MAX_PAYLOAD_BYTES + 1)
    rec = asyncio.run(delivery.deliver("in_app", "t", big))
    assert rec["status"] == "dropped"
    assert channel_store.list_deliveries()[0]["status"] == "dropped"


def test_desktop_channel_no_target_required(stores, monkeypatch):
    delivery, _, _, _ = stores
    monkeypatch.setattr(delivery, "_send_desktop", lambda *a, **k: "notification posted")
    rec = asyncio.run(delivery.deliver("desktop", "", "ping", subject="Alert"))
    assert rec["status"] == "sent"


# ─── obsidian vault export ────────────────────────────────────────────────────

def test_obsidian_writes_markdown_note(stores, tmp_path):
    delivery, channel_store, _, _ = stores
    from storage import settings
    vault = tmp_path / "vault"
    settings.set("obsidianVault", str(vault))
    rec = asyncio.run(delivery.deliver("obsidian", str(vault), "run output here",
                                       subject="Skill Test Run", agent_id="a1"))
    assert rec["status"] == "sent"
    notes = list(vault.rglob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert "run output here" in body
    assert "title: Skill Test Run" in body
    assert channel_store.list_deliveries(agent_id="a1")[0]["status"] == "sent"


def test_obsidian_requires_explicit_target(stores):
    delivery, _, _, _ = stores
    rec = asyncio.run(delivery.deliver("obsidian", "", "text"))
    assert rec["status"] == "failed"
    assert "explicit 'target'" in rec["error"]


# ─── webhook opt-in / allow-list ─────────────────────────────────────────────

def test_webhook_blocked_unless_allowlisted(stores):
    delivery, _, _, _ = stores
    rec = asyncio.run(delivery.deliver("webhook", "https://example.com/hook", "hi"))
    assert rec["status"] == "failed" and "allow-list" in rec["error"]


def test_ensure_webhook_adds_to_allowlist(stores):
    _, channel_store, _, _ = stores
    assert channel_store.webhook_allowlist() == set()
    ch = channel_store.ensure_webhook("https://example.com/hook", name="n8n")
    assert ch["enabled"] is True
    assert "https://example.com/hook" in channel_store.webhook_allowlist()
    # idempotent — calling again reuses the channel
    again = channel_store.ensure_webhook("https://example.com/hook")
    assert again["id"] == ch["id"]


def test_disabled_webhook_channel_not_in_allowlist(stores):
    _, channel_store, _, _ = stores
    ch = channel_store.create_channel("webhook", "off", enabled=False,
                                      config={"urls": ["https://example.com/x"]})
    assert ch["enabled"] is False
    assert channel_store.webhook_allowlist() == set()


# ─── deliver tool (agent-facing) ─────────────────────────────────────────────

def test_deliver_tool_shape_and_explicit_target(stores):
    delivery, _, chat_store, _ = stores
    tools = delivery.make_deliver_tool("a1")
    assert set(tools) == {"deliver"}
    fn = tools["deliver"]["fn"]
    # missing pieces are handled gracefully
    assert "needs 'channel'" in asyncio.run(fn(channel="", text="x"))
    assert "needs 'text'" in asyncio.run(fn(channel="in_app", text=""))
    assert "explicit 'target'" in asyncio.run(fn(channel="in_app", text="x", target=""))
    # a real in-app send works and reports the delivery id
    out = asyncio.run(fn(channel="in_app", target="inbox", text="done"))
    assert "delivered via in_app" in out
    assert chat_store.get_session("inbox") is not None


# ─── schedule deliver_to round-trip ──────────────────────────────────────────

def test_schedule_stores_deliver_to(stores):
    _, _, _, schedule_store = stores
    s = schedule_store.create("a1", "Daily", "summarize", kind="cron",
                              cron_expr="0 8 * * *",
                              deliver_to=[{"channel": "in_app", "target": "inbox"},
                                          {"channel": "desktop", "target": ""}])
    assert s["deliverTo"] == [{"channel": "in_app", "target": "inbox"},
                              {"channel": "desktop", "target": ""}]
    # update can clear / replace targets
    upd = schedule_store.update(s["id"], deliverTo=[{"channel": "in_app", "target": "other"}])
    assert upd["deliverTo"] == [{"channel": "in_app", "target": "other"}]
    # malformed entries are dropped
    upd2 = schedule_store.update(s["id"], deliverTo=[{"target": "no-channel"}, "junk"])
    assert upd2["deliverTo"] == []


def test_existing_schedule_has_empty_deliver_to(stores):
    """The guarded migration leaves pre-existing schedules at 'Activity only'."""
    _, _, _, schedule_store = stores
    s = schedule_store.create("a1", "Legacy", "x", kind="cron", cron_expr="0 9 * * *")
    assert s["deliverTo"] == []


# ─── scheduler integration: explicit target, no captured recipient ───────────

def test_scheduler_delivers_to_explicit_target(stores):
    """A fired schedule delivers its result to the *configured* target only —
    never a captured chat sender (the engine PR-#648 regression)."""
    delivery, channel_store, chat_store, schedule_store = stores
    from agent.scheduler import SchedulerManager

    s = schedule_store.create("a1", "Nightly", "run it", kind="cron",
                              cron_expr="0 2 * * *",
                              deliver_to=[{"channel": "in_app", "target": "ops-inbox"}])
    mgr = SchedulerManager()
    asyncio.run(mgr._deliver_result(s, "the nightly result"))

    # Delivered to the explicit, schedule-sourced target — and audited as such.
    rows = channel_store.list_deliveries(schedule_id=s["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["target"] == "ops-inbox"   # exactly what the config named
    assert rows[0]["source"] == "schedule"
    sess = chat_store.get_session("ops-inbox")
    assert "the nightly result" in sess["messages"][-1]["content"]


def test_scheduler_no_targets_is_activity_only(stores):
    delivery, channel_store, _, schedule_store = stores
    from agent.scheduler import SchedulerManager

    s = schedule_store.create("a1", "Quiet", "x", kind="cron", cron_expr="0 2 * * *")
    mgr = SchedulerManager()
    asyncio.run(mgr._deliver_result(s, "result"))
    assert channel_store.list_deliveries(schedule_id=s["id"]) == []


# ─── paging + search (Deliveries page) ───────────────────────────────────────

def test_deliveries_page_offset_total_and_counts(stores):
    _, channel_store, _, _ = stores
    for i in range(7):
        channel_store.log_delivery(channel="chat:telegram", target=f"t{i}", source="reply",
                                   status="failed" if i == 3 else "sent", payload_text=f"msg {i}")
    page1 = channel_store.list_deliveries(limit=3, offset=0)
    page3 = channel_store.list_deliveries(limit=3, offset=6)
    # newest first; same-second rows keep insertion order (rowid tiebreak) so pages never overlap
    assert [r["target"] for r in page1] == ["t6", "t5", "t4"]
    assert [r["target"] for r in page3] == ["t0"]
    all_ids = [r["id"] for o in (0, 3, 6) for r in channel_store.list_deliveries(limit=3, offset=o)]
    assert len(all_ids) == len(set(all_ids)) == 7
    c = channel_store.count_deliveries()
    assert c == {"total": 7, "byStatus": {"sent": 6, "failed": 1}}
    # a status filter narrows total but the chips still tally every status
    assert channel_store.count_deliveries(status="failed") == {"total": 1, "byStatus": {"sent": 6, "failed": 1}}


def test_deliveries_search_is_server_side_and_literal(stores):
    _, channel_store, _, _ = stores
    channel_store.log_delivery(channel="email", target="Ops@Example.com", source="tool", status="sent",
                               payload_text="weekly report")
    channel_store.log_delivery(channel="webhook", target="https://h", source="tool", status="failed",
                               error="HTTP 500 upstream", payload_text="50% done")
    channel_store.log_delivery(channel="webhook", target="https://h", source="tool", status="sent",
                               payload_text="500 rows")
    assert [r["target"] for r in channel_store.list_deliveries(search="ops@example")] == ["Ops@Example.com"]
    assert len(channel_store.list_deliveries(search="REPORT")) == 1
    assert channel_store.count_deliveries(search="upstream")["total"] == 1      # matches the error text
    assert [r["payloadText"] for r in channel_store.list_deliveries(search="50%")] == ["50% done"]  # % is literal
    assert channel_store.list_deliveries(search="_") == []                       # _ is literal too


def test_deliveries_route_returns_page_metadata(stores):
    from fastapi.testclient import TestClient
    from app import app
    _, channel_store, _, _ = stores
    for i in range(5):
        channel_store.log_delivery(channel="desktop", target=None, source="tool", status="sent",
                                   payload_text=f"n{i}")
    body = TestClient(app).get("/api/deliveries", params={"limit": 2, "offset": 2}).json()
    assert len(body["deliveries"]) == 2 and body["total"] == 5
    assert body["limit"] == 2 and body["offset"] == 2 and body["byStatus"] == {"sent": 5}
    assert TestClient(app).get("/api/deliveries", params={"q": "n4"}).json()["total"] == 1
