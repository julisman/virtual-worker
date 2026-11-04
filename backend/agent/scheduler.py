"""The scheduler — fires agent runs at their scheduled times.

One always-on background loop (started in app.py) ticks every ~30s, asks
``schedule_store`` what's due (local time), and runs each due schedule's prompt
through the normal agent loop — same compose + tool registry the chat route and
the worker use, so a scheduled run behaves exactly like a chat turn. Output is
recorded on the schedule and logged to the agent's activity feed; there is no
auto-delivery, so a recurring schedule can't leak to a frozen recipient.

Also exposes ``make_schedule_tools`` so an agent can schedule its own runs
("remind me at 3pm to …"). That factory deliberately imports nothing heavy, so
compose.py can import it without a cycle; the run path imports compose/loop
lazily inside ``_fire``.
"""

from __future__ import annotations

import asyncio
import logging

from storage import activity_store, agent_store, schedule_store

logger = logging.getLogger(__name__)

_SCHEDULE_DIRECTIVE = (
    "You are running as a SCHEDULED, autonomous turn — no human is watching this "
    "run live. Do the task below using the tools you have, then stop. If the task "
    "is to notify or message someone, use an available tool to do it; your text "
    "output here is recorded but is NOT automatically sent anywhere."
)


# --------------------------------------------------------------------------- #
# Agent-facing tools (no heavy imports — safe for compose.py to import)
# --------------------------------------------------------------------------- #

