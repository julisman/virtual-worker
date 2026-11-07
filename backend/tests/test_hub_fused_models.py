"""Fused fine-tune outputs (APP_HOME/models) must appear in the on-device list."""

import json

import config
from routes.hub import _scan_fused_models


def _mk_fused(base, name):
    d = base / "models" / name
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({
        "architectures": ["Qwen3_5ForConditionalGeneration"],
        "model_type": "qwen3_5",
        "quantization": {"bits": 6},
    }))
    (d / "model.safetensors").write_bytes(b"x" * 2048)
    return d


def test_scan_finds_fused_models(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _mk_fused(tmp_path, "Qwythos-ft-abc123")
    items = _scan_fused_models()
    assert len(items) == 1
    m = items[0]
    assert m["source"] == "finetuned" and m["backend"] == "mlx"
    assert m["repo"] == "local/Qwythos-ft-abc123"   # local/ author namespace
    assert m["path"].endswith("Qwythos-ft-abc123")
    assert m["sizeBytes"] == 2048


def test_scan_skips_incomplete_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    # no config.json → not a servable model → skipped
    d = tmp_path / "models" / "half-baked"
    d.mkdir(parents=True)
    (d / "model.safetensors").write_bytes(b"x")
    # empty dir → skipped
    (tmp_path / "models" / "empty").mkdir()
    assert _scan_fused_models() == []


def test_scan_missing_dir_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path / "nope")
    assert _scan_fused_models() == []


def test_scan_finds_loose_gguf(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    base = tmp_path / "models"
    base.mkdir()
    (base / "nano-LocalFT-1B-v2-F16.gguf").write_bytes(b"g" * 4096)
    (base / "notes.txt").write_text("not a model")  # ignored
    items = _scan_fused_models()
    assert len(items) == 1
    m = items[0]
    assert m["source"] == "finetuned" and m["backend"] == "llamacpp"
    assert m["repo"] == "local/nano-LocalFT-1B-v2-F16"
    assert m["filename"] == "nano-LocalFT-1B-v2-F16.gguf"
    assert m["sizeBytes"] == 4096


def test_scan_lists_gguf_next_to_fused_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    _mk_fused(tmp_path, "MiniCPM5-ft-abc")
    (tmp_path / "models" / "nano-v2.gguf").write_bytes(b"g" * 1024)
    items = _scan_fused_models()
    assert {m["backend"] for m in items} == {"mlx", "llamacpp"}
    assert len(items) == 2
