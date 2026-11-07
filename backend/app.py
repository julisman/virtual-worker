"""Virtual Worker — FastAPI application entry point.

Run from this directory:  uvicorn app:app --port 8888 --reload
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from middleware.api_debug import ApiDebugMiddleware
from middleware.team_auth import TeamAuthMiddleware
from routes.activity import router as activity_router
from routes.agent_files import router as agent_files_router
from routes.agents import router as agents_router
from routes.inbox import router as inbox_router
from routes.analytics import router as analytics_router
from routes.anthropic import router as anthropic_router
from routes.auth import router as auth_router
from routes.backup import router as backup_router
from routes.browser import router as browser_router
from routes.channels import router as channels_router
from routes.chat import router as chat_router
from routes.debug import router as debug_router
from routes.delivery import router as delivery_router
from routes.duty import router as duty_router
from routes.engines import router as engines_router
from routes.gateway_info import router as gateway_info_router
from routes.gemini import router as gemini_router
from routes.hub import router as hub_router
from routes.inference import router as inference_router
from routes.kb import router as kb_router
from routes.wiki import router as wiki_router
from routes.mcp import router as mcp_router
from routes.memory import router as memory_router
from routes.monitor import router as monitor_router
from routes.onboarding import router as onboarding_router
from routes.providers import router as providers_router
from routes.registries import router as registries_router
from routes.schedule import router as schedule_router
from routes.responses import router as responses_router
from routes.runtime import router as runtime_router
from routes.sessions import router as sessions_router
from routes.skills import router as skills_router
from routes.tasks import router as tasks_router
from routes.team import router as team_router
from routes.templates import router as templates_router
from routes.tokens import router as tokens_router
from routes.tools import router as tools_router
from routes.triggers import router as triggers_router
from routes.users import router as users_router
from runtime import llama_manager

logging.basicConfig(level=logging.INFO)
# Log hygiene: httpx logs full request URLs at INFO, which would print bot
# tokens (Telegram puts the token in the path). Quiet it, and scrub any
# credential-shaped substrings that still reach a log line.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class _RedactFilter(logging.Filter):
    import re as _re
    _PATTERNS = [
        (_re.compile(r"/bot\d{6,}:[A-Za-z0-9_-]{20,}"), "/bot<redacted>"),
        (_re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1<redacted>"),
        (_re.compile(r"sk-[A-Za-z0-9_-]{12,}"), "sk-<redacted>"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            scrubbed = msg
            for pat, repl in self._PATTERNS:
                scrubbed = pat.sub(repl, scrubbed)
            if scrubbed != msg:
                record.msg, record.args = scrubbed, ()
        except Exception:  # noqa: BLE001
            pass
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown for the always-on background services. (Replaces the
    deprecated @app.on_event handlers with a single lifespan context.)"""
    _log = logging.getLogger(__name__)
    # --- startup --------------------------------------------------------- #
    # Adopt a llama-server that survived a reload, else autostart the default.
    try:
        await llama_manager.adopt_or_autostart()
    except Exception:
        _log.warning("llama adopt/autostart failed", exc_info=True)
    # Web search: adopt/autostart the managed SearXNG (no-op if not installed).
    try:
        from runtime import searxng_manager
        await searxng_manager.adopt_or_autostart()
    except Exception:
        _log.warning("searxng adopt/autostart failed", exc_info=True)
    # Interns never resume autonomous work on their own after a restart (D2):
    # reset any stale on-duty flag from a previous run back to off.
    try:
        from storage import agent_store
        for a in agent_store.list_agents():
            if (a.get("dutyStatus") or "off") != "off":
                agent_store.update_agent(a["id"], {"dutyStatus": "off"})
    except Exception:
        _log.debug("duty reconcile failed", exc_info=True)
    # Scheduler — always-on; honors timed agent runs while the backend is up.
    try:
        from agent.scheduler import scheduler
        scheduler.start()
    except Exception:
        _log.warning("scheduler start failed", exc_info=True)
    # Trigger engine — always-on; polls file triggers + serves webhook fires.
    try:
        from agent.triggers import triggers
        triggers.start()
    except Exception:
        _log.warning("trigger engine start failed", exc_info=True)
    # Agent inbox — drains delegated task flow-back notifications.
    try:
        from agent.inbox import inbox_manager
        inbox_manager.start()
    except Exception:
        _log.warning("agent inbox start failed", exc_info=True)
    # Chat channels — start every enabled account's adapter (dials out; no inbound port).
    try:
        import channels.builtin  # noqa: F401  (registers adapters)
        from channels.manager import manager as channel_manager
        channel_manager.start()
    except Exception:
        _log.warning("channel manager start failed", exc_info=True)
    # Maintenance — periodically prune the trace/activity tables + finished jobs.
    try:
        from runtime.maintenance import maintenance
        maintenance.start()
    except Exception:
        _log.warning("maintenance loop start failed", exc_info=True)

    yield

    # --- shutdown -------------------------------------------------------- #
    # Bounded, concurrent teardown. Each stop is isolated (one failing never
    # blocks the others) and the whole block is capped by a deadline so a wedged
    # component (a hung browser/MCP close, an unbounded in-flight drain) can't
    # keep the process alive on Ctrl+C. This is the app-level guarantee that also
    # covers the packaged desktop build, which has no shell supervisor around it;
    # dev.sh's TERM→KILL and uvicorn's --timeout-graceful-shutdown are the outer
    # defense-in-depth layers.
    async def _stop(name: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            # The shutdown deadline cancelled this component mid-stop. Note it and
            # re-raise so the gather actually unwinds (don't swallow the deadline).
            _log.warning("shutdown: %s did not finish before the deadline", name)
            raise
        except Exception:
            _log.debug("shutdown: %s failed", name, exc_info=True)

    from agent.scheduler import scheduler
    from agent.triggers import triggers
    from agent.inbox import inbox_manager
    from channels.manager import manager as channel_manager
    from runtime.maintenance import maintenance
    from agent.worker import manager as worker
    from agent.tools import close_browser
    from agent.browser_control import close_control_browser
    from agent.mcp_client import close_pool

    try:
        async with asyncio.timeout(6):          # stay under uvicorn's 8s cap
            await asyncio.gather(
                _stop("scheduler", scheduler.stop()),
                _stop("triggers", triggers.stop()),
                _stop("inbox", inbox_manager.stop()),
                _stop("channels", channel_manager.stop()),
                _stop("maintenance", maintenance.stop()),
                _stop("worker", worker.stop_all()),
                _stop("browser", close_browser()),
                _stop("control-browser", close_control_browser()),
                _stop("mcp-pool", close_pool()),
                return_exceptions=True,
            )
    except TimeoutError:
        _log.warning("shutdown deadline hit — exiting with unfinished cleanup")
    except asyncio.CancelledError:
        # asyncio.timeout converts only its OWN cancellation to TimeoutError, so a
        # CancelledError escaping here is a genuine external cancel of the lifespan
        # task — log it but re-raise to honor the cancellation protocol.
        _log.warning("shutdown cancelled externally — exiting with unfinished cleanup")
        raise
    # NB: the managed SearXNG is intentionally left running (detached) so it
    # survives a uvicorn --reload and is re-adopted — same as llama-server.


app = FastAPI(title="Virtual Worker", version="0.1.0", lifespan=lifespan)

# Team-mode auth gate. Added FIRST so it ends up INNERMOST — CORS then wraps its
# 401s with the right headers. In solo mode (default) it's a transparent no-op.
app.add_middleware(TeamAuthMiddleware)
# CORS (added before -> inner), debug capture last (added after -> OUTERMOST so it
# tees the exact bytes the client receives, without blocking streaming). Origins:
# local only by default (+ VW_CORS_ORIGINS) — never "*", so a web page the user
# happens to visit cannot drive the API through their browser.
import config as _config  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=_config.CORS_ORIGINS,
    allow_origin_regex=_config.CORS_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Host-header allow-list (DNS-rebinding guard): local names + VW_ALLOWED_HOSTS.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_config.ALLOWED_HOSTS)