def make_schedule_tools(agent_id: str) -> dict[str, dict]:
    """``{name: {schema, fn}}`` for schedule_create / schedule_list / schedule_delete."""

    async def schedule_create(title: str = "", prompt: str = "",
                              cron: str = "", at: str = "") -> str:
        title = (title or "").strip()
        prompt = (prompt or "").strip()
        if not prompt:
            return "schedule_create needs 'prompt' — the instruction to run at the scheduled time."
        try:
            if cron.strip():
                s = schedule_store.create(agent_id, title or prompt[:40], prompt,
                                          kind="cron", cron_expr=cron.strip())
            elif at.strip():
                s = schedule_store.create(agent_id, title or prompt[:40], prompt,
                                          kind="at", run_at=at.strip())
            else:
                return "Provide either 'cron' (e.g. '0 17 * * *' for 5pm daily) or 'at' (ISO datetime)."
        except ValueError as exc:
            return f"could not create schedule: {exc}"
        when = s["cronText"] or f"once at {s['runAt']}"
        return f"scheduled '{s['title']}' — {when} (next run {s['nextRunAt']}). id={s['id']}"

    async def schedule_list() -> str:
        rows = schedule_store.list_for_agent(agent_id)
        if not rows:
            return "no schedules yet."
        out = []
        for s in rows:
            when = s["cronText"] or f"once at {s['runAt']}"
            state = "on" if s["enabled"] else "off"
            nxt = f" next {s['nextRunAt']}" if s["nextRunAt"] else ""
            out.append(f"- ({s['id']}) {s['title']} — {when} [{state}]{nxt}")
        return "\n".join(out)

    async def schedule_delete(id: str = "") -> str:
        if not id:
            return "schedule_delete needs the schedule 'id' (from schedule_list)."
        return f"deleted {id}" if schedule_store.delete(id) else f"no schedule with id {id!r}"

    return {
        "schedule_create": {
            "fn": schedule_create,
            "schema": {
                "type": "function",
                "function": {
                    "name": "schedule_create",
                    "description": (
                        "Schedule a prompt to run later — recurring with a cron expression, or "
                        "once at a specific time. The scheduled run executes with your tools; its "
                        "text is recorded but not auto-sent, so include a sending tool in 'prompt' "
                        "if it must reach someone. Times use the computer's local time."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "short label, e.g. 'daily-summary'"},
                            "prompt": {"type": "string", "description": "the instruction to run at fire time"},
                            "cron": {"type": "string", "description": "5-field cron for recurring runs, e.g. '0 14 * * *' = 2pm daily, '0 9 * * 1-5' = 9am weekdays"},
                            "at": {"type": "string", "description": "ISO datetime for a one-time run, e.g. '2026-06-29T15:00'"},
                        },
                        "required": ["prompt"],
                    },
                },
            },
        },
        "schedule_list": {
            "fn": schedule_list,
            "schema": {
                "type": "function",
                "function": {
                    "name": "schedule_list",
                    "description": "List your scheduled runs with their timing, on/off state, and next run.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        },
        "schedule_delete": {
            "fn": schedule_delete,
            "schema": {
                "type": "function",
                "function": {
                    "name": "schedule_delete",
                    "description": "Delete one of your schedules by id (from schedule_list).",
                    "parameters": {
                        "type": "object",
                        "properties": {"id": {"type": "string", "description": "the schedule id"}},
                        "required": ["id"],
                    },
                },
            },
        },
    }


# --------------------------------------------------------------------------- #
# Background loop
# --------------------------------------------------------------------------- #

class SchedulerManager:
    def __init__(self, tick_seconds: int = 30):
        self._tick = tick_seconds
        self._task: asyncio.Task | None = None
        self._running = False
        self._inflight: set[str] = set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("scheduler started (tick=%ss)", self._tick)

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
        # Let startup (model adopt, etc.) settle before the first tick.
        await asyncio.sleep(5)
        while self._running:
            try:
                for s in schedule_store.list_due():
                    if s["id"] not in self._inflight:
                        self._inflight.add(s["id"])
                        asyncio.create_task(self._fire(s))
            except Exception:  # noqa: BLE001
                logger.warning("scheduler tick failed", exc_info=True)
            await asyncio.sleep(self._tick)

    async def run_now(self, schedule: dict) -> dict:
        """Fire a schedule immediately (used by the 'Run now' button)."""
        await self._fire(schedule)
        return schedule_store.get(schedule["id"]) or schedule

    async def _fire(self, schedule: dict) -> None:
        sid = schedule["id"]
        self._inflight.add(sid)
        agent_id = schedule["agentId"]
        title = schedule["title"]
        text_parts: list[str] = []
        err: str | None = None
        try:
            agent = agent_store.get_agent(agent_id)
            if not agent:
                schedule_store.record_run(sid, "error", error="agent no longer exists")
                return

            # Lazy imports break the compose↔scheduler cycle.
            from agent import loop
            from agent.compose import compose
            from gateway.models import resolve_model

            model = agent.get("model") or await resolve_model(None)
            system, registry, temperature, top_p, top_k = compose(agent, extra_system=_SCHEDULE_DIRECTIVE)
            messages = [{"role": "user", "content": schedule["prompt"]}]
            g = (agent.get("guardrails") or {})

            activity_store.log(agent_id, "note", f"⏰ Scheduled run started: {title}",
                               detail={"scheduleId": sid})

            async for ev in loop.run(
                messages, model, registry, temperature,
                session_id=f"schedule:{sid}", system=system,
                top_p=top_p, top_k=top_k, agent_id=agent_id,
                max_iterations=g.get("maxIterations"),
            ):
                t = ev.get("type")
                if t == "text":
                    text_parts.append(ev.get("delta", ""))
                elif t == "error":
                    err = ev.get("message") or "run failed"
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            logger.warning("schedule %s failed", sid, exc_info=True)
        finally:
            self._inflight.discard(sid)
            result = "".join(text_parts).strip()
            status = "error" if err else "ok"
            schedule_store.record_run(sid, status, result=result, error=err or "")
            if err:
                activity_store.log(agent_id, "error", f"⏰ Scheduled run failed: {title} — {err[:160]}",
                                   detail={"scheduleId": sid})
            else:
                snippet = result[:160] + ("…" if len(result) > 160 else "")
                activity_store.log(agent_id, "note", f"⏰ Scheduled run done: {title}"
                                   + (f" — {snippet}" if snippet else ""), detail={"scheduleId": sid})
                # Outbound delivery (PRD-01): fan the result to each explicit
                # target stored on the schedule. Targets are read fresh from the
                # schedule config here — there is no captured/last-seen recipient
                # path, so a recurring job can't leak to a frozen sender (#648).
                await self._deliver_result(schedule, result)

    async def _deliver_result(self, schedule: dict, result: str) -> None:
        targets = schedule.get("deliverTo") or []
        if not targets or not result:
            return
        from agent.delivery import deliver
        agent_id = schedule["agentId"]
        for t in targets:
            channel = (t.get("channel") or "").strip()
            target = (t.get("target") or "").strip()  # explicit, from config
            if not channel:
                continue
            try:
                rec = await deliver(channel, target, result, subject=schedule.get("title"),
                                    agent_id=agent_id, source="schedule",
                                    schedule_id=schedule["id"])
                if rec["status"] != "sent":
                    activity_store.log(
                        agent_id, "error",
                        f"⏰ Delivery via {channel} {rec['status']}: {(rec['error'] or '')[:140]}",
                        detail={"scheduleId": schedule["id"], "deliveryId": rec["id"]})
            except Exception:  # noqa: BLE001
                logger.warning("schedule %s delivery via %s failed", schedule["id"], channel, exc_info=True)


# Module singleton — started/stopped from app.py.
scheduler = SchedulerManager()
