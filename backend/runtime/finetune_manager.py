"""Local LoRA fine-tuning with MLX (Apple Silicon).

A job runs ``mlx_lm.lora`` as a child process (through ``mlx_shim.py``, which
caps the MLX allocator), parses its output for loss metrics, streams progress to
subscribers (SSE), then fuses the adapter into a standalone model directory under
``<VW_HOME>/models``. The Models page lists that directory as an on-device model
and ``llama_manager`` serves it with mlx-lm, so a finished job is one click from
answering in Chat.

Vision-language bases train their text side with ``mlx_vlm.lora`` (vision
frozen). mlx-vlm can't fuse, so the result is base + adapter, served together.

Training runs with the same interpreter the MLX engine lives in
(``engines._pip_target_python``: the managed ``engines/mlx`` venv, or the backend
venv in dev), and "Install MLX" goes through the engines installer.

Layout under ``<VW_HOME>/finetune`` (resolved at call time, so tests can move it):
  datasets/<id>.jsonl + <id>.json    uploaded or feedback-built data + its metadata
  jobs/<id>/job.json                 the job record (status, config, metrics, log tail)
  jobs/<id>/{data,adapters,train.log,config.yaml}
One job at a time: two MLX runs would exhaust unified memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import config

_MAX_LOG = 400            # log lines kept per job in memory
_MAX_METRICS = 2000
_PERSIST_METRICS = 250    # metric points kept in job.json (evenly downsampled)
_PERSIST_LOG = 40

# A job in any of these states owns the GPU / unified memory.
ACTIVE = ("queued", "preparing", "running", "fusing", "cancelling")
FINISHED = ("done", "error", "cancelled", "interrupted")

# mlx-lm ("Iter 10: Train loss 1.23") and mlx-vlm ≥ 0.7 ("optimizer_step=10, train_loss=1.23";
# "Validation (iteration 10):" then "val_loss=0.53"; tqdm bars "| 3/40 [").
_ITER_TRAIN = re.compile(r"Iter\s+(\d+):\s+Train loss\s+([\d.eE+-]+)")
_ITER_VAL = re.compile(r"Iter\s+(\d+):\s+Val loss\s+([\d.eE+-]+)")
_KV_TRAIN = re.compile(r"optimizer_step=(\d+),\s*train_loss=([\d.eE+-]+)")
_KV_VAL_ITER = re.compile(r"Validation \(iteration (\d+)\)")
_KV_VAL = re.compile(r"(?<![\w])val_loss=([\d.eE+-]+)")
_TQDM = re.compile(r"\|\s*(\d+)/(\d+)\s*\[")
_VAL_ITER: dict[str, int] = {}
# mlx-vlm wraps numbers in ANSI colour codes, which would break the loss regex.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"

_JOBS: dict[str, dict] = {}
_PROCS: dict[str, asyncio.subprocess.Process] = {}
_SUBS: dict[str, set[asyncio.Queue]] = {}
_loaded_home: Path | None = None
_setup: dict = {"status": "idle"}


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _root() -> Path:
    return config.APP_HOME / "finetune"


def datasets_dir() -> Path:
    d = _root() / "datasets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jobs_dir() -> Path:
    d = _root() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    """Fused outputs land here; the Models page lists them as on-device models."""
    d = config.APP_HOME / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Persistence: one job.json per job dir
# --------------------------------------------------------------------------- #

def _downsample(seq: list, n: int) -> list:
    """At most n items, evenly spaced, always keeping the last."""
    if len(seq) <= n:
        return seq
    step = len(seq) / n
    out = [seq[int(i * step)] for i in range(n)]
    out[-1] = seq[-1]
    return out


def _persist(job: dict) -> None:
    rec = {**job, "logTail": job.get("logTail", [])[-_PERSIST_LOG:],
           "metrics": _downsample(job.get("metrics", []), _PERSIST_METRICS)}
    d = jobs_dir() / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / "job.json")


def _ensure_loaded() -> None:
    """Load job records from disk once per home. A job that was mid-run when
    the process died can't be resumed, so it is marked interrupted."""
    global _loaded_home
    home = config.APP_HOME
    if _loaded_home == home:
        return
    _JOBS.clear()
    for f in sorted(jobs_dir().glob("*/job.json")):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") in ACTIVE:
            job["status"] = "interrupted"
            job["error"] = job.get("error") or "the backend restarted while this job was running"
        _JOBS[job["id"]] = job
    _loaded_home = home


