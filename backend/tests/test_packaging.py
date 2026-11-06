"""Packaged-mode config resolution (VW_PACKAGED / VW_HOME / bundled LLAMA_BIN)
and the frontend static-dir discovery used by the desktop build."""

import importlib
from pathlib import Path


def _reload_config(monkeypatch, env: dict):
    import config
    for k in ("VW_PACKAGED", "VW_HOME", "LLAMA_BIN", "VW_RESOURCES_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)


def test_dev_defaults_to_dot_ves(monkeypatch):
    cfg = _reload_config(monkeypatch, {})
    assert cfg.PACKAGED is False
    assert cfg.APP_HOME == Path.home() / ".virtual-worker"
    assert cfg.LLAMA_BIN == "llama-server"          # expects it on PATH in dev


def test_ves_home_override_wins_even_when_packaged(monkeypatch, tmp_path):
    cfg = _reload_config(monkeypatch, {"VW_PACKAGED": "1", "VW_HOME": str(tmp_path)})
    assert cfg.PACKAGED is True
    assert cfg.APP_HOME == tmp_path


def test_packaged_home_is_app_support_on_macos(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    cfg = _reload_config(monkeypatch, {"VW_PACKAGED": "1"})
    assert cfg.APP_HOME == Path.home() / "Library" / "Application Support" / "Virtual Worker"


def test_packaged_uses_bundled_llama_when_present(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "llama-server").write_text("#!/bin/sh\n")
    cfg = _reload_config(monkeypatch, {"VW_PACKAGED": "1", "VW_RESOURCES_DIR": str(tmp_path)})
    assert cfg.LLAMA_BIN == str(bindir / "llama-server")


def test_explicit_llama_bin_always_wins(monkeypatch, tmp_path):
    cfg = _reload_config(monkeypatch, {"VW_PACKAGED": "1", "VW_RESOURCES_DIR": str(tmp_path),
                                       "LLAMA_BIN": "/custom/llama-server"})
    assert cfg.LLAMA_BIN == "/custom/llama-server"


def test_config_reload_restores_default(monkeypatch):
    # leave config.py back in its normal (dev) state for other tests in the run
    _reload_config(monkeypatch, {})
