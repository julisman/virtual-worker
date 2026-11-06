"""settings.json holds provider secrets in cleartext, so it must be written with
owner-only (0600) permissions."""

import stat
import sys

import pytest

import config
from storage import settings


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX perms only")
def test_ves_json_is_owner_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    settings.set("providers", {"openai": {"apiKey": "sk-secret"}})
    p = tmp_path / "settings.json"
    assert p.exists()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
