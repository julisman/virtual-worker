"""llama runtime manager — logic that doesn't need a real llama-server binary."""

import asyncio

import pytest

from runtime import llama_manager as mgr


def test_start_rejects_missing_gguf():
    with pytest.raises(FileNotFoundError):
        asyncio.run(mgr.start("/does/not/exist.gguf"))


def test_start_rejects_missing_mlx_dir(tmp_path):
    # non-.gguf path is treated as MLX; a missing dir must error
    with pytest.raises(FileNotFoundError):
        asyncio.run(mgr.start(str(tmp_path / "no-such-mlx-model")))


def test_backend_inference():
    assert mgr._backend_for("/x/model-Q4_K_M.gguf") == "llamacpp"
    assert mgr._backend_for("/x/models--mlx-community--Qwen3-8B-4bit/snapshots/abc") == "mlx"


def test_mlx_args_shape(monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda *_: "/opt/bin/mlx_lm.server")
    args = mgr._mlx_args("/models/mlx-x", 8080, mgr.MLX_BIN, "mlx_lm.server")
    assert args[0] == mgr.MLX_BIN  # console script resolved via PATH
    assert "--model" in args and "/models/mlx-x" in args and "8080" in args


def test_backend_for_multimodal(tmp_path):
    import json
    d = tmp_path / "models--x--vlm" / "snapshots" / "rev"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"vision_config": {}, "text_config": {}}))
    assert mgr._backend_for(str(d)) == "mlx-vlm"
    d2 = tmp_path / "models--x--txt" / "snapshots" / "rev"
    d2.mkdir(parents=True)
    (d2 / "config.json").write_text(json.dumps({"hidden_size": 1}))
    assert mgr._backend_for(str(d2)) == "mlx"


def test_status_shape_when_idle(monkeypatch):
    # no process tracked -> not running, fields present
    monkeypatch.setattr(mgr, "_state", {**mgr._state, "pid": None, "managed": False})
    st = asyncio.run(mgr.status())
    for k in ("running", "healthy", "loading", "managed", "modelName", "binAvailable", "port"):
        assert k in st
    assert st["running"] is False


def test_status_reflects_verify_states(monkeypatch):
    monkeypatch.setattr(mgr, "_state", {**mgr._state, "pid": 123, "managed": True, "path": "/m.gguf"})
    monkeypatch.setattr(mgr, "_pid_alive", lambda pid: True)

    async def healthy(*a, **k):
        return True
    async def aid(*a, **k):
        return "m"
    monkeypatch.setattr(mgr, "_healthy", healthy)
    monkeypatch.setattr(mgr, "_active_model_id", aid)

    # verifying: optimistic — a reachable server is ready, probe runs in background
    monkeypatch.setattr(mgr, "_verify", {"state": "verifying", "error": None, "path": "/m.gguf"})
    st = asyncio.run(mgr.status())
    assert st["healthy"] is True and st["loading"] is False
    assert st["verifying"] is True and st["failed"] is False

    monkeypatch.setattr(mgr, "_verify", {"state": "ok", "error": None, "path": "/m.gguf"})
    st = asyncio.run(mgr.status())
    assert st["healthy"] is True and st["failed"] is False and st["verifying"] is False

    # failed: probe downgraded it — not healthy, error surfaced
    monkeypatch.setattr(mgr, "_verify",
                        {"state": "failed", "error": "Missing 399 parameters", "path": "/m.gguf"})
    st = asyncio.run(mgr.status())
    assert st["failed"] is True and st["healthy"] is False
    assert "Missing 399" in st["error"]


def _fake_client(status_code, text):
    class FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.text = text
    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return FakeResp()
    return FakeClient


def _stub_verify_env(monkeypatch):
    monkeypatch.setattr(mgr, "_state", {**mgr._state, "pid": 123, "port": 8080, "path": "/m"})
    monkeypatch.setattr(mgr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mgr, "_kill", lambda *_: None)
    monkeypatch.setattr(mgr, "_clear_pref", lambda: None)
    monkeypatch.setattr(mgr, "_clear_state", lambda: None)
    monkeypatch.setattr(mgr, "_invalidate_model_cache", lambda: None)
    monkeypatch.setattr(mgr, "_log_tail", lambda *a, **k: "")

    async def healthy(*a, **k):
        return True
    async def aid(*a, **k):
        return "m"
    monkeypatch.setattr(mgr, "_healthy", healthy)
    monkeypatch.setattr(mgr, "_active_model_id", aid)


