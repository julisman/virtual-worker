"""First-run experience — tells the UI whether setup is needed and what to
recommend for this machine.

``GET /api/onboarding/status`` is cheap and side-effect free: the wizard is
shown when nothing can answer a chat yet (no local model, no provider key)
and the operator hasn't dismissed it. ``recommendations`` are search presets
for the model hub ordered for this machine's memory and engines; the wizard
resolves them against the hub so the exact repo/file is always a real one.
"""

from __future__ import annotations

import platform
import sys

from fastapi import APIRouter

from storage import agent_store, chat_channel_store, settings

router = APIRouter()

_KEY = "onboarding"


def _mem_gb() -> float | None:
    try:
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)
            return round(int(out.stdout.strip()) / 1024 ** 3, 1)
        import os
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001
        return None


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _mlx_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("mlx_lm") is not None
    except Exception:  # noqa: BLE001
        return False


def recommendations(mem_gb: float | None, apple_silicon: bool, mlx: bool) -> list[dict]:
    """Ordered hub search presets: the first that fits is the default pick.
    Each entry is a query the hub resolves live, never a hard-coded file."""
    use_mlx = apple_silicon and mlx
    gb = mem_gb or 16
    recs: list[dict] = []
    if use_mlx:
        if gb >= 24:
            recs.append({"label": "Qwen3.5 9B (4-bit, MLX)", "q": "Qwen3.5-9B-MLX-4bit", "fmt": "mlx",
                         "why": "Strong tool use and reasoning; about 6 GB.", "kind": "repo"})
        if gb >= 16:
            recs.append({"label": "Qwen3 8B (4-bit, MLX)", "q": "Qwen3-8B-4bit", "fmt": "mlx",
                         "why": "Reliable tool calling; about 5 GB.", "kind": "repo"})
        recs.append({"label": "Qwen2.5 3B Instruct (4-bit, MLX)", "q": "Qwen2.5-3B-Instruct-4bit", "fmt": "mlx",
                     "why": "Small and fast; fine for chat and simple tools; about 2 GB.", "kind": "repo"})
    else:
        if gb >= 16:
            recs.append({"label": "Qwen3 8B (Q4_K_M, GGUF)", "q": "Qwen3-8B-GGUF", "fmt": "gguf", "quant": "Q4_K_M",
                         "why": "Reliable tool calling on llama.cpp; about 5 GB.", "kind": "file"})
        recs.append({"label": "Qwen3 4B (Q4_K_M, GGUF)", "q": "Qwen3-4B-GGUF", "fmt": "gguf", "quant": "Q4_K_M",
                     "why": "Small and fast; about 2.5 GB.", "kind": "file"})
    return recs


def _providers_configured() -> int:
    try:
        from gateway import providers
        return sum(1 for p in providers.catalog() if p.get("hasKey") or p.get("hasToken"))
    except Exception:  # noqa: BLE001
        return 0


def _local_models() -> list[dict]:
    try:
        from routes import hub
        return hub._scan_hf_cache() + hub._scan_mlx_cache() + hub._scan_fused_models()
    except Exception:  # noqa: BLE001
        return []


@router.get("/status")
async def status():
    from runtime import llama_manager
    state = settings.get(_KEY) or {}
    try:
        rt = await llama_manager.status()
    except Exception:  # noqa: BLE001
        rt = {}
    local = _local_models()
    agents = agent_store.list_agents()
    accounts = chat_channel_store.list_accounts()
    mem = _mem_gb(); apple = _apple_silicon(); mlx = _mlx_available()
    providers_n = _providers_configured()
    has_model = bool(rt.get("healthy")) or bool(local) or providers_n > 0
    needed = not bool(state.get("dismissed")) and not (bool(rt.get("healthy")) or providers_n > 0)
    return {
        "needed": needed,
        "dismissed": bool(state.get("dismissed")),
        "completedAt": state.get("completedAt"),
        "modelActive": bool(rt.get("healthy")),
        "modelLoading": bool(rt.get("loading")),
        "localModels": len(local),
        "providersConfigured": providers_n,
        "hasModel": has_model,
        "agents": len(agents),
        "channels": len(accounts),
        "platform": {"os": sys.platform, "appleSilicon": apple, "mlx": mlx, "memGb": mem},
        "recommendations": recommendations(mem, apple, mlx),
    }


@router.post("/dismiss")
async def dismiss():
    from datetime import datetime, timezone
    settings.set(_KEY, {"dismissed": True, "completedAt": datetime.now(timezone.utc).isoformat()})
    return {"ok": True}


@router.post("/reset")
async def reset():
    settings.set(_KEY, None)
    return {"ok": True}
