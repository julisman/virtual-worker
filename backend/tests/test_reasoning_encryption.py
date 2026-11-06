"""At-rest encryption of plaintext reasoning sidecars (PRD-27) — hermetic.

Covers the crypto round-trip + tamper cases, the fail-closed persistence path
(no key => plaintext never stored), luna summary stripping, replay gating, key
clear/change purge, and the >=8-char key rule. No network, no model."""

import os

import pytest

import config
from gateway import providers
from storage import reasoning_crypto, reasoning_store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    return tmp_path


def _plaintext_item():
    return {"type": "reasoning", "id": "rs_1",
            "content": [{"type": "reasoning_text", "text": "step one, then two"}]}


def _luna_item():
    return {"type": "reasoning", "id": "rs_2",
            "encrypted_content": "OPAQUE-CIPHERTEXT",
            "summary": [{"type": "summary_text", "text": "readable summary"}]}


# --- crypto round-trip ------------------------------------------------------ #

def test_aes_gcm_round_trip():
    salt = os.urandom(16)
    key = reasoning_crypto.derive_key("correct horse battery", salt)
    items = [_plaintext_item(), {"type": "function_call", "call_id": "c1"}]
    aad = reasoning_crypto.build_aad("sess", 3, "fp", "openai-compatible", "kimi-k3", "http://gw")
    env = reasoning_crypto.encrypt_items(key, items, aad)
    import json
    assert "step one" not in json.dumps(env)  # ciphertext carries no readable reasoning
    assert reasoning_crypto.decrypt_items(key, env, aad) == items


def test_decrypt_wrong_key_returns_none():
    salt = os.urandom(16)
    key = reasoning_crypto.derive_key("right-key", salt)
    other = reasoning_crypto.derive_key("wrong-key", salt)
    aad = reasoning_crypto.build_aad("s", 1, "fp", "p", "m", "o")
    env = reasoning_crypto.encrypt_items(key, [_plaintext_item()], aad)
    assert reasoning_crypto.decrypt_items(other, env, aad) is None


def test_decrypt_tampered_ciphertext_returns_none():
    salt = os.urandom(16)
    key = reasoning_crypto.derive_key("a-key-1234", salt)
    aad = reasoning_crypto.build_aad("s", 1, "fp", "p", "m", "o")
    env = reasoning_crypto.encrypt_items(key, [_plaintext_item()], aad)
    ct = env["ct"]
    env["ct"] = ("A" if ct[0] != "A" else "B") + ct[1:]  # flip a ciphertext byte
    assert reasoning_crypto.decrypt_items(key, env, aad) is None


def test_decrypt_wrong_aad_returns_none():
    salt = os.urandom(16)
    key = reasoning_crypto.derive_key("a-key-1234", salt)
    aad = reasoning_crypto.build_aad("s", 1, "fp", "p", "m", "o")
    env = reasoning_crypto.encrypt_items(key, [_plaintext_item()], aad)
    swapped = reasoning_crypto.build_aad("s", 2, "fp", "p", "m", "o")  # row-swap
    assert reasoning_crypto.decrypt_items(key, env, swapped) is None


# --- persistence path (fail-closed) ----------------------------------------- #

def test_no_key_plaintext_not_persisted(home):
    reasoning_store.persist("sess", 1, "fp", [_plaintext_item()],
                            provider="openai-compatible", model="kimi-k3",
                            origin_id="http://gw", enc_key=None)
    assert reasoning_store.get_for_session("sess") == []


def test_key_present_plaintext_persisted_encrypted_and_decrypts(home):
    key = reasoning_crypto.derive_key("a-strong-key", os.urandom(16))
    items = [_plaintext_item()]
    reasoning_store.persist("sess", 1, "fp", items, provider="openai-compatible",
                            model="kimi-k3", origin_id="http://gw", enc_key=key)
    # Wrong/absent key => fail-closed skip (and purge).
    assert reasoning_store.get_for_session("sess", None) == []
    key2 = reasoning_crypto.derive_key("a-strong-key", os.urandom(16))
    reasoning_store.persist("sess", 1, "fp", items, provider="openai-compatible",
                            model="kimi-k3", origin_id="http://gw", enc_key=key2)
    loaded = reasoning_store.get_for_session("sess", key2)
    assert len(loaded) == 1 and loaded[0]["items"] == items


def test_luna_summary_stripped_encrypted_content_preserved(home):
    reasoning_store.persist("sess", 1, "fp", [_luna_item()],
                            provider="openai-compatible", model="gpt-5.6-luna",
                            origin_id="http://gw", enc_key=None)
    loaded = reasoning_store.get_for_session("sess")
    assert len(loaded) == 1
    item = loaded[0]["items"][0]
    assert "summary" not in item
    assert item["encrypted_content"] == "OPAQUE-CIPHERTEXT"


