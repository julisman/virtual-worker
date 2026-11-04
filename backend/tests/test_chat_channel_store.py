"""Chat-channel store — accounts, write-only secrets, bindings, dedup, pairing."""

import config
from storage import chat_channel_store as store


def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)


def test_account_crud_and_config_defaults(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    a = store.create_account("whatsapp", "Main number", config={"dmPolicy": "bogus", "debounceMs": "x"})
    assert a["kind"] == "whatsapp" and a["enabled"] is False
    assert a["config"]["dmPolicy"] == "pairing"           # invalid → default
    assert a["config"]["debounceMs"] == store.DEFAULT_CONFIG["debounceMs"]
    b = store.update_account(a["id"], enabled=True, config={"allowFrom": ["628111", " 628222 ", ""]})
    assert b["enabled"] is True and b["config"]["allowFrom"] == ["628111", "628222"]
    assert b["config"]["groupPolicy"] == "mention"        # untouched keys survive a partial patch
    assert [x["id"] for x in store.list_accounts()] == [a["id"]]
    assert store.delete_account(a["id"]) is True
    assert store.get_account(a["id"]) is None


def test_secrets_are_write_only_and_live_outside_the_db(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    a = store.create_account("whatsapp_cloud", "biz")
    store.set_secrets(a["id"], {"accessToken": "tok", "appSecret": "sec", "verifyToken": ""})
    assert store.get_secrets(a["id"]) == {"accessToken": "tok", "appSecret": "sec"}
    flags = store.secret_flags(a["id"], ["accessToken", "appSecret", "verifyToken"])
    assert flags == {"hasAccessToken": True, "hasAppSecret": True, "hasVerifyToken": False}
    store.set_secrets(a["id"], {"appSecret": ""})
    assert store.get_secrets(a["id"]) == {"accessToken": "tok"}
    assert "tok" not in str(store.get_account(a["id"]))
    store.delete_account(a["id"])
    assert store.get_secrets(a["id"]) == {}


def test_bindings_upsert_and_delete(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    a = store.create_account("whatsapp", "n")
    b1 = store.upsert_binding(a["id"], "123-456@g.us", "agent-A", title="ops group")
    b2 = store.upsert_binding(a["id"], "123-456@g.us", "agent-B")
    assert b1["id"] == b2["id"] and b2["agentId"] == "agent-B" and b2["title"] == "ops group"
    assert store.get_binding(a["id"], "123-456@g.us")["agentId"] == "agent-B"
    assert store.get_binding(a["id"], "nope") is None
    assert store.delete_binding(b1["id"]) is True
    assert store.list_bindings(a["id"]) == []


def test_dedup_is_insert_before_run(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    assert store.seen_before("acct", "evt-1") is False
    assert store.seen_before("acct", "evt-1") is True
    assert store.seen_before("other", "evt-1") is False
    assert store.seen_before("acct", "") is False
    assert store.prune_events(days=0) >= 2


def test_pairing_request_lifecycle_adds_sender_to_allowlist(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    a = store.create_account("whatsapp", "n")
    req, created = store.request_pairing(a["id"], "628999", "Agung")
    assert created and req["status"] == "pending" and len(req["code"]) == 6
    req2, created2 = store.request_pairing(a["id"], "628999", "Agung")
    assert not created2 and req2["id"] == req["id"]
    assert [r["id"] for r in store.list_pairing_requests(a["id"])] == [req["id"]]
    done = store.resolve_pairing(req["id"], approve=True)
    assert done["status"] == "approved"
    assert "628999" in store.get_account(a["id"])["config"]["allowFrom"]
    assert store.list_pairing_requests(a["id"]) == []
    assert store.resolve_pairing("missing", approve=False) is None
