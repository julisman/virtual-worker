"""Maintenance loop — keeps an always-on instance bounded.

Runs once shortly after startup and then every ``MAINTENANCE_INTERVAL_S``:

  • Debug traces older than ``TRACE_RETENTION_DAYS`` (and beyond ``TRACE_MAX_RUNS``)
  • Activity rows older than ``ACTIVITY_RETENTION_DAYS``
  • Delivery audit rows older than ``DELIVERY_RETENTION_DAYS``
  • Channel dedup ids older than ``CHAT_EVENT_RETENTION_DAYS``
  • Received channel media files older than ``CHANNEL_MEDIA_RETENTION_DAYS``
  • A WAL checkpoint so the database file doesn't grow unbounded

Every step is best-effort and independent; a failure is logged and the next
step still runs. ``run_once()`` is synchronous and testable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, days))).isoformat()


def prune_channel_media(days: int) -> int:
    """Delete files under ``<VW_HOME>/channels/*/media`` older than ``days``."""
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


def run_once() -> dict:
    """One maintenance pass. Returns per-step counts (or ``error`` strings)."""
    out: dict = {}

    def step(name, fn):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance step %s failed: %s", name, exc)
            out[name] = f"error: {exc}"

    from storage import activity_store, channel_store, chat_channel_store, debug_db

    step("traces", lambda: debug_db.prune(older_than_iso=_cutoff(config.TRACE_RETENTION_DAYS),
                                          max_runs=config.TRACE_MAX_RUNS))
    step("activity", lambda: activity_store.prune(_cutoff(config.ACTIVITY_RETENTION_DAYS)))
    step("deliveries", lambda: channel_store.prune_deliveries(_cutoff(config.DELIVERY_RETENTION_DAYS)))
    step("chatEvents", lambda: chat_channel_store.prune_events(days=config.CHAT_EVENT_RETENTION_DAYS))
    step("media", lambda: prune_channel_media(config.CHANNEL_MEDIA_RETENTION_DAYS))

    def checkpoint():
        conn = sqlite3.connect(str(config.db_path()))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        return True

    step("checkpoint", checkpoint)
    return out


class MaintenanceManager:
    def __init__(self, interval_s: int | None = None, initial_delay_s: int = 60):
        self.interval_s = interval_s or config.MAINTENANCE_INTERVAL_S
        self.initial_delay_s = initial_delay_s
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(self._run())
            logger.info("maintenance loop started (every %ss)", self.interval_s)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(self.initial_delay_s)
        while True:
            try:
                result = await asyncio.to_thread(run_once)
                logger.info("maintenance: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("maintenance pass failed", exc_info=True)
            await asyncio.sleep(self.interval_s)


maintenance = MaintenanceManager()