# --- replay gating ---------------------------------------------------------- #

def test_has_replayable_reasoning_plaintext_gated():
    plain = [_plaintext_item()]
    assert reasoning_store.has_replayable_reasoning(plain, allow_plaintext=False) is False
    assert reasoning_store.has_replayable_reasoning(plain, allow_plaintext=True) is True


def test_has_replayable_reasoning_encrypted_always_true():
    luna = [_luna_item()]
    assert reasoning_store.has_replayable_reasoning(luna, allow_plaintext=False) is True
    assert reasoning_store.has_replayable_reasoning(luna, allow_plaintext=True) is True


# --- key clear / change purge ----------------------------------------------- #

def test_purge_encrypted_plaintext_removes_enc_rows(home):
    key = reasoning_crypto.derive_key("a-strong-key", os.urandom(16))
    reasoning_store.persist("s1", 1, "fp", [_plaintext_item()],
                            provider="openai-compatible", model="kimi-k3",
                            origin_id="o", enc_key=key)
    reasoning_store.persist("s2", 1, "fp", [_luna_item()],
                            provider="openai-compatible", model="gpt-5.6-luna",
                            origin_id="o", enc_key=None)
    removed = reasoning_store.purge_encrypted_plaintext()
    assert removed == 1
    assert reasoning_store.get_for_session("s1", key) == []       # enc row gone
    assert len(reasoning_store.get_for_session("s2")) == 1        # luna row kept


def test_set_record_key_change_purges(home):
    providers.set_record("openai-compatible", baseUrl="http://gw/v1",
                         apiType="responses", reasoningKey="first-key-1")
    salt1 = providers.get_record("openai-compatible")["reasoningSalt"]
    key1 = reasoning_crypto.derive_key("first-key-1", bytes.fromhex(salt1))
    reasoning_store.persist("s", 1, "fp", [_plaintext_item()],
                            provider="openai-compatible", model="kimi-k3",
                            origin_id="o", enc_key=key1)
    # Changing the key regenerates the salt and purges stale encrypted rows.
    providers.set_record("openai-compatible", reasoningKey="second-key-2")
    salt2 = providers.get_record("openai-compatible")["reasoningSalt"]
    assert salt2 != salt1
    assert reasoning_store.get_for_session("s", key1) == []


def test_set_record_clear_key_purges_and_drops_fields(home):
    providers.set_record("openai-compatible", baseUrl="http://gw/v1",
                         apiType="responses", reasoningKey="first-key-1")
    reasoning_store.persist("s", 1, "fp", [_plaintext_item()],
                            provider="openai-compatible", model="kimi-k3",
                            origin_id="o",
                            enc_key=reasoning_crypto.derive_key(
                                "first-key-1",
                                bytes.fromhex(providers.get_record("openai-compatible")["reasoningSalt"])))
    providers.set_record("openai-compatible", reasoningKey="")
    rec = providers.get_record("openai-compatible")
    assert "reasoningKey" not in rec and "reasoningSalt" not in rec
    assert reasoning_store.get_for_session("s", None) == []


# --- validation + catalog masking ------------------------------------------- #

def test_set_record_rejects_short_key(home):
    with pytest.raises(ValueError):
        providers.set_record("openai-compatible", reasoningKey="short")


def test_catalog_exposes_has_reasoning_key_never_value(home):
    providers.set_record("openai-compatible", baseUrl="http://gw/v1",
                         apiType="responses", reasoningKey="a-secret-key-1")
    custom = {p["id"]: p for p in providers.catalog()}["openai-compatible"]
    assert custom["hasReasoningKey"] is True
    assert "a-secret-key-1" not in repr(custom)
    assert "reasoningKey" not in custom and "reasoningSalt" not in custom
    assert "reasoningSalt" not in repr(custom)


def test_resolve_target_stamps_enc_key_and_retain_flag(home):
    import asyncio
    providers.set_record("openai-compatible", baseUrl="http://gw/v1",
                         apiType="responses", authMode="none",
                         reasoningKey="a-secret-key-1")
    _m, target = asyncio.run(providers.resolve_target(
        {"modelSource": "byok", "provider": "openai-compatible", "model": "kimi-k3"}))
    assert target["retain_plaintext_reasoning"] is True
    assert isinstance(target["reasoning_enc_key"], bytes) and len(target["reasoning_enc_key"]) == 32
