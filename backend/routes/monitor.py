"""Local-inference monitoring.

  GET  /api/monitor/overview          -> live memory / CPU / disk / runtime / API stats
  GET  /api/monitor/logs?lines=N      -> tail of the inference-server log
  GET  /api/monitor/errors?limit=N    -> recent failed LLM API calls (from the debug DB)

Running models locally makes resource pressure the #1 cause of a wedged or
killed inference server, so this surfaces the things that actually go wrong:
how much memory the box (and the model process) is using, how full the model
disk is, whether the server is healthy, and the most recent errors + log tail.

Read-only and best-effort — every probe degrades to null rather than raising,
and nothing here mutates state. Reuses the hardware probes from routes.hub and
the live process view from runtime.llama_manager so numbers match the rest of
the app. Localhost-only, like the other /api routes.
"""

from __future__ import annotations

import logging
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter

import config
from routes import hub
from runtime import llama_manager as mgr
from storage import debug_db

logger = logging.getLogger(__name__)
router = APIRouter()

GIB = 1024 ** 3


# --------------------------------------------------------------------------- #
# Live memory / process probes (the bits routes.hub doesn't already cover)
# --------------------------------------------------------------------------- #

def _available_ram_bytes() -> int | None:
    """Currently-reclaimable system memory. Platform-specific; None if unknown."""
    system = platform.system()
    try:
        if system == "Linux":
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        elif system == "Darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
            page = 4096
            m = re.search(r"page size of (\d+) bytes", out)
            if m:
                page = int(m.group(1))

            def pages(label: str) -> int:
                mm = re.search(rf"{label}:\s+(\d+)\.", out)
                return int(mm.group(1)) if mm else 0

            # free + reclaimable (inactive/speculative/purgeable) ≈ "available"
            reclaimable = (
                pages("Pages free")
                + pages("Pages inactive")
                + pages("Pages speculative")
                + pages("Pages purgeable")
            )
            return reclaimable * page
    except Exception as exc:  # noqa: BLE001 - best-effort probe
        logger.debug("available-ram probe failed: %s", exc)
    return None


def _process_rss_bytes(pid: int | None) -> int | None:
    """Resident memory of a process (the inference server), via `ps`. KB → bytes."""
    if not pid:
        return None
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return int(out) * 1024 if out else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("rss probe failed for pid %s: %s", pid, exc)
        return None


def _nvidia_used_bytes() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip().splitlines()
        return int(out[0]) * 1024 * 1024 if out else None  # MiB → bytes
    except Exception:  # noqa: BLE001
        return None


def _load_percent(cores: int | None) -> float | None:
    """1-minute load average as a rough % of total CPU capacity."""
    try:
        load1 = __import__("os").getloadavg()[0]
    except (OSError, AttributeError):
        return None
    if not cores:
        return None
    return round(min(load1 / cores * 100, 100), 1)


def _pct(used: int | None, total: int | None) -> float | None:
    if not used or not total:
        return None
    return round(used / total * 100, 1)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.get("/overview")
async def overview() -> dict:
    total_ram = hub._total_ram_bytes()
    avail_ram = _available_ram_bytes()
    used_ram = (total_ram - avail_ram) if (total_ram and avail_ram is not None) else None

    vram, vram_kind = hub._vram_bytes()
    vram_used = _nvidia_used_bytes() if vram_kind == "nvidia" else None

    disk_free, disk_total = hub._disk_usage()
    disk_used = (disk_total - disk_free) if (disk_total and disk_free is not None) else None

    cores, brand = hub._cpu_info()

    rt = await mgr.status()
    pid = rt.get("pid")
    started_at = mgr._state.get("started_at")
    rss = _process_rss_bytes(pid) if rt.get("running") else None

    api = debug_db.summary()

    return {
        "memory": {
            "totalBytes": total_ram,
            "usedBytes": used_ram,
            "availableBytes": avail_ram,
            "percent": _pct(used_ram, total_ram),
            "unified": vram_kind == "unified",  # on Apple Silicon RAM == VRAM
        },
        "vram": {
            "totalBytes": vram,
            "usedBytes": vram_used,
            "percent": _pct(vram_used, vram),
            "kind": vram_kind,  # "unified" | "nvidia" | "unknown"
        },
        "cpu": {
            "cores": cores,
            "brand": brand,
            "loadPercent": _load_percent(cores),
        },
        "disk": {
            "freeBytes": disk_free,
            "usedBytes": disk_used,
            "totalBytes": disk_total,
            "percent": _pct(disk_used, disk_total),
            "path": str(hub.hf_cache_dir()),
        },
        "process": {
            "pid": pid,
            "rssBytes": rss,
            "uptimeSec": round(time.time() - started_at) if started_at else None,
        } if rt.get("running") else None,
        "runtime": {
            "running": rt.get("running"),
            "healthy": rt.get("healthy"),
            "loading": rt.get("loading"),
            "failed": rt.get("failed"),
            "managed": rt.get("managed"),
            "backend": rt.get("backend"),
            "modelName": rt.get("modelName"),
            "quant": rt.get("quant"),
            "ctx": rt.get("ctx"),
            "port": rt.get("port"),
            "error": rt.get("error"),
        },
        "api": {
            "runs": api.get("totalRuns", 0),
            "errors": api.get("totalErrors", 0),
            "sessions": api.get("totalSessions", 0),
            "tokens": api.get("totalTokens", 0),
            "cost": api.get("totalCost", 0),
        },
        "host": {
            "os": platform.system(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "upstreamUrl": config.UPSTREAM_URL,
        },
        "now": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


@router.get("/logs")
async def logs(lines: int = 200) -> dict:
    lines = max(1, min(lines, 2000))
    log_file: Path = mgr._LOG_FILE
    text = ""
    try:
        if log_file.exists():
            text = "\n".join(log_file.read_text(errors="replace").splitlines()[-lines:])
    except Exception as exc:  # noqa: BLE001
        logger.debug("reading inference log failed: %s", exc)
    return {
        "path": str(log_file),
        "exists": log_file.exists(),
        "text": text,
        "source": "inference-server",
    }


@router.get("/errors")
async def errors(limit: int = 25) -> dict:
    limit = max(1, min(limit, 200))
    rows: list[dict] = []
    try:
        conn = debug_db.get_connection()
        cur = conn.execute(
            """
            SELECT run_id, session_id, start_time, path, endpoint, provider, model,
                   status_code, error_text, duration_ms
              FROM debug_runs
             WHERE is_error = 1
             ORDER BY start_time DESC
             LIMIT ?
            """,
            (limit,),
        )
        for r in cur.fetchall():
            rows.append({
                "runId": r["run_id"],
                "session": r["session_id"],
                "time": r["start_time"],
                "endpoint": r["endpoint"] or r["path"],
                "provider": r["provider"],
                "model": r["model"],
                "statusCode": r["status_code"],
                "error": (r["error_text"] or "")[:600],
                "durationMs": r["duration_ms"],
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("recent-errors query failed: %s", exc)
    return {"errors": rows}
