# Model Hub — discover, inspect, download, and run models locally.

"""The Hub backend.

Mirrors the Unsloth "Hub" experience: discover models (HuggingFace, incl. the
Unsloth org), inspect their GGUF files, check whether they fit the local
machine, download them into the real HuggingFace cache (with live progress and
cancel), and list what's already on the device (HF cache + Ollama).

  GET  /api/hub/capability                  -> local hardware (RAM/VRAM/os)
  GET  /api/hub/discover?...                 -> search HF models (GGUF)
  GET  /api/hub/model/{owner}/{name}         -> model detail + GGUF files
  GET  /api/hub/local                        -> models on device (HF cache + Ollama)
  POST /api/hub/download                     -> start a download (HF file or Ollama pull)
  GET  /api/hub/downloads                    -> in-flight + finished download jobs
  POST /api/hub/downloads/{job_id}/cancel    -> cancel a running download

NOTE: no auth yet — localhost only. Add an auth dependency before exposing it.
Gated/private HF repos need a token: set HF_TOKEN (or HUGGING_FACE_HUB_TOKEN).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from runtime import model_meta

logger = logging.getLogger(__name__)

router = APIRouter()

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
HF_TOKEN = (
    os.environ.get("HF_TOKEN")
    or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    or os.environ.get("HUGGINGFACE_TOKEN")
)

GIB = 1024**3

# Parallel download tuning: split large files into chunks fetched over N
# connections (like hf_transfer). Tune via env. 8 is a good balance — more
# connections raise throughput but also the rate of CDN-dropped sockets.
_DL_CONNECTIONS = max(1, int(os.environ.get("VW_DL_CONNECTIONS", "8")))
_DL_CHUNK = max(1, int(os.environ.get("VW_DL_CHUNK_MB", "16"))) * 1024 * 1024
# Per-chunk retries on transient network failures (the HF CDN drops sockets
# mid-transfer; without retry a single drop fails the whole multi-GB download).
_DL_RETRIES = max(0, int(os.environ.get("VW_DL_RETRIES", "5")))
_RETRY_STATUS = {429, 500, 502, 503, 504}


class _Retryable(Exception):
    """A transient HTTP status worth retrying (rate-limit / 5xx)."""


# httpx.TransportError covers RemoteProtocolError ("peer closed connection…"),
# ConnectError/ReadError, and timeouts — all transient.
_TRANSIENT = (httpx.TransportError, _Retryable)


def _dl_backoff(attempt: int) -> float:
    return min(2 ** attempt, 8.0)  # 1, 2, 4, 8, 8 … seconds


# --------------------------------------------------------------------------- #
# HuggingFace cache layout helpers
#   ~/.cache/huggingface/hub/models--{owner}--{name}/
#       blobs/{etag}            (the file content)
#       snapshots/{commit}/{f}  (symlink -> ../../blobs/{etag})
#       refs/{revision}         (text file holding the commit sha)
# Writing this layout ourselves keeps the file in the *real* HF cache, so
# llama.cpp / huggingface_hub / On-Device all see it, while we keep full control
# over progress + cancel.
# --------------------------------------------------------------------------- #

def hf_cache_dir() -> Path:
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub:
        return Path(hub)
    home = os.environ.get("HF_HOME")
    base = Path(home) if home else Path.home() / ".cache" / "huggingface"
    return base / "hub"


def _repo_cache_dir(repo: str) -> Path:
    owner, name = repo.split("/", 1)
    return hf_cache_dir() / f"models--{owner}--{name}"


def _hf_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}


# --------------------------------------------------------------------------- #
# Hardware capability
# --------------------------------------------------------------------------- #

def _total_ram_bytes() -> int | None:
    try:  # unix (macOS + Linux) — portable, no third-party deps
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def _vram_bytes() -> tuple[int | None, str]:
    """Best-effort VRAM. Returns (bytes_or_None, note)."""
    # NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4,
            ).stdout.strip().splitlines()
            mb = sum(int(x) for x in out if x.strip().isdigit())
            if mb:
                return mb * 1024 * 1024, "nvidia"
        except Exception:  # pragma: no cover - hardware dependent
            pass
    # Apple Silicon — unified memory (GPU shares system RAM)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        ram = _total_ram_bytes()
        if ram:
            return ram, "unified"
    return None, "unknown"


def _disk_usage():
    """Free/total bytes on the volume holding the model cache (downloads land here)."""
    p = hf_cache_dir()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        du = shutil.disk_usage(p)
        return du.free, du.total
    except OSError:
        return None, None


def _cpu_info() -> tuple[int | None, str | None]:
    cores = os.cpu_count()
    brand = None
    try:
        if platform.system() == "Darwin":
            brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                   capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        pass
    return cores, (brand or platform.processor() or None)


@router.get("/capability")
async def capability():
    # These probe hardware via blocking subprocesses (nvidia-smi, sysctl) and
    # disk stat — run them off the event loop so the endpoint doesn't stall other
    # requests while it shells out.
    def _probe():
        ram = _total_ram_bytes()
        vram, vram_note = _vram_bytes()
        disk_free, disk_total = _disk_usage()
        cpu_cores, cpu_brand = _cpu_info()
        return ram, vram, vram_note, disk_free, disk_total, cpu_cores, cpu_brand

    ram, vram, vram_note, disk_free, disk_total, cpu_cores, cpu_brand = \
        await asyncio.to_thread(_probe)
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "cpuCores": cpu_cores,
        "cpuBrand": cpu_brand,
        "cpuRamBytes": ram,
        "cpuRamGb": round(ram / GIB, 1) if ram else None,
        "vramBytes": vram,
        "vramGb": round(vram / GIB, 1) if vram else None,
        "vramKind": vram_note,  # "unified" | "nvidia" | "unknown"
        "diskFreeBytes": disk_free,
        "diskFreeGb": round(disk_free / GIB, 1) if disk_free else None,
        "diskTotalGb": round(disk_total / GIB, 1) if disk_total else None,
        "hfTokenPresent": bool(HF_TOKEN),
        "hfCacheDir": str(hf_cache_dir()),
    }


def _fit_for(size_bytes: int | None) -> str:
    """Rough "does this fit?" verdict against usable memory."""
    if not size_bytes:
        return "unknown"
    vram, _ = _vram_bytes()
    budget = vram or _total_ram_bytes()
    if not budget:
        return "unknown"
    # Need headroom for the KV cache + runtime (~25%).
    need = size_bytes * 1.25
    if need <= budget * 0.85:
        return "fits"
    if size_bytes <= budget:
        return "tight"
    return "too_large"


_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB]\b")


def _param_billions(model_id: str) -> float | None:
    """Largest 'NNB' param count parsed from a model id, e.g. gemma-4-26B-A4B ->
    26 (total params drive memory, not the MoE active count)."""
    nums = [float(m) for m in _PARAM_RE.findall(model_id.split("/")[-1])]
    return max(nums) if nums else None


def _repo_fits(model_id: str) -> bool | None:
    """Heuristic: can ANY quant of this repo run locally? Estimates the smallest
    practical footprint (~0.6 GB per billion params at 4-bit + 25% headroom)
    against usable memory. None = unknown (no param count in the name)."""
    params = _param_billions(model_id)
    if params is None:
        return None
    budget = (_vram_bytes()[0] or _total_ram_bytes())
    if not budget:
        return None
    min_bytes = params * 0.6 * GIB * 1.25
    return min_bytes <= budget


# --------------------------------------------------------------------------- #
# Discover (HuggingFace search)
# --------------------------------------------------------------------------- #

_SORT_MAP = {
    "newest": "lastModified",
    "downloads": "downloads",
    "likes": "likes",
    "trending": "trendingScore",
}


@router.get("/discover")
async def discover(
    q: str = "",
    sort: str = "newest",
    author: str = "",
    capability: str = "",  # pipeline_tag filter, e.g. text-generation
    fmt: str = "gguf",      # "gguf" (llama.cpp) | "mlx" (Apple Silicon)
    fit_only: bool = True,  # hide models too big for this machine
    limit: int = 40,
):
    params: dict[str, object] = {
        "filter": "mlx" if fmt == "mlx" else "gguf",
        "sort": _SORT_MAP.get(sort, "lastModified"),
        "direction": -1,
        "limit": max(1, min(limit, 100)),
        "full": "true",
    }
    if q:
        params["search"] = q
    if author:
        params["author"] = author
    if capability:
        params["pipeline_tag"] = capability

    try:
        async with httpx.AsyncClient(timeout=20, headers=_hf_headers()) as client:
            resp = await client.get(f"{HF_ENDPOINT}/api/models", params=params)
            resp.raise_for_status()
            raw = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"HuggingFace unreachable: {exc}") from exc

    models = []
    for m in raw:
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        fits = _repo_fits(mid)
        # When fit_only, drop models that clearly don't fit (keep unknowns).
        if fit_only and fits is False:
            continue
        models.append({
            "id": mid,
            "author": mid.split("/")[0],
            "likes": m.get("likes", 0),
            "downloads": m.get("downloads", 0),
            "lastModified": m.get("lastModified") or m.get("modifiedAt"),
            "pipelineTag": m.get("pipeline_tag"),
            "tags": [t for t in (m.get("tags") or []) if not t.startswith("base_model:")][:8],
            "gated": bool(m.get("gated")),
            "paramsB": _param_billions(mid),
            "fits": fits,
        })
    return {"models": models}


# Quant tags: legacy/k-quants/i-quants (incl. Unsloth Dynamic "UD-" + _XL/_XS mixes).
_QUANT_RE = re.compile(r"(UD-)?(IQ\d[A-Z0-9_]*|Q\d+(_[A-Z0-9]+)*|BF16|F16|F32)", re.IGNORECASE)


def _quant_label(filename: str) -> str | None:
    m = _QUANT_RE.search(Path(filename).stem)
    return m.group(0).upper() if m else None


@router.get("/model/{owner}/{name}")
async def model_detail(owner: str, name: str):
    repo = f"{owner}/{name}"
    try:
        async with httpx.AsyncClient(timeout=20, headers=_hf_headers()) as client:
            info_r, tree_r = await asyncio.gather(
                client.get(f"{HF_ENDPOINT}/api/models/{repo}"),
                client.get(f"{HF_ENDPOINT}/api/models/{repo}/tree/main", params={"recursive": "true"}),
            )
            info_r.raise_for_status()
            info = info_r.json()
            tree = tree_r.json() if tree_r.status_code == 200 else []
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"HuggingFace unreachable: {exc}") from exc

    cached = _cached_filenames(repo)
    files = []
    for node in tree:
        path = node.get("path", "")
        if node.get("type") != "file" or not path.lower().endswith(".gguf"):
            continue
        size = node.get("size") or (node.get("lfs") or {}).get("size")
        files.append({
            "filename": path,
            "sizeBytes": size,
            "sizeGb": round(size / GIB, 2) if size else None,
            "quant": _quant_label(path),
            "fit": _fit_for(size),
            "cached": path in cached,
        })
    files.sort(key=lambda f: f["sizeBytes"] or 0)

    tags = info.get("tags") or []
    is_mlx = "mlx" in tags and not files  # MLX repo (no GGUF, weights are safetensors)
    # whole-repo size (used for MLX, which downloads as a directory, not one file)
    repo_size = sum((n.get("size") or (n.get("lfs") or {}).get("size") or 0)
                    for n in tree if n.get("type") == "file" and not n.get("path", "").endswith(".gguf"))

    card = info.get("cardData") or {}
    base = next((t.split(":", 1)[1] for t in tags if t.startswith("base_model:")), None)
    return {
        "id": repo,
        "author": owner,
        "downloads": info.get("downloads", 0),
        "likes": info.get("likes", 0),
        "lastModified": info.get("lastModified"),
        "pipelineTag": info.get("pipeline_tag"),
        "license": card.get("license") or next(
            (t.split(":", 1)[1] for t in tags if t.startswith("license:")), None),
        "tags": [t for t in tags if ":" not in t][:12],
        "baseModel": base,
        "gated": bool(info.get("gated")),
        "files": files,
        "mlx": is_mlx,
        "repoSizeBytes": repo_size or None,
        "repoSizeGb": round(repo_size / GIB, 2) if repo_size else None,
        "repoCached": _mlx_repo_cached(repo) if is_mlx else False,
        "repoFit": _fit_for(repo_size) if is_mlx else "unknown",
    }


# --------------------------------------------------------------------------- #
# On-device inventory (HF cache + Ollama)
# --------------------------------------------------------------------------- #

def _cached_filenames(repo: str) -> set[str]:
    """GGUF filenames already present in the HF cache for a given repo."""
    snaps = _repo_cache_dir(repo) / "snapshots"
    out: set[str] = set()
    if snaps.is_dir():
        for rev in snaps.iterdir():
            for f in rev.rglob("*.gguf"):
                out.add(str(f.relative_to(rev)))
    return out


def _scan_hf_cache() -> list[dict]:
    base = hf_cache_dir()
    if not base.is_dir():
        return []
    items: list[dict] = []
    for repo_dir in base.glob("models--*"):
        repo = repo_dir.name.replace("models--", "").replace("--", "/", 1)
        snaps = repo_dir / "snapshots"
        if not snaps.is_dir():
            continue
        for rev in snaps.iterdir():
            for f in rev.rglob("*.gguf"):
                try:
                    size = f.stat().st_size  # follows symlink to the blob
                except OSError:
                    size = None
                ctx = model_meta.ctx_info(str(f), "llamacpp", size)
                items.append({
                    "source": "hf-cache",
                    "backend": "llamacpp",
                    "repo": repo,
                    "filename": str(f.relative_to(rev)),
                    "path": str(f),
                    "sizeBytes": size,
                    "sizeGb": round(size / GIB, 2) if size else None,
                    "quant": _quant_label(f.name),
                    "maxCtx": ctx["maxCtx"],
                    "recommendedCtx": ctx["recommendedCtx"],
                    "mtpCapable": ctx.get("mtpCapable", False),
                })
    return items


def _mlx_meta(rev: Path) -> tuple[str | None, bool, bool]:
    """(quant label, is_multimodal, is_text_generative) from an MLX model's
    config.json. Multimodal (vision/audio) MLX models need mlx-vlm — mlx_lm.server
    (text) can't run them. text_generative is True for chat/instruct LMs, which
    answer a text prompt regardless of any vision/audio *input* head; it's False
    only for dedicated audio (TTS/STT) or image-generation models, which can't
    run a text eval suite. Modern chat VLMs (Qwen3.5, Gemma-4) declare a
    vision_config yet are perfectly text-generative."""
    try:
        cfg = json.loads((rev / "config.json").read_text())
    except Exception:
        return None, False, False
    q = cfg.get("quantization") or {}
    quant = f"{q['bits']}-bit" if q.get("bits") else None
    multimodal = ("vision_config" in cfg) or ("audio_config" in cfg)
    archs = cfg.get("architectures") or []
    is_lm_arch = any(
        isinstance(a, str) and (a.endswith("ForCausalLM") or a.endswith("ForConditionalGeneration"))
        for a in archs
    )
    # A plain text model has no vision/audio head; a chat VLM is an LM-arch with a
    # text_config. Either way it answers text — only pure audio/image models don't.
    text_generative = (not multimodal) or is_lm_arch or ("text_config" in cfg)
    return quant, multimodal, text_generative


def _mlx_repo_cached(repo: str) -> bool:
    snaps = _repo_cache_dir(repo) / "snapshots"
    if not snaps.is_dir():
        return False
    return any((rev / "config.json").exists() and any(rev.glob("*.safetensors"))
               for rev in snaps.iterdir())


def _scan_mlx_cache() -> list[dict]:
    """MLX models in the cache are directories (config.json + *.safetensors)."""
    base = hf_cache_dir()
    if not base.is_dir():
        return []
    items: list[dict] = []
    for repo_dir in base.glob("models--*"):
        repo = repo_dir.name.replace("models--", "").replace("--", "/", 1)
        snaps = repo_dir / "snapshots"
        if not snaps.is_dir():
            continue
        for rev in snaps.iterdir():
            safes = list(rev.glob("*.safetensors"))
            if not (rev / "config.json").exists() or not safes:
                continue
            size = 0
            for f in safes:
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
            quant, multimodal, text_generative = _mlx_meta(rev)
            ctx = model_meta.ctx_info(str(rev), "mlx", size or None)
            items.append({
                "source": "hf-cache",
                "backend": "mlx",
                "repo": repo,
                "filename": repo.split("/")[-1],
                "path": str(rev),  # the snapshot directory
                "sizeBytes": size or None,
                "sizeGb": round(size / GIB, 2) if size else None,
                "quant": quant,
                "multimodal": multimodal,
                "textGenerative": text_generative,
                "maxCtx": ctx["maxCtx"],
                "recommendedCtx": ctx["recommendedCtx"],
            })
    return items


def _scan_fused_models() -> list[dict]:
    """Fine-tuned models fused by the Train tab live under ``APP_HOME/models``
    (one MLX model directory each) — not in the HF cache, so the cache scanners
    miss them. Surface them in the on-device list with source "finetuned".
    Loose ``*.gguf`` files there (fuses converted for llama.cpp serving) are
    listed too, same as HF-cache GGUFs."""
    from config import APP_HOME
    base = APP_HOME / "models"
    if not base.is_dir():
        return []
    items: list[dict] = []
    for d in sorted(base.iterdir()):
        if d.is_file() and d.suffix.lower() == ".gguf":
            try:
                size = d.stat().st_size
            except OSError:
                size = None
            ctx = model_meta.ctx_info(str(d), "llamacpp", size)
            items.append({
                "source": "finetuned",
                "backend": "llamacpp",
                "repo": f"local/{d.stem}",
                "filename": d.name,
                "path": str(d),
                "sizeBytes": size,
                "sizeGb": round(size / GIB, 2) if size else None,
                "quant": _quant_label(d.name),
                "maxCtx": ctx["maxCtx"],
                "recommendedCtx": ctx["recommendedCtx"],
                "mtpCapable": ctx.get("mtpCapable", False),
            })
            continue
        safes = list(d.glob("*.safetensors")) if d.is_dir() else []
        if not safes or not (d / "config.json").exists():
            continue
        size = 0
        for f in safes:
            try:
                size += f.stat().st_size
            except OSError:
                pass
        quant, multimodal, text_generative = _mlx_meta(d)
        ctx = model_meta.ctx_info(str(d), "mlx", size or None)
        items.append({
            "source": "finetuned",
            "backend": "mlx",
            # Locally fine-tuned models live under the "local/" author namespace —
            # they were produced by this studio, not downloaded from a HF org.
            "repo": f"local/{d.name}",
            "filename": d.name,
            "path": str(d),
            "sizeBytes": size or None,
            "sizeGb": round(size / GIB, 2) if size else None,
            "quant": quant,
            "multimodal": multimodal,
            "textGenerative": text_generative,
            "maxCtx": ctx["maxCtx"],
            "recommendedCtx": ctx["recommendedCtx"],
        })
    return items


@router.get("/local")
async def local_models():
    # Scanning the HF + MLX caches walks the whole cache tree (blocking I/O) —
    # run it off the event loop.
    items = await asyncio.to_thread(
        lambda: _scan_hf_cache() + _scan_mlx_cache() + _scan_fused_models())
    return {"models": items, "count": len(items)}


class DeleteRequest(BaseModel):
    path: str  # a cached .gguf file (llama.cpp) OR an MLX snapshot directory


@router.post("/delete")
async def delete_local(req: DeleteRequest):
    """Delete a downloaded model to free disk. Removes snapshot symlinks AND the
    underlying blobs (where the bytes actually live). Works for a single GGUF
    file or a whole MLX model directory."""
    path = Path(req.path)
    cache = str(hf_cache_dir())
    cache_r = hf_cache_dir().resolve()
    # Deletable roots: the HF cache, and APP_HOME/models (fused fine-tunes).
    from config import APP_HOME
    fused_root = APP_HOME / "models"
    in_cache = str(path).startswith(cache)
    in_fused = str(path.resolve()).startswith(str(fused_root.resolve()) + os.sep)
    is_gguf = path.suffix.lower() == ".gguf"
    if not (in_cache or in_fused) or not (is_gguf or path.is_dir()):
        raise HTTPException(400, "path is not a cached model")

    # Don't pull the rug out from under a running model.
    from runtime import llama_manager
    st = await llama_manager.status()
    if st.get("running") and st.get("modelPath") == str(path):
        raise HTTPException(409, "that model is currently active — stop or switch it first")

    if not path.exists() and not path.is_symlink():
        raise HTTPException(404, "model not found")

    freed = 0

    def _rm(p: Path):
        nonlocal freed
        if p.is_symlink():
            blob = p.resolve()  # real bytes in blobs/<etag>
            if blob.exists() and cache_r in blob.parents:
                freed += blob.stat().st_size
                blob.unlink()
            p.unlink()
        elif p.is_file():
            freed += p.stat().st_size
            p.unlink()

    try:
        if path.is_dir():  # MLX model directory — remove all its files + blobs
            for f in path.rglob("*"):
                if f.is_symlink() or f.is_file():
                    _rm(f)
            shutil.rmtree(path, ignore_errors=True)
        else:
            _rm(path)
    except OSError as exc:
        raise HTTPException(500, f"delete failed: {exc}") from exc

    return {"deleted": str(path), "freedBytes": freed, "freedGb": round(freed / GIB, 2)}


# --------------------------------------------------------------------------- #
# Downloads — in-memory job registry, background tasks, progress + cancel
# --------------------------------------------------------------------------- #

class DownloadJob:
    __slots__ = ("id", "source", "repo", "filename", "total", "received",
                 "status", "error", "started", "task")

    def __init__(self, job_id: str, source: str, repo: str, filename: str | None):
        self.id = job_id
        self.source = source
        self.repo = repo
        self.filename = filename
        self.total: int | None = None
        self.received = 0
        self.status = "starting"  # starting|downloading|done|error|cancelled
        self.error: str | None = None
        self.started = time.time()
        self.task: asyncio.Task | None = None

    def public(self) -> dict:
        pct = round(self.received / self.total * 100, 1) if self.total else None
        return {
            "id": self.id, "source": self.source, "repo": self.repo,
            "filename": self.filename, "status": self.status, "error": self.error,
            "totalBytes": self.total, "receivedBytes": self.received,
            "percent": pct, "startedAt": self.started,
        }


_JOBS: dict[str, DownloadJob] = {}
_JOB_SEQ = 0


def evict_finished_jobs(keep: int = 20) -> int:
    """Drop all but the ``keep`` most-recent terminal (done/error/cancelled)
    download jobs from the in-memory map so it doesn't grow unbounded on a
    long-running backend. In-flight jobs are never evicted. Returns count removed."""
    terminal = sorted(
        ((jid, j) for jid, j in _JOBS.items()
         if j.status in ("done", "error", "cancelled")),
        key=lambda kv: kv[1].started, reverse=True)
    removed = 0
    for jid, _ in terminal[keep:]:
        _JOBS.pop(jid, None)
        removed += 1
    return removed


def _new_job_id() -> str:
    global _JOB_SEQ
    _JOB_SEQ += 1
    return f"dl{_JOB_SEQ}-{int(time.time())}"


def _clean_etag(headers, filename: str) -> str:
    # ETag may be weak (W/"…") and quoted, esp. for small non-LFS files;
    # strip the W/ prefix + quotes, and keep it filesystem-safe.
    etag = (headers.get("x-linked-etag") or headers.get("etag") or "").strip()
    if etag.startswith("W/"):
        etag = etag[2:]
    etag = etag.strip('"') or filename.replace("/", "_")
    return etag.replace("/", "_")


async def _repo_commit(client: httpx.AsyncClient, repo: str) -> str:
    """The repo's main-branch commit sha. Resolved ONCE per download so every
    file lands in the same snapshots/<sha>/ dir (per-file x-repo-commit is
    inconsistent: LFS files served via CDN drop the header)."""
    try:
        r = await client.get(f"{HF_ENDPOINT}/api/models/{repo}")
        return r.json().get("sha") or "main"
    except Exception:
        return "main"


async def _download_stream(client, url, incomplete, on_chunk) -> None:
    """Single-connection download, resuming a partial via Range append. Retries
    transient drops, recomputing the resume offset from disk each attempt so it
    continues where it left off. Used for small files / non-range servers."""
    counted = incomplete.stat().st_size if incomplete.exists() else 0
    on_chunk(counted)  # pre-existing bytes (counted exactly once, before the loop)
    for attempt in range(_DL_RETRIES + 1):
        resume_from = incomplete.stat().st_size if incomplete.exists() else 0
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    raise RuntimeError("gated/private repo — set HF_TOKEN")
                if resp.status_code == 416:  # range past EOF — already complete
                    return
                if resp.status_code in _RETRY_STATUS:
                    raise _Retryable(f"status {resp.status_code}")
                resp.raise_for_status()
                if resume_from and resp.status_code == 206:
                    mode = "ab"
                else:  # server ignored Range and restarted at 0 — re-count
                    mode = "wb"
                    on_chunk(-counted)
                    counted = 0
                with open(incomplete, mode) as fh:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        fh.write(chunk)
                        counted += len(chunk)
                        on_chunk(len(chunk))
                return
        except _TRANSIENT:
            if attempt >= _DL_RETRIES:
                raise
            await asyncio.sleep(_dl_backoff(attempt))


async def _download_parallel(client, url, incomplete, size, on_chunk) -> None:
    """Multi-connection chunked download (≈hf_transfer). Splits the file into
    fixed chunks fetched over _DL_CONNECTIONS connections, writing each at its
    offset. A ``.parts`` sidecar records completed chunks so it RESUMES across
    interruptions; cancellation leaves the sidecar so re-download continues."""
    parts = Path(str(incomplete) + ".parts")
    n_chunks = (size + _DL_CHUNK - 1) // _DL_CHUNK

    def _chunk_len(i: int) -> int:
        return min(_DL_CHUNK, size - i * _DL_CHUNK)

    done: set[int] = set()
    if incomplete.exists() and parts.exists():
        try:
            meta = json.loads(parts.read_text())
            if meta.get("size") == size and meta.get("chunk") == _DL_CHUNK:
                done = set(meta.get("done", []))
        except Exception:
            done = set()
    if not incomplete.exists() or not done:
        with open(incomplete, "wb") as f:  # preallocate the full size
            f.truncate(size)
        done = set()
        parts.write_text(json.dumps({"size": size, "chunk": _DL_CHUNK, "done": []}))

    for i in done:  # count bytes we already have
        on_chunk(_chunk_len(i))

    sem = asyncio.Semaphore(_DL_CONNECTIONS)
    lock = asyncio.Lock()

    async def fetch(i: int):
        start, end = i * _DL_CHUNK, i * _DL_CHUNK + _chunk_len(i) - 1
        # identity encoding so byte offsets line up (no gzip re-framing); raw_bytes
        # so httpx never decodes content-encoding under us.
        hdrs = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        async with sem:
            for attempt in range(_DL_RETRIES + 1):
                written = 0  # rolled back on retry so progress stays accurate
                try:
                    async with client.stream("GET", url, headers=hdrs) as resp:
                        if resp.status_code in _RETRY_STATUS:
                            raise _Retryable(f"status {resp.status_code}")
                        resp.raise_for_status()
                        if resp.status_code != 206:
                            raise RuntimeError(f"server ignored Range (status {resp.status_code})")
                        with open(incomplete, "r+b") as f:
                            f.seek(start)  # idempotent: a retry re-writes the same offset
                            async for b in resp.aiter_raw(1024 * 256):
                                f.write(b)
                                written += len(b)
                                on_chunk(len(b))
                    break  # chunk complete
                except _TRANSIENT:
                    on_chunk(-written)  # undo this attempt's partial progress
                    if attempt >= _DL_RETRIES:
                        raise
                    await asyncio.sleep(_dl_backoff(attempt))
        async with lock:
            done.add(i)
            parts.write_text(json.dumps({"size": size, "chunk": _DL_CHUNK, "done": sorted(done)}))

    await asyncio.gather(*(fetch(i) for i in range(n_chunks) if i not in done))
    parts.unlink(missing_ok=True)


async def _fetch_one(client: httpx.AsyncClient, repo: str, filename: str, on_chunk, commit: str) -> None:
    """Fetch one repo file into the real HF cache (blobs + snapshot + ref).
    Large range-capable files download in parallel; everything else streams.
    Both resume across interruptions. ``commit`` is the shared snapshot rev."""
    url = f"{HF_ENDPOINT}/{repo}/resolve/main/{filename}"
    cache = _repo_cache_dir(repo)
    (cache / "blobs").mkdir(parents=True, exist_ok=True)
    (cache / "refs").mkdir(parents=True, exist_ok=True)

    # HEAD without following the redirect: the HF response (302 for LFS) carries
    # x-linked-etag (the real sha256, our blob name) + x-linked-size; these are
    # lost once you follow through to the CDN.
    head = await client.head(url, follow_redirects=False)
    if head.status_code in (401, 403):
        raise RuntimeError("gated/private repo — set HF_TOKEN")
    etag = _clean_etag(head.headers, filename)
    size_h = head.headers.get("x-linked-size") or head.headers.get("content-length")
    size = int(size_h) if size_h and size_h.isdigit() else None
    # LFS files (have x-linked-etag) are CDN-served and support ranges.
    ranges_ok = ("x-linked-etag" in head.headers) or head.headers.get("accept-ranges", "").lower() == "bytes"
    blob = cache / "blobs" / etag

    if not blob.exists():
        incomplete = blob.with_suffix(blob.suffix + ".incomplete")
        if size and size > _DL_CHUNK and _DL_CONNECTIONS > 1 and ranges_ok:
            await _download_parallel(client, url, incomplete, size, on_chunk)
        else:
            await _download_stream(client, url, incomplete, on_chunk)
        if incomplete.exists():
            incomplete.rename(blob)
    else:
        on_chunk(blob.stat().st_size)

    snap = cache / "snapshots" / commit / filename
    snap.parent.mkdir(parents=True, exist_ok=True)
    if not snap.exists():
        rel = os.path.relpath(blob, snap.parent)
        try:
            snap.symlink_to(rel)
        except OSError:
            shutil.copy2(blob, snap)
    (cache / "refs" / "main").write_text(commit)


async def _file_size(client: httpx.AsyncClient, repo: str, filename: str) -> int | None:
    try:
        r = await client.head(f"{HF_ENDPOINT}/{repo}/resolve/main/{filename}")
        s = r.headers.get("x-linked-size") or r.headers.get("content-length")
        return int(s) if s and s.isdigit() else None
    except Exception:
        return None


async def _download_hf_file(job: DownloadJob):
    """Download a single GGUF file (llama.cpp)."""
    try:
        job.status = "downloading"
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=_hf_headers()) as client:
            commit = await _repo_commit(client, job.repo)
            job.total = await _file_size(client, job.repo, job.filename)

            def on_chunk(n: int):
                job.received += n
            await _fetch_one(client, job.repo, job.filename, on_chunk, commit)
        job.status = "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = str(exc)
        logger.warning("hub download failed (%s/%s): %s", job.repo, job.filename, exc)


async def _download_hf_repo(job: DownloadJob):
    """Download a whole repo (MLX: config + tokenizer + safetensors, etc.)."""
    try:
        job.status = "downloading"
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=_hf_headers()) as client:
            commit = await _repo_commit(client, job.repo)  # one revision for all files
            tree = (await client.get(f"{HF_ENDPOINT}/api/models/{job.repo}/tree/main",
                                     params={"recursive": "true"})).json()
            files = [n for n in tree if n.get("type") == "file"
                     and not n.get("path", "").lower().endswith(".gguf")]
            job.total = sum((n.get("size") or (n.get("lfs") or {}).get("size") or 0) for n in files) or None

            def on_chunk(n: int):
                job.received += n
            for n in files:
                await _fetch_one(client, job.repo, n["path"], on_chunk, commit)
        job.status = "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = str(exc)
        logger.warning("hub repo download failed (%s): %s", job.repo, exc)


class DownloadRequest(BaseModel):
    source: str = "hf"
    repo: str                    # "owner/name"
    filename: str | None = None  # required when kind="file" (the GGUF to fetch)
    kind: str = "file"           # "file" (one GGUF) | "repo" (whole repo, for MLX)


@router.post("/download")
async def start_download(req: DownloadRequest):
    if req.kind == "file" and not req.filename:
        raise HTTPException(400, "filename is required")

    target = req.filename or req.repo
    # Don't start a duplicate — reuse an in-flight download for the same target.
    for j in _JOBS.values():
        if j.repo == req.repo and j.filename == target and j.status in ("starting", "downloading"):
            return j.public()

    job = DownloadJob(_new_job_id(), "hf", req.repo, target)
    _JOBS[job.id] = job
    runner = _download_hf_repo if req.kind == "repo" else _download_hf_file
    job.task = asyncio.create_task(runner(job))
    return job.public()


@router.get("/downloads")
async def list_downloads():
    # newest first; drop very old finished jobs to keep the list tidy
    jobs = sorted(_JOBS.values(), key=lambda j: j.started, reverse=True)
    return {"downloads": [j.public() for j in jobs[:50]]}


@router.post("/downloads/{job_id}/cancel")
async def cancel_download(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such download")
    if job.task and not job.task.done():
        job.task.cancel()
    return {"cancelled": job_id}
