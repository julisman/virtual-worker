"""Periodic housekeeping for the always-on instance.

Bounds the database and on-disk media so a long-running worker never grows
without limit: the Debug trace store (full prompts + tool results on every model
call), the append-only activity feed, the delivery audit log, chat-channel
dedup ids, expired originals of compressed tool results, received chat
attachments, and finished download jobs. It also writes the opt-in scheduled
local backup snapshot when one is due. Started/stopped from app.py's lifespan
alongside the scheduler/trigger loops. Best-effort: a prune failure is logged
and never crashes the loop. ``prune_once()`` is synchronous and testable.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import config
from storage import activity_store, ccr_store, channel_store, chat_channel_store, debug_db

logger = logging.getLogger(__name__)


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, days))).isoformat()


def prune_channel_media(days: int) -> int:
    """Delete files under ``<home>/channels/*/media`` older than ``days``."""
    root = config.APP_HOME / "channels"
    if not root.exists() or days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for media_dir in root.glob("*/media"):
        for f in media_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def prune_once() -> dict:
    """One synchronous housekeeping pass. Returns per-step counts (or errors)."""
    out: dict = {}

    def step(name, fn):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance step %s failed: %s", name, exc)
            out[name] = f"error: {exc}"

    step("debug", lambda: debug_db.prune(max_age_days=config.DEBUG_RETENTION_DAYS,
                                         max_rows=config.DEBUG_MAX_ROWS))
    step("activities", lambda: activity_store.prune(max_age_days=config.ACTIVITY_RETENTION_DAYS,
                                                    max_rows=config.ACTIVITY_MAX_ROWS))
    step("deliveries", lambda: channel_store.prune_deliveries(_cutoff(config.DELIVERY_RETENTION_DAYS)))
    step("chatEvents", lambda: chat_channel_store.prune_events(days=config.CHAT_EVENT_RETENTION_DAYS))
    step("media", lambda: prune_channel_media(config.CHANNEL_MEDIA_RETENTION_DAYS))
    step("ccr", ccr_store.purge_expired)

    def audit():
        from storage import audit_store
        return audit_store.prune(config.AUDIT_RETENTION_DAYS)
    step("audit", audit)

    def jobs():
        from routes.hub import evict_finished_jobs
        return evict_finished_jobs()
    step("jobs", jobs)

    def scheduled_backup():
        from storage import backup
        return backup.scheduled_backup_once().get("ran", False)
    step("backup", scheduled_backup)

    def checkpoint():
        conn = sqlite3.connect(str(config.db_path()))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        return True
    step("checkpoint", checkpoint)
    if any(isinstance(v, int) and v for v in out.values()):
        logger.info("maintenance: %s", out)
    return out


run_once = prune_once  # alias


class MaintenanceLoop:
    def __init__(self, interval_s: int | None = None, initial_delay_s: int = 60):
        self._interval = interval_s or config.MAINTENANCE_INTERVAL_S
        self._initial = initial_delay_s
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("maintenance loop started (every %ss)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(self._initial)  # let startup settle; prune once soon after boot
        while self._running:
            try:
                await asyncio.to_thread(prune_once)
            except Exception:  # noqa: BLE001
                logger.warning("maintenance prune failed", exc_info=True)
            await asyncio.sleep(self._interval)


maintenance = MaintenanceLoop()
MaintenanceManager = MaintenanceLoop  # alias
