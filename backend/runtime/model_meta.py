"""Read a model's max context window (and the numbers needed to size its KV
cache), so activation can default to the largest context that *fits this machine*
instead of a blind 32K — capped at what the model actually supports.

Dependency-free:
  * GGUF (llama.cpp) — a small hand parser of the GGUF metadata header, reading
    ``<arch>.context_length`` (== llama.cpp's ``n_ctx_train``) plus the layer /
    head / embedding sizes. Big array values (the tokenizer vocab) are skipped, so
    it only touches the small scalar header.
  * MLX / HF — ``config.json`` (``max_position_embeddings`` + layer/head sizes).

``recommend_ctx`` turns the model's max + KV-per-token + weight size into a
memory-aware default using the existing RAM/VRAM probes. It's a heuristic that errs
LOW (the user can always raise it); the true max is always reported too.
"""

from __future__ import annotations

import json
import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

# GGUF scalar value-type → struct format (types 0-12; 8=string, 9=array).
_GGUF_SCALAR = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
    6: "<f", 7: "<B", 10: "<Q", 11: "<q", 12: "<d",
}
_GGUF_SCALAR_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

_KV_ELEM_BYTES = 2  # KV cache stored as f16 by default in llama.cpp


# --------------------------------------------------------------------------- #
# GGUF header parsing
# --------------------------------------------------------------------------- #

def _read_gguf_value(f, t: int):
    """Read one GGUF metadata value. Scalars/strings are returned; arrays are
    skipped in-place (we never need the huge vocab arrays) and return None."""
    if t in _GGUF_SCALAR:
        return struct.unpack(_GGUF_SCALAR[t], f.read(_GGUF_SCALAR_SIZE[t]))[0]
    if t == 8:  # string
        (ln,) = struct.unpack("<Q", f.read(8))
        return f.read(ln).decode("utf-8", "replace")
    if t == 9:  # array — skip its elements without materializing them
        (elem_t,) = struct.unpack("<I", f.read(4))
        (count,) = struct.unpack("<Q", f.read(8))
        if elem_t in _GGUF_SCALAR_SIZE:
            f.seek(_GGUF_SCALAR_SIZE[elem_t] * count, 1)
        elif elem_t == 8:
            for _ in range(count):
                (ln,) = struct.unpack("<Q", f.read(8))
                f.seek(ln, 1)
        else:  # nested arrays are not used by the keys we read
            for _ in range(count):
                _read_gguf_value(f, elem_t)
        return None
    raise ValueError(f"unknown GGUF value type {t}")


