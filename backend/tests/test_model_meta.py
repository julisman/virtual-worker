"""GGUF/HF metadata reading + memory-aware ctx recommendation (hermetic — a tiny
synthetic GGUF is built in-memory; no real model file is needed)."""

import json
import struct

import pytest

from runtime import model_meta


# --------------------------------------------------------------------------- #
# Build a minimal but valid GGUF header
# --------------------------------------------------------------------------- #

def _kv_str(key: str, val: str) -> bytes:
    b = struct.pack("<Q", len(key)) + key.encode()
    b += struct.pack("<I", 8)  # type 8 = string
    b += struct.pack("<Q", len(val)) + val.encode()
    return b


def _kv_u32(key: str, val: int) -> bytes:
    b = struct.pack("<Q", len(key)) + key.encode()
    b += struct.pack("<I", 4)  # type 4 = uint32
    b += struct.pack("<I", val)
    return b


def _kv_str_array(key: str, vals: list[str]) -> bytes:
    b = struct.pack("<Q", len(key)) + key.encode()
    b += struct.pack("<I", 9)              # type 9 = array
    b += struct.pack("<I", 8)              # element type = string
    b += struct.pack("<Q", len(vals))
    for v in vals:
        b += struct.pack("<Q", len(v)) + v.encode()
    return b


def _build_gguf(arch="llama", ctx=8192, layers=32, heads=32, kv_heads=8, embed=4096,
                nextn=None) -> bytes:
    kvs = [
        _kv_str("general.architecture", arch),
        # an array in the middle to prove array-skipping works
        _kv_str_array("tokenizer.ggml.tokens", ["<s>", "</s>", "hello", "world"]),
        _kv_u32(f"{arch}.context_length", ctx),
        _kv_u32(f"{arch}.block_count", layers),
        _kv_u32(f"{arch}.attention.head_count", heads),
        _kv_u32(f"{arch}.attention.head_count_kv", kv_heads),
        _kv_u32(f"{arch}.embedding_length", embed),
    ]
    if nextn is not None:
        kvs.append(_kv_u32(f"{arch}.nextn_predict_layers", nextn))
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs))
    return header + b"".join(kvs)


@pytest.fixture
def gguf_file(tmp_path):
    p = tmp_path / "tiny.gguf"
    p.write_bytes(_build_gguf())
    return str(p)


# --------------------------------------------------------------------------- #
# GGUF parsing
# --------------------------------------------------------------------------- #

def test_read_gguf_meta_reads_ctx_and_shapes(gguf_file):
    meta = model_meta.read_gguf_meta(gguf_file)
    assert meta["maxCtx"] == 8192
    assert meta["nLayers"] == 32
    assert meta["nKvHeads"] == 8
    assert meta["headDim"] == 4096 // 32  # embed / head_count
    assert meta["mtpCapable"] is False    # no nextn head in the default fixture


def test_mtp_capable_detected_from_nextn_layers(tmp_path):
    p = tmp_path / "mtp.gguf"
    p.write_bytes(_build_gguf(arch="qwen35", nextn=1))
    assert model_meta.read_gguf_meta(str(p))["mtpCapable"] is True
    # ctx_info surfaces the same flag for the hub listing
    assert model_meta.ctx_info(str(p), "llamacpp", None)["mtpCapable"] is True


def test_read_gguf_meta_bad_file_returns_none(tmp_path):
    p = tmp_path / "not.gguf"
    p.write_bytes(b"NOPE" + b"\x00" * 32)
    assert model_meta.read_gguf_meta(str(p)) is None


def test_kv_bytes_per_token(gguf_file):
    meta = model_meta.read_gguf_meta(gguf_file)
    # 2 (K+V) * 32 layers * 8 kv-heads * 128 head_dim * 2 bytes
    assert model_meta.kv_bytes_per_token(meta) == 2 * 32 * 8 * 128 * 2


# --------------------------------------------------------------------------- #
# HF / MLX config.json
# --------------------------------------------------------------------------- #

def test_read_hf_meta(tmp_path):
    cfg = {"max_position_embeddings": 32768, "num_hidden_layers": 28,
           "num_attention_heads": 16, "num_key_value_heads": 4, "hidden_size": 2048}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    meta = model_meta.read_hf_meta(p)
    assert meta["maxCtx"] == 32768
    assert meta["nKvHeads"] == 4
    assert meta["headDim"] == 2048 // 16


# --------------------------------------------------------------------------- #
# Memory-aware recommendation
# --------------------------------------------------------------------------- #

def test_recommend_ctx_caps_when_memory_tight(monkeypatch):
    # 1M-ctx model, KV = 128 KiB/token, only ~4 GiB usable → must cap well below max
    monkeypatch.setattr(model_meta, "_memory_budget_bytes", lambda: 4 * 1024**3)
    kv = 128 * 1024
    ctx = model_meta.recommend_ctx(1_048_576, kv, model_size_bytes=2 * 1024**3)
    assert ctx < 1_048_576
    assert ctx % 4096 == 0
    assert ctx >= 2048


def test_recommend_ctx_uses_max_when_memory_ample(monkeypatch):
    monkeypatch.setattr(model_meta, "_memory_budget_bytes", lambda: 512 * 1024**3)
    ctx = model_meta.recommend_ctx(8192, 128 * 1024, model_size_bytes=2 * 1024**3)
    assert ctx == 8192  # plenty of room → the model's own max


def test_recommend_ctx_fallback_without_memory(monkeypatch):
    monkeypatch.setattr(model_meta, "_memory_budget_bytes", lambda: None)
    # no memory reading → safe fallback min(max, 32K)
    assert model_meta.recommend_ctx(131072, 1024, 0) == 32768
    assert model_meta.recommend_ctx(4096, 1024, 0) == 4096


def test_ctx_info_memoized(monkeypatch, gguf_file):
    model_meta._cache.clear()
    monkeypatch.setattr(model_meta, "_memory_budget_bytes", lambda: 64 * 1024**3)
    calls = {"n": 0}
    real = model_meta.read_gguf_meta

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(model_meta, "read_gguf_meta", counting)
    a = model_meta.ctx_info(gguf_file, "llamacpp", 1000)
    b = model_meta.ctx_info(gguf_file, "llamacpp", 1000)
    assert a == b and a["maxCtx"] == 8192
    assert calls["n"] == 1  # second call served from cache
