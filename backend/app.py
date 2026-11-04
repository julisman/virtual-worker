"""Virtual Worker — FastAPI application entry point.

Run from this directory:  uvicorn app:app --port 8888 --reload
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

import config

from middleware.api_debug import ApiDebugMiddleware
from middleware.team_auth import TeamAuthMiddleware
from routes.activity import router as activity_router
from routes.agents import router as agents_router
from routes.analytics import router as analytics_router
from routes.anthropic import router as anthropic_router
from routes.auth import router as auth_router
from routes.backup import router as backup_router
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
from routes.mcp import router as mcp_router
from routes.memory import router as memory_router
from routes.monitor import router as monitor_router
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

app = FastAPI(title="Virtual Worker", version="0.1.0")

# Team-mode auth gate. Added FIRST so it ends up INNERMOST — CORS then wraps its
# 401s with the right headers. In solo mode (default) it's a transparent no-op.
app.add_middleware(TeamAuthMiddleware)
# CORS (added before -> inner), debug capture last (added after -> OUTERMOST
# so it tees the exact bytes the client receives, without blocking streaming).
# Origins: local only by default (+ VW_CORS_ORIGINS) — never "*", so a web page
# the user happens to visit cannot drive the API through their browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_origin_regex=config.CORS_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Host-header allow-list (DNS-rebinding guard): local names + VW_ALLOWED_HOSTS.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
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

app.include_router(memory_router, prefix="/api/agents", tags=["memory"])
# Intern — task board (Kanban) + activity feed / standup per agent.
app.include_router(tasks_router, prefix="/api/agents", tags=["tasks"])
app.include_router(activity_router, prefix="/api/agents", tags=["activity"])
app.include_router(duty_router, prefix="/api/agents", tags=["duty"])
# Skills — upload/manage SKILL.md packs.
app.include_router(skills_router, prefix="/api/skills", tags=["skills"])
# Knowledge — local Knowledge Bases + RAG retrieval (PRD-03).
app.include_router(kb_router, prefix="/api/kb", tags=["knowledge"])
# Templates — one-click deployable virtual worker blueprints (gallery + deploy + export/import).
app.include_router(templates_router, prefix="/api/templates", tags=["templates"])
# MCP servers — connect agents to external tool providers.
app.include_router(mcp_router, prefix="/api/mcp", tags=["mcp"])
# Custom tools — give agents new capabilities by declaration (HTTP, first-class).
app.include_router(tools_router, prefix="/api/tools", tags=["tools"])
# Inference engines — versions + updates (llama.cpp, mlx-lm, mlx-vlm, mlx).
app.include_router(engines_router, prefix="/api/engines", tags=["engines"])
# Monitoring — live memory/CPU/disk, runtime health, recent errors + log tail.
app.include_router(monitor_router, prefix="/api/monitor", tags=["monitor"])
# Analytics — historical cost/usage over the captured debug facts (PRD-06).
app.include_router(analytics_router, prefix="/api/analytics", tags=["analytics"])
# Schedules — timed agent runs (cron / one-time).
app.include_router(schedule_router, prefix="/api/agents", tags=["schedules"])
# Delivery — outbound channels + audit log (in-app chat, desktop, webhook).
app.include_router(delivery_router, prefix="/api", tags=["delivery"])
# Triggers — event-driven agent runs (file watch / inbound webhook).
app.include_router(triggers_router, prefix="/api/triggers", tags=["triggers"])
# Chat channels — message agents from WhatsApp (and more); accounts + webhooks.
app.include_router(channels_router, prefix="/api/channels", tags=["channels"])
# Teams — multi-agent org view + combined delegation feed (PRD-02).
app.include_router(team_router, prefix="/api/team", tags=["team"])
# Backup, Sync & Portability — export/import a portable .vwbak studio snapshot.
app.include_router(backup_router, prefix="/api/backup", tags=["backup"])


@app.on_event("startup")
async def _startup():
    # Adopt a llama-server that survived a reload, else autostart the default.
    try:
        await llama_manager.adopt_or_autostart()
    except Exception:
        logging.getLogger(__name__).warning("llama adopt/autostart failed", exc_info=True)
    # Interns never resume autonomous work on their own after a restart (D2):
    # reset any stale on-duty flag from a previous run back to off.
    try:
        from storage import agent_store
        for a in agent_store.list_agents():
            if (a.get("dutyStatus") or "off") != "off":
                agent_store.update_agent(a["id"], {"dutyStatus": "off"})
    except Exception:
        logging.getLogger(__name__).debug("duty reconcile failed", exc_info=True)
    # Scheduler — always-on; honors timed agent runs while the backend is up.
    try:
        from agent.scheduler import scheduler
        scheduler.start()
    except Exception:
        logging.getLogger(__name__).warning("scheduler start failed", exc_info=True)
    # Trigger engine — always-on; polls file triggers + serves webhook fires.
    try:
        from agent.triggers import triggers
        triggers.start()
    except Exception:
        logging.getLogger(__name__).warning("trigger engine start failed", exc_info=True)
    # Maintenance — keeps traces/activity/deliveries/media bounded (retention knobs in config).
    try:
        from runtime.maintenance import maintenance
        maintenance.start()
    except Exception:
        logging.getLogger(__name__).warning("maintenance start failed", exc_info=True)
    # Chat channels — start every enabled account's adapter (dials out; no inbound port).
    try:
        import channels.builtin  # noqa: F401  (registers adapters)
        from channels.manager import manager as channel_manager
        channel_manager.start()
    except Exception:
        logging.getLogger(__name__).warning("channel manager start failed", exc_info=True)


@app.on_event("shutdown")
async def _shutdown():
    try:
        from agent.scheduler import scheduler
        await scheduler.stop()
    except Exception:
        logging.getLogger(__name__).debug("scheduler stop failed", exc_info=True)
    try:
        from agent.triggers import triggers
        await triggers.stop()
    except Exception:
        logging.getLogger(__name__).debug("trigger engine stop failed", exc_info=True)
    try:
        from agent.worker import manager
        await manager.stop_all()
    except Exception:
        logging.getLogger(__name__).debug("worker stop_all failed", exc_info=True)
    try:
        from channels.manager import manager as channel_manager
        await channel_manager.stop()
    except Exception:
        logging.getLogger(__name__).debug("channel manager stop failed", exc_info=True)
    try:
        from runtime.maintenance import maintenance
        await maintenance.stop()
    except Exception:
        logging.getLogger(__name__).debug("maintenance stop failed", exc_info=True)


@app.get("/health")
async def health():
    return {"status": "ok", "app": "virtual-worker"}


# --- Built UI (service mode) ------------------------------------------------- #
# With VW_SERVE_UI=1 (set by `vw start`) and a frontend build present, serve the
# single-page app from this same origin so one port is the whole product. API,
# gateway and docs paths are registered above and therefore win; everything
# else falls back to index.html (client-side routing). Dev uses Vite instead.
import os as _os
from pathlib import Path as _Path

_UI_DIR = _Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _os.environ.get("VW_SERVE_UI", "0") not in ("0", "false", "False") and (_UI_DIR / "index.html").exists():
    from fastapi import HTTPException as _HTTPException
    from fastapi.responses import FileResponse as _FileResponse
    from fastapi.staticfiles import StaticFiles as _StaticFiles

    app.mount("/assets", _StaticFiles(directory=str(_UI_DIR / "assets")), name="ui-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def _spa(path: str):
        if path.startswith(("api/", "v1/", "v1beta/")) or path in ("docs", "openapi.json", "redoc"):
            raise _HTTPException(404)
        candidate = (_UI_DIR / path).resolve()
        if path and candidate.is_file() and str(candidate).startswith(str(_UI_DIR.resolve())):
            return _FileResponse(str(candidate))
        return _FileResponse(str(_UI_DIR / "index.html"))