def test_run_verify_marks_failed_on_bad_generation(monkeypatch):
    _stub_verify_env(monkeypatch)
    monkeypatch.setattr(mgr.httpx, "AsyncClient", _fake_client(500, "Missing 399 parameters"))
    asyncio.run(mgr._run_verify("/m", 8080))
    assert mgr._verify["state"] == "failed"
    assert "Missing 399" in mgr._verify["error"]


def test_run_verify_marks_ok_on_good_generation(monkeypatch):
    _stub_verify_env(monkeypatch)
    monkeypatch.setattr(mgr.httpx, "AsyncClient", _fake_client(200, '{"choices":[]}'))
    asyncio.run(mgr._run_verify("/m", 8080))
    assert mgr._verify["state"] == "ok"


def test_pid_alive_false_for_bogus():
    assert mgr._pid_alive(None) is False
    assert mgr._pid_alive(2_000_000_000) is False  # almost certainly not a real pid


def test_stop_is_idempotent():
    # stopping when nothing runs should be safe and report not running
    assert mgr.stop()["running"] is False


def test_active_model_pref_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr.config, "APP_HOME", tmp_path)
    assert mgr._load_pref() is None
    mgr._save_pref("/models/x.gguf", 4096, 99)
    assert mgr._load_pref() == {"path": "/models/x.gguf", "ctx": 4096, "ngl": 99, "backend": "llamacpp", "adapter": None}
    # stored in the single settings file
    import json
    assert json.loads((tmp_path / "settings.json").read_text())["activeModel"]["ctx"] == 4096
    mgr._clear_pref()
    assert mgr._load_pref() is None


def test_stop_clears_pref(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr.config, "APP_HOME", tmp_path)
    mgr._save_pref("/models/x.gguf", 4096, 99)
    mgr.stop()                       # explicit stop forgets the choice
    assert mgr._load_pref() is None


def test_stop_can_keep_pref(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr.config, "APP_HOME", tmp_path)
    mgr._save_pref("/models/x.gguf", 4096, 99)
    mgr.stop(clear_pref=False)       # internal switch keeps it
    assert mgr._load_pref() is not None


def test_settings_migrates_old_pref_file(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(mgr.config, "APP_HOME", tmp_path)
    (tmp_path / "active-model.json").write_text(json.dumps({"path": "/m.gguf", "ctx": 8192, "ngl": 99}))
    pref = mgr._load_pref()
    assert pref["ctx"] == 8192
    assert not (tmp_path / "active-model.json").exists()           # old file removed
    assert json.loads((tmp_path / "settings.json").read_text())["activeModel"]["path"] == "/m.gguf"


def test_autostart_prefers_persisted_model(tmp_path, monkeypatch):
    import shutil as _sh
    model = tmp_path / "m.gguf"
    model.write_text("x")
    monkeypatch.setattr(mgr.config, "MANAGED_LLAMA", True)
    monkeypatch.setattr(mgr, "_load_state", lambda: None)         # no live pid
    monkeypatch.setattr(_sh, "which", lambda *_: "/bin/llama-server")

    async def not_healthy(*_a, **_k):
        return False
    monkeypatch.setattr(mgr, "_healthy", not_healthy)
    monkeypatch.setattr(mgr, "_load_pref", lambda: {"path": str(model), "ctx": 16384, "ngl": 50})

    started = {}
    async def fake_start(path, ctx=None, ngl=None, force=False, adapter=None):
        started.update(path=path, ctx=ctx, ngl=ngl)
        return {"running": True}
    monkeypatch.setattr(mgr, "start", fake_start)

    asyncio.run(mgr.adopt_or_autostart())
    assert started == {"path": str(model), "ctx": 16384, "ngl": 50}