def read_gguf_meta(path: str) -> dict | None:
    """Parse a GGUF header → {maxCtx, nLayers, nKvHeads, headDim} (best-effort).
    Returns None on any error — callers treat missing metadata as 'unknown'."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            struct.unpack("<I", f.read(4))          # version
            struct.unpack("<Q", f.read(8))          # tensor count
            (n_kv,) = struct.unpack("<Q", f.read(8))
            kv: dict[str, object] = {}
            for _ in range(n_kv):
                (klen,) = struct.unpack("<Q", f.read(8))
                key = f.read(klen).decode("utf-8", "replace")
                (t,) = struct.unpack("<I", f.read(4))
                val = _read_gguf_value(f, t)
                if val is not None:                 # keep scalars/strings only
                    kv[key] = val
    except Exception as exc:  # noqa: BLE001 — never crash the catalog scan
        logger.debug("gguf parse failed for %s: %s", path, exc)
        return None

    arch = kv.get("general.architecture")
    if not isinstance(arch, str):
        return None

    def g(suffix: str):
        return kv.get(f"{arch}.{suffix}")

    max_ctx = g("context_length")
    if not isinstance(max_ctx, int) or max_ctx <= 0:
        return None
    n_layers = g("block_count")
    n_heads = g("attention.head_count")
    n_kv_heads = g("attention.head_count_kv") or n_heads
    embed = g("embedding_length")
    key_len = g("attention.key_length")
    head_dim = key_len or (int(embed) // int(n_heads) if embed and n_heads else None)
    # A native MTP (multi-token-prediction / "nextn") head is baked in when the
    # arch declares nextn_predict_layers > 0. llama.cpp can self-speculate off it
    # (--spec-type draft-mtp) for a throughput win with identical output.
    nextn = g("nextn_predict_layers")
    return {
        "maxCtx": int(max_ctx),
        "nLayers": int(n_layers) if n_layers else None,
        "nKvHeads": int(n_kv_heads) if n_kv_heads else None,
        "headDim": int(head_dim) if head_dim else None,
        "mtpCapable": bool(isinstance(nextn, int) and nextn > 0),
    }


# --------------------------------------------------------------------------- #
# HF / MLX config.json
# --------------------------------------------------------------------------- #

def read_hf_meta(config_json: str | Path) -> dict | None:
    try:
        cfg = json.loads(Path(config_json).read_text())
    except Exception:  # noqa: BLE001
        return None
    # multimodal configs nest the text model under text_config
    text = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else cfg
    max_ctx = text.get("max_position_embeddings") or cfg.get("max_position_embeddings")
    if not isinstance(max_ctx, int) or max_ctx <= 0:
        return None
    n_layers = text.get("num_hidden_layers")
    n_heads = text.get("num_attention_heads")
    n_kv_heads = text.get("num_key_value_heads") or n_heads
    hidden = text.get("hidden_size")
    head_dim = text.get("head_dim") or (int(hidden) // int(n_heads) if hidden and n_heads else None)
    return {
        "maxCtx": int(max_ctx),
        "nLayers": int(n_layers) if n_layers else None,
        "nKvHeads": int(n_kv_heads) if n_kv_heads else None,
        "headDim": int(head_dim) if head_dim else None,
    }


# --------------------------------------------------------------------------- #
# KV-cache sizing + memory-aware recommendation
# --------------------------------------------------------------------------- #

def kv_bytes_per_token(meta: dict) -> int | None:
    """Bytes of KV cache per token: 2 (K+V) · layers · kv_heads · head_dim · f16."""
    n_layers, n_kv_heads, head_dim = meta.get("nLayers"), meta.get("nKvHeads"), meta.get("headDim")
    if not (n_layers and n_kv_heads and head_dim):
        return None
    return 2 * n_layers * n_kv_heads * head_dim * _KV_ELEM_BYTES


def _memory_budget_bytes() -> int | None:
    """Usable memory for weights + KV. Reuses the Monitor/Hub probes. On Apple
    Silicon (unified memory) the pool is total RAM; on an NVIDIA box it's VRAM."""
    try:
        from routes import hub, monitor
        vram, _ = hub._vram_bytes()
        total = hub._total_ram_bytes()
        pool = vram or total
        avail = monitor._available_ram_bytes()
        if pool is None:
            return avail
        # Prefer the smaller of "currently available" and the pool; if we can't
        # read availability, assume ~70% of the pool is free.
        return min(pool, avail) if avail else int(pool * 0.7)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory budget probe failed: %s", exc)
        return None


def recommend_ctx(max_ctx: int, kv_per_tok: int | None,
                  model_size_bytes: int | None) -> int:
    """Largest context that likely fits this machine, capped at the model's max.
    Rounded down to a 4096 multiple; never below 2048. Falls back to min(max, 32K)
    when we can't size the KV cache or read memory."""
    ceiling = min(max_ctx, 32_768)                       # safe fallback ceiling
    budget = _memory_budget_bytes()
    if not kv_per_tok or not budget:
        return max(2048, min(max_ctx, ceiling))
    usable = budget * 0.8 - (model_size_bytes or 0)      # leave headroom for weights+overhead
    if usable <= 0:
        return max(2048, min(max_ctx, 4096))
    fit = int(usable // kv_per_tok)
    ctx = min(max_ctx, fit)
    ctx = (ctx // 4096) * 4096                           # round down to a clean step
    return max(2048, min(ctx, max_ctx))


# --------------------------------------------------------------------------- #
# Public convenience — used by the catalog scan (memoized by path+size)
# --------------------------------------------------------------------------- #

_cache: dict[tuple[str, int], dict] = {}


def ctx_info(path: str, backend: str, size_bytes: int | None) -> dict:
    """{maxCtx, recommendedCtx} for a model row (best-effort; values may be None).
    Memoized on (path, size) — the GGUF/config read is skipped on repeat polls."""
    key = (path, size_bytes or 0)
    if key in _cache:
        return _cache[key]
    meta = None
    try:
        if backend == "llamacpp":
            meta = read_gguf_meta(path)
        else:  # mlx / hf snapshot directory
            meta = read_hf_meta(Path(path) / "config.json")
    except Exception as exc:  # noqa: BLE001
        logger.debug("ctx_info failed for %s: %s", path, exc)
    if not meta:
        info = {"maxCtx": None, "recommendedCtx": None, "mtpCapable": False}
    else:
        max_ctx = meta["maxCtx"]
        rec = recommend_ctx(max_ctx, kv_bytes_per_token(meta), size_bytes)
        info = {"maxCtx": max_ctx, "recommendedCtx": rec,
                "mtpCapable": bool(meta.get("mtpCapable"))}
    _cache[key] = info
    return info
