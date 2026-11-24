"""GGUF LoRA adapters on the llama.cpp runtime: telling adapters / projectors apart
from models, validating base + adapter, and passing ``--lora`` to llama-server.
Hermetic: tiny synthetic GGUF headers, no real model or llama-server binary."""

import asyncio
import struct

import pytest

from routes import hub
from runtime import llama_manager as mgr
from runtime import model_meta


def _kv_str(key: str, val: str) -> bytes:
    return (struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 8)
            + struct.pack("<Q", len(val)) + val.encode())


def _kv_u32(key: str, val: int) -> bytes:
    return struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 4) + struct.pack("<I", val)


INKLING_TMPL = ('{%- set effort_map = {"none": 0.0, "low": 0.2, "high": 0.9} -%}'
                '{%- set eff = reasoning_effort if reasoning_effort is defined else 0.9 -%}')
QWEN_TMPL = ("{%- if enable_thinking is undefined or enable_thinking is true %}"
             "{%- set e = reasoning_effort|default('xhigh') %}")


def _gguf(path, arch: str, gtype: str | None = None, template: str | None = None):
    kvs = [_kv_str("general.architecture", arch)]
    if gtype:
        kvs.append(_kv_str("general.type", gtype))
    if template:
        kvs.append(_kv_str("tokenizer.chat_template", template))
    if gtype in (None, "model"):
        kvs.append(_kv_u32(f"{arch}.context_length", 8192))
    path.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)
                     + struct.pack("<Q", len(kvs)) + b"".join(kvs))
    return path


@pytest.fixture
def files(tmp_path):
    return {
        "base": _gguf(tmp_path / "Qwen-27B-Q4.gguf", "qwen35", "model"),
        "lora": _gguf(tmp_path / "Qwen-27B-pi-lora-F16.gguf", "qwen35", "adapter"),
        "other_lora": _gguf(tmp_path / "Llama-lora.gguf", "llama", "adapter"),
        "mmproj": _gguf(tmp_path / "mmproj-Q8_0.gguf", "clip", "mmproj"),
        "old_mmproj": _gguf(tmp_path / "mmproj-old.gguf", "clip"),
        "untyped": _gguf(tmp_path / "old-model.gguf", "llama"),
    }


def test_gguf_kind(files):
    def ka(name):
        k = model_meta.gguf_kind(str(files[name]))
        return k["kind"], k["arch"]
    assert ka("base") == ("model", "qwen35")
    assert ka("lora") == ("adapter", "qwen35")
    assert ka("mmproj")[0] == "mmproj"
    assert ka("old_mmproj")[0] == "mmproj"
    assert ka("untyped") == ("model", "llama")


def test_gguf_kind_unreadable_defaults_to_model(tmp_path):
    bad = tmp_path / "broken.gguf"
    bad.write_bytes(b"nope")
    assert model_meta.gguf_kind(str(bad)) == {"kind": "model", "arch": None, "thinking": []}


def test_start_rejects_adapter_as_base(files):
    with pytest.raises(ValueError, match="adapter file, not a model"):
        asyncio.run(mgr.start(str(files["lora"])))


def test_start_rejects_projector_as_base(files):
    with pytest.raises(ValueError, match="mmproj file, not a model"):
        asyncio.run(mgr.start(str(files["mmproj"])))


def test_start_rejects_arch_mismatch(files):
    with pytest.raises(ValueError, match="is for llama models"):
        asyncio.run(mgr.start(str(files["base"]), adapter=str(files["other_lora"])))


def test_start_rejects_non_adapter_lora(files):
    with pytest.raises(ValueError, match="not a LoRA adapter"):
        asyncio.run(mgr.start(str(files["base"]), adapter=str(files["untyped"])))


def test_start_rejects_missing_adapter(files, tmp_path):
    with pytest.raises(FileNotFoundError, match="adapter"):
        asyncio.run(mgr.start(str(files["base"]), adapter=str(tmp_path / "missing.gguf")))


def test_spawn_passes_lora(monkeypatch, tmp_path, files):
    seen = {}

    class _Proc:
        pid = 4242

    def _popen(args, **_):
        seen["args"] = args
        return _Proc()

    monkeypatch.setattr(mgr, "APP_HOME", tmp_path)
    monkeypatch.setattr(mgr, "_LOG_FILE", tmp_path / "llama.log")
    monkeypatch.setattr(mgr.subprocess, "Popen", _popen)
    monkeypatch.setattr(mgr.config, "llama_bin", lambda: "/opt/bin/llama-server")

    assert mgr._spawn(str(files["base"]), 8192, 99, 8080, "llamacpp", str(files["lora"])) == 4242
    args = seen["args"]
    assert args[args.index("--lora") + 1] == str(files["lora"])

    mgr._spawn(str(files["base"]), 8192, 99, 8080, "llamacpp")
    assert "--lora" not in seen["args"]


