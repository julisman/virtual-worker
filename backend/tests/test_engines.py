"""Inference-engine version reporting + update-availability logic."""

import asyncio

from runtime import engines


def test_update_available_pip_semver():
    assert engines._update_available("0.31.3", "0.32.0", "pip") is True
    assert engines._update_available("0.31.3", "0.31.3", "pip") is False
    assert engines._update_available("0.32.0", "0.31.3", "pip") is False  # installed newer


def test_update_available_brew_build_number():
    assert engines._update_available("9301", "9820", "brew") is True
    assert engines._update_available("9820", "9820", "brew") is False
    assert engines._update_available("9820", "9301", "brew") is False


def test_update_available_handles_missing():
    assert engines._update_available(None, "1.0", "pip") is False
    assert engines._update_available("1.0", None, "pip") is False


def test_digits():
    assert engines._digits("version: 9301 (abc)") == 9301
    assert engines._digits("b5847") == 5847
    assert engines._digits(None) is None
    assert engines._digits("none") is None


def test_current_shape(monkeypatch):
    monkeypatch.setattr(engines, "_installed", lambda e: "1.2.3")
    monkeypatch.setattr(engines, "_can_update", lambda e: True)
    rows = engines.current()
    keys = {r["key"] for r in rows}
    assert {"llama.cpp", "mlx-lm", "mlx-vlm", "mlx"} <= keys
    for r in rows:
        assert set(("key", "label", "manager", "installed", "available", "canUpdate")) <= set(r)
        assert r["installed"] == "1.2.3" and r["available"] is True


def test_check_merges_latest(monkeypatch):
    monkeypatch.setattr(engines, "_installed", lambda e: "1.0.0")
    monkeypatch.setattr(engines, "_can_update", lambda e: True)

    async def fake_latest(engine):
        return "9999" if engine["kind"] == "brew" else "2.0.0"
    monkeypatch.setattr(engines, "_latest", fake_latest)

    rows = asyncio.run(engines.check())
    by = {r["key"]: r for r in rows}
    assert by["mlx-lm"]["latest"] == "2.0.0" and by["mlx-lm"]["updateAvailable"] is True
    assert by["llama.cpp"]["latest"] == "9999" and by["llama.cpp"]["updateAvailable"] is True


def test_update_unknown_engine():
    res = asyncio.run(engines.update("nope"))
    assert res["ok"] is False and "unknown engine" in res["output"]


def test_update_cmd_shapes(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: f"/bin/{x}")
    brew_cmd = engines._update_cmd(engines._by_key("llama.cpp"))
    assert brew_cmd[:3] == ["/bin/brew", "upgrade", "llama.cpp"]
    pip_cmd = engines._update_cmd(engines._by_key("mlx-lm"))
    assert pip_cmd[0] == "/bin/uv" and pip_cmd[1:4] == ["pip", "install", "--python"]
    assert pip_cmd[-2:] == ["-U", "mlx-lm"]