app.add_middleware(ApiDebugMiddleware)

# OpenAI-compatible inference gateway (proxied to VW_UPSTREAM_URL).
app.include_router(inference_router, prefix="/v1", tags=["inference"])
# Anthropic Messages API surface (Claude Code -> local model).
app.include_router(anthropic_router, prefix="/v1", tags=["anthropic"])
# OpenAI Responses API surface (Codex -> local model).
app.include_router(responses_router, prefix="/v1", tags=["responses"])
# Gemini API surface (Gemini SDK / Antigravity -> local model).
app.include_router(gemini_router, prefix="/v1beta", tags=["gemini"])
# API-Call Debugging read API.
app.include_router(debug_router, prefix="/api/debug", tags=["debug"])
# Model Hub — discover / download / run models locally.
app.include_router(hub_router, prefix="/api/hub", tags=["hub"])
# Gateway info — powers the frontend Connect page.
app.include_router(gateway_info_router, prefix="/api/gateway", tags=["gateway"])
# Managed runtime — switch the active local model.
app.include_router(runtime_router, prefix="/api/runtime", tags=["runtime"])
# Auth — optional Team mode accounts/login/session (PRD-07). Default OFF (solo).
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
# User management — list/invite/role/remove (owner/admin); only used in Team mode.
app.include_router(users_router, prefix="/api/users", tags=["users"])
# API access tokens — keys agents use to authenticate to the gateway.
app.include_router(tokens_router, prefix="/api/tokens", tags=["tokens"])
# Agentic chat — local model + tools (web search/fetch), streamed.
app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
# Persistent chat sessions (local conversations).
app.include_router(sessions_router, prefix="/api/sessions", tags=["sessions"])
# Agents — local agents (persona + tools + skills).
app.include_router(agents_router, prefix="/api/agents", tags=["agents"])
# Agent inbox — flow-back notifications from delegated work.
app.include_router(inbox_router, prefix="/api/agents", tags=["inbox"])
# Providers — external LLM provider catalog + BYOK/subscription credentials.
app.include_router(providers_router, prefix="/api/providers", tags=["providers"])

