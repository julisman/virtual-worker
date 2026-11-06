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


# --------------------------------------------------------------------------- #
# managed engines (packaged desktop mode)
# --------------------------------------------------------------------------- #

import config  # noqa: E402


def test_managed_pip_installed_reads_dist_info(tmp_path, monkeypatch):
    venv = tmp_path / "engines" / "mlx"
    site = venv / "lib" / "python3.12" / "site-packages"
    (site / "mlx_lm-0.26.5.dist-info").mkdir(parents=True)
    monkeypatch.setattr(config, "MLX_VENV", venv)
    assert engines._managed_pip_installed("mlx-lm") == "0.26.5"
    assert engines._pip_installed("mlx-lm") == "0.26.5"  # managed wins
    assert engines._managed_pip_installed("mlx-vlm") is None


def test_pip_target_python_prefers_managed_venv(tmp_path, monkeypatch):
    venv = tmp_path / "engines" / "mlx"
    monkeypatch.setattr(config, "MLX_VENV", venv)
    # dev, venv absent → backend interpreter
    monkeypatch.setattr(config, "PACKAGED", False)
    assert engines._pip_target_python() == engines._VENV_PY
    # packaged → always the managed venv (created on demand)
    monkeypatch.setattr(config, "PACKAGED", True)
    assert engines._pip_target_python() == str(venv / "bin" / "python")
    # dev but the venv exists → managed venv
    monkeypatch.setattr(config, "PACKAGED", False)
    py = venv / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.touch()
    assert engines._pip_target_python() == str(py)


def test_mlx_bin_resolution(tmp_path, monkeypatch):
    venv = tmp_path / "engines" / "mlx"
    monkeypatch.setattr(config, "MLX_VENV", venv)
    monkeypatch.delenv("MLX_BIN", raising=False)
    assert config.mlx_bin() == "mlx_lm.server"  # bare name (PATH lookup)
    server = venv / "bin" / "mlx_lm.server"
    server.parent.mkdir(parents=True)
    server.touch()
    assert config.mlx_bin() == str(server)  # managed venv wins
    monkeypatch.setenv("MLX_BIN", "/custom/mlx_lm.server")
    assert config.mlx_bin() == "/custom/mlx_lm.server"  # env outranks all


def test_llama_bin_prefers_managed_copy(tmp_path, monkeypatch):
    managed_dir = tmp_path / "engines" / "llama"
    monkeypatch.setattr(config, "LLAMA_ENGINE_DIR", managed_dir)
    monkeypatch.setenv("LLAMA_BIN", "/bundled/llama-server")
    assert config.llama_bin() == "/bundled/llama-server"  # no managed copy yet
    managed = managed_dir / "llama-server"
    managed.parent.mkdir(parents=True)
    managed.touch()
    managed.chmod(0o755)
    assert config.llama_bin() == str(managed)  # managed update outranks bundled


def test_can_update_packaged(monkeypatch):
    monkeypatch.setattr(config, "PACKAGED", True)
    monkeypatch.setattr(config, "uv_bin", lambda: "/bundled/uv")
    assert engines._can_update(engines._by_key("llama.cpp")) is True  # no brew needed
    assert engines._can_update(engines._by_key("mlx-lm")) is True


def test_update_pip_packaged_creates_venv_and_uses_uv(tmp_path, monkeypatch):
    venv = tmp_path / "engines" / "mlx"
    monkeypatch.setattr(config, "PACKAGED", True)
    monkeypatch.setattr(config, "MLX_VENV", venv)
    monkeypatch.setattr(config, "ENGINES_DIR", tmp_path / "engines")
    monkeypatch.setattr(config, "uv_bin", lambda: "/bundled/uv")

    calls = []

    async def fake_run(cmd, timeout, env=None):
        calls.append((cmd, env))
        if cmd[1] == "venv":  # simulate uv creating the venv
            py = venv / "bin" / "python"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.touch()
        return 0, "ok"

    monkeypatch.setattr(engines, "_run", fake_run)
    monkeypatch.setattr(engines, "_installed", lambda e: "0.26.5")
    res = asyncio.run(engines.update("mlx-lm"))
    assert res["ok"] is True and res["installed"] == "0.26.5"
    assert calls[0][0][:2] == ["/bundled/uv", "venv"]           # venv created first
    assert "UV_PYTHON_INSTALL_DIR" in (calls[0][1] or {})       # python kept in APP_HOME
    assert calls[1][0][:3] == ["/bundled/uv", "pip", "install"]
    assert str(venv / "bin" / "python") in calls[1][0]          # targets the managed venv
    assert calls[1][0][-1] == "mlx-lm"


def test_update_llamacpp_packaged_downloads_release(monkeypatch):
    monkeypatch.setattr(config, "PACKAGED", True)

    async def fake_release():
        return {"ok": True, "returncode": 0, "installed": "9999", "output": "installed"}
    monkeypatch.setattr(engines, "_update_llamacpp_release", fake_release)
    res = asyncio.run(engines.update("llama.cpp"))
    assert res["ok"] is True and res["installed"] == "9999"


def test_extract_llama_release_flattens_and_filters(tmp_path):
    import io
    import tarfile as tf
    tar_path = tmp_path / "rel.tar.gz"
    with tf.open(tar_path, "w:gz") as t:
        for name, keep in [("llama-b9/bin/llama-server", True),
                           ("llama-b9/bin/libggml.dylib", True),
                           ("llama-b9/bin/default.metal", True),
                           ("llama-b9/README.md", False),
                           ("../evil.dylib", True)]:  # traversal name → flattened
            data = b"x"
            info = tf.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    dest = tmp_path / "out"
    copied = engines._extract_llama_release(tar_path, dest)
    assert set(copied) == {"llama-server", "libggml.dylib", "default.metal", "evil.dylib"}
    assert (dest / "llama-server").exists()
    assert not (tmp_path / "evil.dylib").exists()  # never escaped dest
    import os as _os
    assert _os.access(dest / "llama-server", _os.X_OK)