def test_hub_scan_tags_kind(monkeypatch, tmp_path):
    snap = tmp_path / "models--acme--Qwen-GGUF" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    _gguf(snap / "Qwen-Q4.gguf", "qwen35", "model")
    _gguf(snap / "Qwen-lora-F16.gguf", "qwen35", "adapter")
    monkeypatch.setattr(hub, "hf_cache_dir", lambda: tmp_path)

    by_name = {i["filename"]: i for i in hub._scan_hf_cache()}
    assert by_name["Qwen-Q4.gguf"]["ggufKind"] == "model"
    assert by_name["Qwen-lora-F16.gguf"]["ggufKind"] == "adapter"
    assert by_name["Qwen-lora-F16.gguf"]["arch"] == "qwen35"
    # Evals / onboarding only offer things that can answer a chat
    assert by_name["Qwen-Q4.gguf"]["textGenerative"] is True
    assert by_name["Qwen-lora-F16.gguf"]["textGenerative"] is False


# --------------------------------------------------------------------------- #
# thinking level pinned through --chat-template-kwargs
# --------------------------------------------------------------------------- #

def test_thinking_options_from_template():
    assert model_meta.thinking_options(INKLING_TMPL) == ["off", "low", "medium", "high"]
    assert model_meta.thinking_options(QWEN_TMPL) == ["off", "low", "medium", "high"]
    assert model_meta.thinking_options("{{ enable_thinking }}") == ["off"]
    assert model_meta.thinking_options("{{ messages }}") == []


def test_thinking_kwargs_per_template(tmp_path):
    inkling = _gguf(tmp_path / "inkling.gguf", "inkling2_loop", "model", INKLING_TMPL)
    qwen = _gguf(tmp_path / "qwen.gguf", "qwen35", "model", QWEN_TMPL)
    plain = _gguf(tmp_path / "plain.gguf", "llama", "model", "{{ messages }}")
    # Inkling has no on/off switch: "off" is its lowest effort
    assert model_meta.gguf_thinking_kwargs(str(inkling), "off") == {"reasoning_effort": "none"}
    assert model_meta.gguf_thinking_kwargs(str(inkling), "low") == {"reasoning_effort": "low"}
    # Qwen: off flips enable_thinking; levels go to reasoning_effort
    assert model_meta.gguf_thinking_kwargs(str(qwen), "off") == {"enable_thinking": False}
    assert model_meta.gguf_thinking_kwargs(str(qwen), "high") == {"reasoning_effort": "high"}
    with pytest.raises(ValueError):
        model_meta.gguf_thinking_kwargs(str(plain), "low")


def test_start_rejects_unsupported_thinking(tmp_path):
    plain = _gguf(tmp_path / "plain.gguf", "llama", "model", "{{ messages }}")
    with pytest.raises(ValueError, match="isn't supported"):
        asyncio.run(mgr.start(str(plain), thinking="low"))


def test_spawn_passes_template_kwargs(monkeypatch, tmp_path, files):
    seen = {}

    class _Proc:
        pid = 1

    monkeypatch.setattr(mgr, "APP_HOME", tmp_path)
    monkeypatch.setattr(mgr, "_LOG_FILE", tmp_path / "llama.log")
    monkeypatch.setattr(mgr.subprocess, "Popen", lambda args, **_: seen.update(args=args) or _Proc())
    monkeypatch.setattr(mgr.config, "llama_bin", lambda: "/opt/bin/llama-server")
    mgr._spawn(str(files["base"]), 8192, 99, 8080, "llamacpp",
               template_kwargs={"enable_thinking": False})
    args = seen["args"]
    assert args[args.index("--chat-template-kwargs") + 1] == '{"enable_thinking": false}'


def test_status_explains_unknown_architecture():
    assert "newer llama.cpp" in mgr._explain("error: unknown model architecture: 'inkling2_loop'")
    assert mgr._explain("other failure") == "other failure"
    assert mgr._explain(None) is None


def test_pid_alive_false_for_exited_child():
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    for _ in range(100):  # becomes a zombie once it exits; must read as dead
        if not mgr._pid_alive(proc.pid):
            break
        import time
        time.sleep(0.05)
    assert mgr._pid_alive(proc.pid) is False


def test_start_route_maps_bad_requests_to_400(files):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from routes import runtime as runtime_routes
    app = FastAPI()
    app.include_router(runtime_routes.router, prefix="/api/runtime")
    r = TestClient(app).post("/api/runtime/start", json={"path": str(files["lora"])})
    assert r.status_code == 400 and "not a model" in r.json()["detail"]
