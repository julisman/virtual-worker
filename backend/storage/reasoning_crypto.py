"""At-rest encryption for plaintext reasoning sidecars (PRD-27).

DeepSeek/Kimi behind the custom Responses gateway return their chain-of-thought
as *plaintext* (`content[].text`, no opaque `encrypted_content`). Replaying that
means persisting readable reasoning into ``app.db``, so we encrypt it at rest
under a user-provided key — the key IS the consent.

Crypto (fixed spec, security-critical — do not improvise):
  - key derivation: scrypt(key_text, salt, n=2**14, r=8, p=1, dklen=32)
  - cipher: AES-GCM, fresh 12-byte nonce per encryption, canonical structured AAD
    binding the ciphertext to its own row identity (blocks row-swap).
  - envelope (stored in the ``items`` column when ``enc_version=1``):
    ``{"n": base64(nonce), "ct": base64(ct)}``.
  - fail-closed: decrypt returns ``None`` on ANY failure — never raises into the
    loop, never logs key material, nonce, ciphertext, or plaintext reasoning.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AAD_LABEL = b"vw.reasoning.sidecar.v1|"


def derive_key(key_text: str, salt_bytes: bytes) -> bytes:
    """scrypt-derive a 32-byte AES key from the operator's key text + per-provider
    salt. Deliberately slow so a leaked db can't be cheaply brute-forced."""
    return hashlib.scrypt(
        password=key_text.encode("utf-8"), salt=salt_bytes,
        n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def build_aad(session_id: str, seq: int, fingerprint: str,
              provider: str | None, model: str | None,
              origin_id: str | None) -> bytes:
    """Canonical, structured AAD rebuilt identically on encrypt and decrypt from
    the row's own identity columns. Binds the ciphertext to its row."""
    body = json.dumps([session_id, seq, fingerprint, provider, model, origin_id],
                      separators=(",", ":"), ensure_ascii=False)
    return _AAD_LABEL + body.encode("utf-8")


def encrypt_items(derived_key: bytes, items: list[dict], aad: bytes) -> dict:
    """Encrypt the items JSON into an envelope dict ``{"n":…, "ct":…}`` (base64).
    Fresh nonce every call. The caller serializes the envelope into the DB
    ``items`` column (so a ``enc_version=1`` row holds only the envelope)."""
    plaintext = json.dumps(items, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    nonce = os.urandom(12)
    ct = AESGCM(derived_key).encrypt(nonce, plaintext, aad)
    return {
        "n": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def decrypt_items(derived_key: bytes, envelope: str | dict, aad: bytes) -> list[dict] | None:
    """Decrypt an envelope (a JSON string from the DB column, or the dict itself)
    back to the items list. Returns ``None`` on ANY failure (no key / wrong key /
    tamper / bad AAD) — never raises, never logs."""
    try:
        env = json.loads(envelope) if isinstance(envelope, str) else envelope
        nonce = base64.b64decode(env["n"])
        ct = base64.b64decode(env["ct"])
        plaintext = AESGCM(derived_key).decrypt(nonce, ct, aad)
        items = json.loads(plaintext.decode("utf-8"))
        return items if isinstance(items, list) else None
    except Exception:
        return None