app.include_router(memory_router, prefix="/api/agents", tags=["memory"])
# Working-dir file serving — lets the UI render an ![](...)  image a tool wrote.
app.include_router(agent_files_router, prefix="/api/agents", tags=["agent-files"])
# Intern — task board (Kanban) + activity feed / standup per agent.
app.include_router(tasks_router, prefix="/api/agents", tags=["tasks"])
app.include_router(activity_router, prefix="/api/agents", tags=["activity"])
app.include_router(duty_router, prefix="/api/agents", tags=["duty"])
# Skills — upload/manage SKILL.md packs.
app.include_router(skills_router, prefix="/api/skills", tags=["skills"])
# Knowledge — local Knowledge Bases + RAG retrieval (PRD-03).
app.include_router(kb_router, prefix="/api/kb", tags=["knowledge"])
app.include_router(wiki_router, prefix="/api/wiki", tags=["wiki"])
# Templates — one-click deployable worker blueprints (gallery + deploy + export/import).
app.include_router(templates_router, prefix="/api/templates", tags=["templates"])
# Git-backed template registries — explicitly synced, read-only gallery sources.
app.include_router(registries_router, prefix="/api/registries", tags=["registries"])
# MCP servers — connect agents to external tool providers.
app.include_router(mcp_router, prefix="/api/mcp", tags=["mcp"])
# Custom tools — give agents new capabilities by declaration (HTTP, first-class).
app.include_router(tools_router, prefix="/api/tools", tags=["tools"])
# Inference engines — versions + updates (llama.cpp, mlx-lm, mlx-vlm, mlx).
app.include_router(engines_router, prefix="/api/engines", tags=["engines"])
# Monitoring — live memory/CPU/disk, runtime health, recent errors + log tail.
app.include_router(monitor_router, prefix="/api/monitor", tags=["monitor"])
# First-run setup — status + recommendations for the onboarding wizard.
app.include_router(onboarding_router, prefix="/api/onboarding", tags=["onboarding"])
# Analytics — historical cost/usage over the captured debug facts (PRD-06).
app.include_router(analytics_router, prefix="/api/analytics", tags=["analytics"])
# Schedules — timed agent runs (cron / one-time).
app.include_router(schedule_router, prefix="/api/agents", tags=["schedules"])
# Delivery — outbound channels + audit log (in-app chat, desktop, webhook).
app.include_router(delivery_router, prefix="/api", tags=["delivery"])
# Triggers — event-driven agent runs (file watch / inbound webhook).
app.include_router(triggers_router, prefix="/api/triggers", tags=["triggers"])
# Chat channels — message agents from WhatsApp / Telegram; accounts, pairing, webhooks.
app.include_router(channels_router, prefix="/api/channels", tags=["channels"])
# Teams — multi-agent org view + combined delegation feed (PRD-02).
app.include_router(team_router, prefix="/api/team", tags=["team"])
# Backup, Sync & Portability — export/import a portable .vwbak studio snapshot.
app.include_router(backup_router, prefix="/api/backup", tags=["backup"])
# Browser control — pop the persistent visible browser open to log in once.
app.include_router(browser_router, prefix="/api/browser", tags=["browser"])


@app.get("/health")
async def health():
    return {"status": "ok", "app": "virtual-worker"}


# --- Serve the built frontend (packaged / production) ---------------------- #
# In dev the Vite server hosts the UI and proxies /api → the configured backend
# port. In a packaged
# app there's no Vite: the backend serves the built SPA itself so everything is
# single-origin on one port. Mounted LAST so all /api, /v1, /health, /docs routes
# win first; the SPA fallback returns index.html for any other path.
def _static_dir():
    import config
    for cand in (
        os.environ.get("VW_STATIC_DIR"),
        config.RESOURCES_DIR / "frontend",          # packaged layout
        config.RESOURCES_DIR.parent / "frontend" / "dist",
        Path(__file__).resolve().parent.parent / "frontend" / "dist",  # dev build
    ):
        if cand and Path(cand).is_dir() and (Path(cand) / "index.html").exists():
            return Path(cand)
    return None


def _safe_static_file(root: Path, full_path: str) -> Path | None:
    """Resolve full_path under root, returning the file only if it stays inside
    root and exists. Confines the SPA fallback so an encoded "../" can't read
    arbitrary files off disk (this handler serves on the gateway's port)."""
    if not full_path:
        return None
    f = (root / full_path).resolve()
    if f.is_file() and f.is_relative_to(root):
        return f
    return None


_STATIC = _static_dir()
if _STATIC is not None:
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    # Hashed asset files (JS/CSS/images) served straight from disk.
    app.mount("/assets", StaticFiles(directory=_STATIC / "assets"), name="assets")

    _STATIC_ROOT = _STATIC.resolve()

    @app.get("/{full_path:path}")
    async def _spa(full_path: str):
        # SPA fallback: any non-API path returns index.html (client-side nav).
        f = _safe_static_file(_STATIC_ROOT, full_path)
        if f is not None:
            return FileResponse(f)
        return FileResponse(_STATIC_ROOT / "index.html")
    logging.getLogger(__name__).info("serving built frontend from %s", _STATIC)