# --------------------------------------------------------------------------- #
# Capability / setup
# --------------------------------------------------------------------------- #

def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _mlx_python() -> str:
    """The interpreter that has (or will get) mlx-lm: the engines install target."""
    from runtime import engines
    return engines._pip_target_python()


def _has_module(module: str) -> bool:
    py = _mlx_python()
    if py == sys.executable:
        import importlib.util
        top, _, rest = module.partition(".")
        spec = importlib.util.find_spec(top)  # top-level lookup doesn't import it
        if spec is None or not rest:
            return spec is not None
        # mlx_vlm.train: look for the file rather than import mlx_vlm (and MLX)
        # into the backend process
        return any((Path(d) / rest).with_suffix(".py").exists() or (Path(d) / rest).is_dir()
                   for d in (spec.submodule_search_locations or []))
    if not Path(py).exists():
        return False
    try:
        r = subprocess.run([py, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('{module}') else 1)"],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def mlx_installed() -> bool:
    return _has_module("mlx_lm")


def mlx_vlm_installed() -> bool:
    return _has_module("mlx_vlm")


def capability() -> dict:
    return {
        "appleSilicon": _apple_silicon(),
        "mlxInstalled": mlx_installed(),
        "mlxVlmInstalled": mlx_vlm_installed(),
        "python": _mlx_python(),
        "setup": dict(_setup),
    }


async def setup() -> dict:
    """Install mlx-lm through the engines installer (explicit, user-initiated)."""
    if not _apple_silicon():
        raise RuntimeError("fine-tuning needs Apple Silicon (MLX); this machine isn't arm64 macOS")
    if mlx_installed():
        return {"ok": True, "already": True}
    if _setup.get("status") == "running":
        return {"ok": True, "started": False, "running": True}
    _setup.update(status="running", output="")

    async def _go():
        from runtime import engines
        try:
            res = await engines.update("mlx-lm")
            _setup.update(status="done" if res.get("ok") else "error", output=(res.get("output") or "")[-2000:])
        except Exception as exc:  # noqa: BLE001
            _setup.update(status="error", output=str(exc))

    asyncio.create_task(_go())
    return {"ok": True, "started": True}


# --------------------------------------------------------------------------- #
# Base models: what on disk MLX can LoRA-train
# --------------------------------------------------------------------------- #

_SEQ2SEQ = ("t5", "mt5", "umt5", "bart", "mbart", "marian", "pegasus",
            "m2m_100", "nllb", "prophetnet", "led", "blenderbot")


def _trainable(cfg: dict) -> tuple[bool, str | None, str | None]:
    """(trainable, note, trainer). Causal-LMs train with mlx-lm; a vision-language
    model trains its text side with mlx-vlm (vision frozen). Audio, seq2seq and
    encoder models are not trainable here."""
    archs = [str(a) for a in (cfg.get("architectures") or [])]
    mtype = (cfg.get("model_type") or "").lower()
    is_vlm = "vision_config" in cfg or "vl" in mtype or any("VL" in a or "Vision" in a for a in archs)
    is_audio = ("audio_config" in cfg or "talker_config" in cfg
                or any(k in mtype for k in ("audio", "whisper", "conformer", "parakeet", "wav2vec", "speech", "tts"))
                or any("TTS" in a or "Speech" in a for a in archs))
    if any(a.endswith("ForCausalLM") for a in archs):
        return True, None, "mlx-lm"
    if is_audio:
        return False, "speech / audio model", None
    if is_vlm:
        return True, "text-only LoRA via mlx-vlm (vision frozen)", "mlx-vlm"
    if any(a.endswith("ForConditionalGeneration") for a in archs):
        # Newer chat families name plain text causal-LMs *ForConditionalGeneration*
        # too (e.g. Qwen3.5). Text-only and not a classic encoder-decoder → mlx-lm.
        if mtype and not any(mtype.startswith(s) for s in _SEQ2SEQ):
            return True, None, "mlx-lm"
        return False, "multimodal / seq2seq model", None
    if any(a.endswith("Model") for a in archs) or mtype in ("bert", "roberta", "xlm-roberta", "nomic_bert"):
        return False, "encoder / embedding model", None
    return False, "not a causal-LM text model", None


def _latest_snapshot(repo_dir: Path) -> Path | None:
    snaps = repo_dir / "snapshots"
    if not snaps.is_dir():
        return None
    dirs = sorted((d for d in snaps.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs:
        if (d / "config.json").exists():
            return d
    return dirs[0] if dirs else None


def _missing_shards(path: Path) -> int:
    """Weight files a sharded model's index names but the folder lacks (a
    partial download). 0 when complete or unsharded."""
    idx = _read_cfg(path / "model.safetensors.index.json")
    if idx and isinstance(idx.get("weight_map"), dict):
        return sum(1 for f in set(idx["weight_map"].values()) if not (path / f).exists())
    # No index (yet): shard names say how many files to expect, e.g. model-00001-of-00004.
    expected: set[str] = set()
    for f in path.glob("*-of-*.safetensors"):
        m = re.fullmatch(r"(.*)-(\d+)-of-(\d+)\.safetensors", f.name)
        if m:
            width, total = len(m.group(2)), int(m.group(3))
            expected |= {f"{m.group(1)}-{i:0{width}d}-of-{m.group(3)}.safetensors" for i in range(1, total + 1)}
    return sum(1 for f in expected if not (path / f).exists())


def _base_entry(repo: str, name: str, path: Path, cfg: dict, weights: list[Path]) -> dict:
    trainable, note, trainer = _trainable(cfg)
    missing = _missing_shards(path)
    if trainable and missing:
        trainable, note, trainer = False, f"incomplete download ({missing} weight file(s) missing)", None
    bits = (cfg.get("quantization") or {}).get("bits")
    size = sum((p.stat().st_size for p in weights if p.exists()), 0)
    return {"repo": repo, "name": name, "path": str(path), "modelType": cfg.get("model_type"),
            "mlx": bool(cfg.get("quantization")), "quant": f"{bits}-bit" if bits else None,
            "sizeGb": round(size / 1e9, 2), "trainable": trainable, "reason": note, "trainer": trainer}


def _read_cfg(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_base_models() -> list[dict]:
    """Safetensors models in the HF cache (GGUF-only repos can't be trained) plus
    fused fine-tunes under ``<VW_HOME>/models`` (``local/`` namespace, the same as
    the Models page), each flagged trainable or not."""
    out: list[dict] = []
    if _HF_CACHE.is_dir():
        for repo_dir in _HF_CACHE.glob("models--*"):
            snap = _latest_snapshot(repo_dir)
            weights = list(snap.glob("*.safetensors")) if snap else []
            cfg = _read_cfg(snap / "config.json") if snap and weights else None
            if cfg is None:
                continue
            repo = repo_dir.name[len("models--"):].replace("--", "/")
            out.append(_base_entry(repo, repo.split("/")[-1], snap, cfg, weights))
    base = config.APP_HOME / "models"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            weights = list(d.glob("*.safetensors")) if d.is_dir() else []
            cfg = _read_cfg(d / "config.json") if weights else None
            if cfg is None:
                continue
            out.append(_base_entry(f"local/{d.name}", d.name, d, cfg, weights))
    out.sort(key=lambda m: (not m["trainable"], m["repo"].lower()))
    return out


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #

def _row_format(obj: dict) -> str | None:
    msgs = obj.get("messages")
    if isinstance(msgs, list) and msgs:
        ok = all(isinstance(m, dict) and "role" in m and "content" in m for m in msgs)
        return "chat" if ok else None
    if isinstance(obj.get("prompt"), str) and isinstance(obj.get("completion"), str):
        return "completion"
    if isinstance(obj.get("text"), str):
        return "text"
    return None


def inspect_jsonl(path: Path) -> tuple[int, str]:
    """Every line must be a JSON object in ONE supported mlx-lm schema.
    Returns (count, format); raises ValueError naming the offending line."""
    count, fmt = 0, None
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {i}: invalid JSON ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"line {i}: each line must be a JSON object")
            rf = _row_format(obj)
            if rf is None:
                raise ValueError(f"line {i}: unsupported row; expected "
                                 '{"messages":[{role,content}]}, {"prompt","completion"} or {"text"}')
            if fmt is None:
                fmt = rf
            elif fmt != rf:
                raise ValueError(f"line {i}: mixed formats ({fmt} and {rf}); keep one schema per file")
            count += 1
    if count == 0:
        raise ValueError("dataset is empty")
    return count, fmt or "text"


def save_dataset(filename: str, raw: bytes, source: str = "upload") -> dict:
    did = "ds_" + uuid.uuid4().hex[:8]
    path = datasets_dir() / f"{did}.jsonl"
    path.write_bytes(raw)
    try:
        examples, fmt = inspect_jsonl(path)
    except (ValueError, UnicodeDecodeError) as exc:
        path.unlink(missing_ok=True)
        raise ValueError(str(exc)) from exc
    name = Path(filename or "dataset.jsonl").name
    rec = {"id": did, "name": name, "path": str(path), "examples": examples, "format": fmt,
           "source": source, "createdAt": int(time.time())}
    (datasets_dir() / f"{did}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def list_datasets() -> list[dict]:
    out = []
    for f in datasets_dir().glob("ds_*.json"):
        rec = _read_cfg(f)
        if rec and Path(rec.get("path", "")).exists():
            out.append(rec)
    return sorted(out, key=lambda r: r.get("createdAt", 0), reverse=True)


def get_dataset(did: str) -> dict | None:
    if not re.fullmatch(r"ds_[0-9a-f]{8}", did or ""):
        return None
    return _read_cfg(datasets_dir() / f"{did}.json")


def delete_dataset(did: str) -> bool:
    rec = get_dataset(did)
    if not rec:
        return False
    for p in (datasets_dir() / f"{did}.jsonl", datasets_dir() / f"{did}.json"):
        p.unlink(missing_ok=True)
    return True


def _split(dataset_path: str, dest: Path, batch_size: int = 1) -> int:
    """Split one JSONL into the train.jsonl + valid.jsonl mlx-lm expects. Both
    splits must hold a full batch or mlx-lm raises; tiny sets share all rows."""
    dest.mkdir(parents=True, exist_ok=True)
    lines = [ln for ln in Path(dataset_path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise ValueError("dataset is empty")
    n, b = len(lines), max(1, batch_size)
    n_val = max(b, n // 10)
    if n - n_val < b:
        train = valid = lines
    else:
        valid, train = lines[:n_val], lines[n_val:]
    (dest / "train.jsonl").write_text("\n".join(train) + "\n", encoding="utf-8")
    (dest / "valid.jsonl").write_text("\n".join(valid) + "\n", encoding="utf-8")
    return n


# --------------------------------------------------------------------------- #
# Pub/sub for the SSE stream
# --------------------------------------------------------------------------- #

def subscribe(job_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _SUBS.setdefault(job_id, set()).add(q)
    return q


def unsubscribe(job_id: str, q: asyncio.Queue) -> None:
    _SUBS.get(job_id, set()).discard(q)


def _emit(job_id: str, evt: dict) -> None:
    for q in list(_SUBS.get(job_id, set())):
        q.put_nowait(evt)


def _set_status(job: dict, status: str, **extra) -> None:
    job["status"] = status
    job.update(extra)
    _emit(job["id"], {"type": "status", "status": status, **extra})
    _persist(job)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

def list_jobs() -> list[dict]:
    _ensure_loaded()
    return sorted(_JOBS.values(), key=lambda j: j.get("createdAt", 0), reverse=True)


def get_job(job_id: str) -> dict | None:
    _ensure_loaded()
    return _JOBS.get(job_id)


def _clean_base_name(base: str) -> str:
    """Readable name from an HF id or a cache snapshot path (whose basename is a hash)."""
    p = Path(base)
    for part in p.parts:
        if part.startswith("models--"):
            return part[len("models--"):].replace("--", "/").split("/")[-1]
    return p.name


def _slug(name: str) -> str | None:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-.")[:64]
    return s or None


def _write_config(job_dir: Path, cfg: dict) -> Path:
    """mlx-lm lora YAML (the lora_config.yaml shape). Values are JSON-encoded,
    which is valid YAML and keeps paths with odd characters safe."""
    iters = cfg["iters"]
    rows = {
        "model": cfg["baseModel"], "train": True, "data": str(job_dir / "data"),
        "fine_tune_type": "lora", "num_layers": cfg["numLayers"], "batch_size": cfg["batchSize"],
        "iters": iters, "learning_rate": cfg["learningRate"], "max_seq_length": cfg["maxSeqLength"],
        "grad_checkpoint": bool(cfg.get("gradCheckpoint")), "adapter_path": str(job_dir / "adapters"),
        # cadences must not exceed the run, or mlx never reaches them
        "steps_per_eval": min(iters, max(10, iters // 5)), "save_every": min(iters, max(20, iters // 2)),
    }
    text = "".join(f"{k}: {json.dumps(v)}\n" for k, v in rows.items())
    text += ("lora_parameters:\n"
             f"  rank: {int(cfg['loraRank'])}\n"
             f"  scale: {float(cfg['loraAlpha'])}\n"
             f"  dropout: {float(cfg['loraDropout'])}\n")
    p = job_dir / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _num(config_in: dict, key: str, default, cast, lo=None, hi=None):
    try:
        v = cast(config_in.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if lo is not None and v < lo:
        raise ValueError(f"{key} must be ≥ {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{key} must be ≤ {hi}")
    return v


async def start(config_in: dict) -> dict:
    """Validate, record and launch a training job (one at a time)."""
    _ensure_loaded()
    if not await asyncio.to_thread(mlx_installed):
        raise RuntimeError("mlx-lm is not installed; use Install MLX on the Train page first")
    # (checked after the await, so two quick clicks can't both pass)
    busy = next((j for j in _JOBS.values() if j.get("status") in ACTIVE), None)
    if busy:
        raise RuntimeError(f"a training job is already active ({busy['name']}, {busy['status']}); "
                           "stop it before starting another")

    base = (config_in.get("baseModel") or "").strip()
    if not base:
        raise ValueError("baseModel is required")
    ds = get_dataset(config_in.get("datasetId") or "")
    if not ds:
        raise ValueError("pick a dataset (upload one or build it from feedback)")
    examples, fmt = inspect_jsonl(Path(ds["path"]))

    trainer = config_in.get("trainer")
    if trainer not in ("mlx-lm", "mlx-vlm"):
        cfg_json = _read_cfg(Path(base) / "config.json") if Path(base).exists() else None
        trainer = (_trainable(cfg_json)[2] if cfg_json else None) or "mlx-lm"
    if trainer == "mlx-vlm":
        if not await asyncio.to_thread(mlx_vlm_installed):
            raise RuntimeError("mlx-vlm is not installed; install it from Settings → Engines")
        if fmt != "chat":
            raise ValueError('vision-language training needs a chat dataset ({"messages": [...]} per line)')

    batch = _num(config_in, "batchSize", 1, int, 1, 64)
    if config_in.get("useEpochs"):
        epochs = _num(config_in, "epochs", 3, int, 1, 100)
        iters = max(1, (examples * epochs) // batch)
    else:
        epochs = None
        iters = _num(config_in, "iters", 300, int, 1, 100000)

    cfg = {
        "baseModel": base, "trainer": trainer, "datasetId": ds["id"], "datasetName": ds["name"],
        "iters": iters, "useEpochs": bool(config_in.get("useEpochs")), "epochs": epochs,
        "batchSize": batch,
        "numLayers": _num(config_in, "numLayers", 8, int, 1, 256),
        "learningRate": _num(config_in, "learningRate", 2e-4, float, 1e-8, 1.0),
        "maxSeqLength": _num(config_in, "maxSeqLength", 2048, int, 64, 131072),
        "loraRank": _num(config_in, "loraRank", 8, int, 1, 512),
        "loraAlpha": _num(config_in, "loraAlpha", 16, float, 0.1, 4096),
        "loraDropout": _num(config_in, "loraDropout", 0.0, float, 0.0, 0.9),
        "gradCheckpoint": bool(config_in.get("gradCheckpoint")),
        # A named job also names its fused output dir (local/<name> on the Models page).
        "outputName": _slug(config_in["name"]) if config_in.get("name") else None,
    }
    if any(j.get("status") in ACTIVE for j in _JOBS.values()):
        raise RuntimeError("a training job is already active; stop it before starting another")
    jid = "ft_" + uuid.uuid4().hex[:8]
    job_dir = jobs_dir() / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": jid, "status": "queued", "config": cfg, "examples": examples,
        "createdAt": int(time.time()), "finishedAt": None, "metrics": [], "logTail": [],
        "progress": {"iter": 0, "total": iters}, "outputPath": None, "error": None,
        "name": (config_in.get("name") or "").strip() or f"{_clean_base_name(base)} · {Path(ds['name']).stem}",
    }
    _JOBS[jid] = job
    _persist(job)
    asyncio.create_task(_run(job, job_dir))
    return job


def _log(job: dict, line: str) -> None:
    job.setdefault("logTail", []).append(line)
    if len(job["logTail"]) > _MAX_LOG:
        job["logTail"] = job["logTail"][-_MAX_LOG:]
    _emit(job["id"], {"type": "log", "line": line})
    try:  # tee to disk so the log survives a crash
        with open(jobs_dir() / job["id"] / "train.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def parse_line(job: dict, line: str) -> None:
    """Record one line of trainer output: log it and pick out loss metrics."""
    _log(job, line)
    jid, total = job["id"], job["config"]["iters"]

    def train(it: int, loss: float) -> None:
        job["progress"] = {"iter": it, "total": total}
        job["metrics"].append({"iter": it, "trainLoss": loss})
        _emit(jid, {"type": "metric", "iter": it, "trainLoss": loss})

    def val(it: int, loss: float) -> None:
        job["metrics"].append({"iter": it, "valLoss": loss})
        _emit(jid, {"type": "metric", "iter": it, "valLoss": loss})

    bars = _TQDM.findall(line)
    if bars:  # progress between loss reports (the last bar on a \r-joined line wins)
        it = min(int(bars[-1][0]), total)
        if it > (job.get("progress") or {}).get("iter", 0):
            job["progress"] = {"iter": it, "total": total}
            _emit(jid, {"type": "progress", "iter": it, "total": total})
    if (vi := _KV_VAL_ITER.search(line)):
        _VAL_ITER[jid] = int(vi.group(1))
    if (m := _ITER_TRAIN.search(line)):
        train(int(m.group(1)), float(m.group(2)))
    elif (m := _ITER_VAL.search(line)):
        val(int(m.group(1)), float(m.group(2)))
    elif (m := _KV_TRAIN.search(line)):
        train(int(m.group(1)), float(m.group(2)))
    elif (m := _KV_VAL.search(line)):
        val(_VAL_ITER.get(jid, (job.get("progress") or {}).get("iter", 0)), float(m.group(1)))
    if len(job["metrics"]) > _MAX_METRICS:
        job["metrics"] = job["metrics"][-_MAX_METRICS:]


async def _pump(job: dict, proc, metrics: bool) -> None:
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
        if line:
            parse_line(job, line) if metrics else _log(job, line)


def _signal_group(proc, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)  # pgid == pid (start_new_session)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate(proc, grace: float = 5.0) -> None:
    """SIGTERM the trainer's whole process group (a helper process holding the
    output pipe would otherwise keep the job in "cancelling"), then SIGKILL it
    if it is still around after ``grace`` seconds."""
    if proc.returncode is not None:
        return
    _signal_group(proc, signal.SIGTERM)

    async def _escalate():
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)
    try:
        asyncio.get_running_loop().create_task(_escalate())
    except RuntimeError:
        pass


class _Cancelled(Exception):
    """The job was cancelled between steps (no process to signal)."""


async def _exec(job: dict, cmd: list[str], cwd: Path, metrics: bool = True):
    if job.get("status") in ("cancelling", "cancelled"):
        raise _Cancelled
    _log(job, "$ " + " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True)  # own process group, so a stop reaches its children too
    _PROCS[job["id"]] = proc
    if job.get("status") == "cancelling":  # cancelled while the process was spawning
        _terminate(proc)
    try:
        await _pump(job, proc, metrics)
        return await proc.wait()
    finally:
        _PROCS.pop(job["id"], None)


def _finish_if_stopped(job: dict, rc: int | None, what: str) -> bool:
    if job.get("status") in ("cancelling", "cancelled"):
        _set_status(job, "cancelled", finishedAt=int(time.time()))
        return True
    if rc != 0:
        _set_status(job, "error", error=f"{what} exited with code {rc}", finishedAt=int(time.time()))
        return True
    return False


async def _run(job: dict, job_dir: Path) -> None:
    cfg = job["config"]
    py = _mlx_python()
    if job.get("status") == "cancelled":  # cancelled before the task got going
        return
    try:
        _set_status(job, "preparing")
        if cfg["trainer"] == "mlx-vlm":
            data = job_dir / "data"
            iters = cfg["iters"]
            common = ["--iters", str(iters), "--batch-size", str(cfg["batchSize"]),
                      "--learning-rate", str(cfg["learningRate"]), "--max-seq-length", str(cfg["maxSeqLength"]),
                      "--lora-rank", str(cfg["loraRank"]), "--lora-alpha", str(int(cfg["loraAlpha"])),
                      "--lora-dropout", str(cfg["loraDropout"]),
                      "--steps-per-eval", str(min(iters, max(10, iters // 5))),
                      # where the adapter is saved; no --train-vision → language side only
                      "--output-path", str(job_dir / "adapters")]
            if await asyncio.to_thread(_has_module, "mlx_vlm.train"):
                # mlx-vlm ≥ 0.7: one trainer, reads a train/valid JSONL folder like mlx-lm
                _split(str(datasets_dir() / f"{cfg['datasetId']}.jsonl"), data, cfg["batchSize"])
                cmd = [py, "-m", "mlx_vlm.train", "--model", cfg["baseModel"], "--data", str(data), *common]
            else:
                # older mlx-vlm: mlx_vlm.lora loads a datasets "train" split from a folder
                data.mkdir(parents=True, exist_ok=True)
                lines = [ln for ln in Path(datasets_dir() / f"{cfg['datasetId']}.jsonl").read_text(
                    encoding="utf-8").splitlines() if ln.strip()]
                (data / "train.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
                cmd = [py, "-m", "mlx_vlm.lora", "--model-path", cfg["baseModel"],
                       "--dataset", str(data), "--split", "train", *common]
            _advance(job, "running")
            rc = await _exec(job, cmd, job_dir)
            if _finish_if_stopped(job, rc, "training"):
                return
            # mlx-vlm can't fuse: the adapter is the deliverable (served with its base)
            out = str(job_dir / "adapters")
            _set_status(job, "done", outputPath=out, finishedAt=int(time.time()))
            _emit(job["id"], {"type": "done", "outputPath": out})
            return

        _split(str(datasets_dir() / f"{cfg['datasetId']}.jsonl"), job_dir / "data", cfg["batchSize"])
        cfg_path = _write_config(job_dir, cfg)
        shim = Path(__file__).with_name("mlx_shim.py")
        _advance(job, "running")
        rc = await _exec(job, [py, str(shim), "mlx_lm.lora", "-c", str(cfg_path)], job_dir)
        if _finish_if_stopped(job, rc, "training"):
            return

        _advance(job, "fusing")
        out_dir = models_dir() / (cfg.get("outputName") or f"{_clean_base_name(cfg['baseModel'])}-ft-{job['id']}")
        if out_dir.exists():
            out_dir = models_dir() / f"{out_dir.name}-{job['id']}"
        rc = await _exec(job, [py, "-m", "mlx_lm.fuse", "--model", cfg["baseModel"],
                               "--adapter-path", str(job_dir / "adapters"),
                               "--save-path", str(out_dir)], job_dir, metrics=False)
        if _finish_if_stopped(job, rc, "fuse"):
            return
        _set_status(job, "done", outputPath=str(out_dir), finishedAt=int(time.time()))
        _emit(job["id"], {"type": "done", "outputPath": str(out_dir)})
    except _Cancelled:
        _set_status(job, "cancelled", finishedAt=int(time.time()))
    except Exception as exc:  # noqa: BLE001 — a job failure must never crash the app
        _set_status(job, "error", error=str(exc), finishedAt=int(time.time()))
    finally:
        _VAL_ITER.pop(job["id"], None)


def _advance(job: dict, status: str) -> None:
    """Move a job to its next phase unless a cancel is pending."""
    if job.get("status") in ("cancelling", "cancelled"):
        raise _Cancelled
    _set_status(job, status)


async def cancel(job_id: str) -> dict:
    job = get_job(job_id)
    if not job:
        raise KeyError(job_id)
    if job.get("status") not in ACTIVE or job.get("status") == "cancelling":
        return job
    proc = _PROCS.get(job_id)
    if job.get("status") == "queued":
        _set_status(job, "cancelled", finishedAt=int(time.time()))
        return job
    # The run loop sees "cancelling" before its next step; a live process is signalled now.
    _set_status(job, "cancelling")
    if proc:
        _terminate(proc)
    return job


async def shutdown() -> None:
    """Stop any training process when the backend exits, so it doesn't keep
    holding unified memory as an orphan (the job reloads as interrupted)."""
    procs = [p for p in _PROCS.values() if p.returncode is None]
    for proc in procs:
        _signal_group(proc, signal.SIGTERM)
    for proc in procs:
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)


async def activate(job_id: str) -> dict:
    """Serve a finished job as the active on-device model."""
    job = get_job(job_id)
    if not job or job.get("status") != "done" or not job.get("outputPath"):
        raise ValueError("job is not finished")
    if not Path(job["outputPath"]).exists():
        raise ValueError("the trained model is no longer on disk")
    from runtime import llama_manager
    if job["config"].get("trainer") == "mlx-vlm":
        base = job["config"]["baseModel"]
        if not Path(base).exists():
            raise ValueError("serving a vision adapter needs a base model on disk "
                             "(train from a downloaded model, not a Hugging Face id)")
        return await llama_manager.start(base, adapter=job["outputPath"], force=True)
    return await llama_manager.start(job["outputPath"], force=True)


def delete(job_id: str) -> dict:
    """Remove a job and reclaim its disk (adapters + fused model)."""
    job = get_job(job_id)
    if not job:
        raise KeyError(job_id)
    if job.get("status") in ACTIVE:
        raise ValueError("job is still active; stop it before deleting")
    freed = 0
    targets = [jobs_dir() / job_id]
    out = Path(job["outputPath"]) if job.get("outputPath") else None
    # only ever delete inside our own dirs
    if out and (out.parent == models_dir() or jobs_dir() in out.parents):
        targets.append(out)
    for t in targets:
        if t.exists():
            freed += sum((p.stat().st_size for p in t.rglob("*") if p.is_file()), 0)
            shutil.rmtree(t, ignore_errors=True)
    _JOBS.pop(job_id, None)
    _SUBS.pop(job_id, None)
    _VAL_ITER.pop(job_id, None)
    return {"deleted": job_id, "freedBytes": freed}
