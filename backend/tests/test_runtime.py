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
    import config
    args = mgr._mlx_args("/models/mlx-x", 8080, config.mlx_bin(), "mlx_lm.server")
    assert args[0] == config.mlx_bin()  # console script resolved via PATH
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

    async def probe(*a, **k):
        return True, "m"
    monkeypatch.setattr(mgr, "_probe", probe)

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
    assert mgr._load_pref() == {"path": "/models/x.gguf", "ctx": 4096, "ngl": 99,
                                "backend": "llamacpp", "adapter": None,
                                "mtp": False, "mtpNMax": mgr._MTP_N_MAX_DEFAULT}
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
    async def fake_start(path, ctx=None, ngl=None, force=False, adapter=None, **kw):
        started.update(path=path, ctx=ctx, ngl=ngl)
        return {"running": True}
    monkeypatch.setattr(mgr, "start", fake_start)

    asyncio.run(mgr.adopt_or_autostart())
    assert started == {"path": str(model), "ctx": 16384, "ngl": 50}


def test_verify_probes_the_started_model_not_the_first_listed(monkeypatch):
    """mlx_vlm.server lists the whole HF cache (oldest first) on /v1/models and
    treats an inbound `model` as a selector — probing data[0] made verify unload
    the model we just started and load an unrelated one, then report 'ok'."""
    _stub_verify_env(monkeypatch)
    sent = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):  # a stale model is listed first
            raise AssertionError("verify must not ask /v1/models which model to probe")
        async def post(self, url, json=None, **k):
            sent.update(json or {})
            return FakeResp()

    monkeypatch.setattr(mgr.httpx, "AsyncClient", FakeClient)
    asyncio.run(mgr._run_verify("/models/new-27b", 8080))
    assert mgr._verify["state"] == "ok"
    assert sent["model"] == "/models/new-27b"


def test_active_model_id_prefers_started_path_over_v1_models(monkeypatch):
    """Echoing back /v1/models[0] swaps the loaded model on mlx-vlm; callers
    (verify, eval_runner) must get the path Virtual Worker actually started."""
    monkeypatch.setattr(mgr, "_state", {**mgr._state, "path": "/models/new-27b"})

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            raise AssertionError("should not query /v1/models when a path is known")

    monkeypatch.setattr(mgr.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(mgr._active_model_id(8080)) == "/models/new-27b"


def _capture_spawn_args(monkeypatch, tmp_path):
    """Run mgr._spawn with Popen stubbed; return the argv it would exec."""
    import subprocess
    monkeypatch.setattr(mgr.config, "APP_HOME", tmp_path)
    monkeypatch.setattr(mgr, "APP_HOME", tmp_path)
    monkeypatch.setattr(mgr.shutil, "which", lambda *_: "/bin/llama-server")
    captured = {}
    class FakeProc:
        pid = 4242
    def fake_popen(args, **kw):
        captured["args"] = args
        return FakeProc()
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return captured


def test_spawn_adds_mtp_flags_for_llamacpp(tmp_path, monkeypatch):
    cap = _capture_spawn_args(monkeypatch, tmp_path)
    pid = mgr._spawn("/m/x.gguf", 8192, 99, 8080, "llamacpp", None, mtp=True, mtp_n_max=3)
    assert pid == 4242
    a = cap["args"]
    assert "--spec-type" in a and a[a.index("--spec-type") + 1] == "draft-mtp"
    assert "--model-draft" in a and a[a.index("--model-draft") + 1] == "/m/x.gguf"
    assert a[a.index("--spec-draft-n-max") + 1] == "3"


def test_spawn_clamps_mtp_n_max(tmp_path, monkeypatch):
    cap = _capture_spawn_args(monkeypatch, tmp_path)
    mgr._spawn("/m/x.gguf", 8192, 99, 8080, "llamacpp", None, mtp=True, mtp_n_max=999)
    a = cap["args"]
    assert a[a.index("--spec-draft-n-max") + 1] == str(mgr._MTP_N_MAX_MAX)


def test_spawn_no_mtp_flags_when_disabled(tmp_path, monkeypatch):
    cap = _capture_spawn_args(monkeypatch, tmp_path)
    mgr._spawn("/m/x.gguf", 8192, 99, 8080, "llamacpp", None, mtp=False)
    assert "--spec-type" not in cap["args"]
