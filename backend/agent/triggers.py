"""The trigger engine — fires agent runs when an event happens (PRD-09, M1).

The event-driven sibling of agent/scheduler.py. One always-on background loop
(started in app.py) ticks every ~10s and polls each enabled ``file`` trigger for
new/changed files, debouncing repeats via a stored fingerprint so one real-world
change causes exactly one run. ``webhook`` triggers are push-based: an inbound
``POST /api/triggers/hook/{token}`` calls ``fire_hook`` directly.

Either way a fire runs the trigger's agent prompt through the same
``compose()`` + ``loop.run`` path the scheduler and chat use, so a triggered run
behaves exactly like a chat turn. Output is recorded on the trigger and logged to
the agent's activity feed; there is no auto-delivery, so a trigger can't leak to a
frozen recipient (same safety posture as the scheduler).

Deferred (noted, not built here): email-in / monitor-threshold triggers and
multi-step workflow chaining — a trigger fires one agent + prompt for now.
"""

from __future__ import annotations

import asyncio
import json
import logging

from storage import activity_store, agent_store, trigger_store

logger = logging.getLogger(__name__)

_TRIGGER_DIRECTIVE = (
    "You are running as an EVENT-TRIGGERED, autonomous turn — no human is watching "
    "this run live. An event happened (shown below); handle it using the tools you "
    "have, then stop. If the task is to notify or message someone, use an available "
    "tool to do it; your text output here is recorded but is NOT automatically sent "
    "anywhere."
)


def _render_prompt(template: str, event_context: str) -> str:
    """Build the user message: substitute ``{{event}}`` if present, else append."""
    template = template or ""
    if "{{event}}" in template:
        return template.replace("{{event}}", event_context)
    if event_context.strip():
        return f"{template}\n\n--- Event ---\n{event_context}".strip()
    return template


def detect_file_changes(trigger: dict) -> list[str]:
    """New/changed files for a ``file`` trigger since its last poll.

    Reads the watched path, diffs against the trigger's stored fingerprint, and
    persists the new fingerprint so the same change won't fire twice (dedup).
    Returns the changed file paths (possibly empty). Pure w.r.t. the model — used
    by the background loop and directly by tests.
    """
    if trigger.get("type") != "file":
        return []
    path = trigger.get("path") or ""
    prev = trigger_store.get_watch_state(trigger["id"])
    cur = trigger_store.snapshot_dir(path)
    changed = trigger_store.diff_snapshot(prev, cur)
    if cur != prev:
        trigger_store.set_watch_state(trigger["id"], cur)
    return changed


async def execute_run(agent: dict, prompt: str, session_id: str) -> tuple[str, str | None]:
    """Run ``prompt`` through the agent's composed runtime; return (text, error).

    Isolated as a module function so tests can monkeypatch it (no model needed).
    """
    # Lazy imports break the compose↔triggers import cycle.
    from agent import loop
    from agent.compose import compose
    from gateway.models import resolve_model

    model = agent.get("model") or await resolve_model(None)
    system, registry, temperature, top_p, top_k = compose(agent, extra_system=_TRIGGER_DIRECTIVE)
    messages = [{"role": "user", "content": prompt}]
    g = (agent.get("guardrails") or {})

    text_parts: list[str] = []
    err: str | None = None
    async for ev in loop.run(
        messages, model, registry, temperature,
        session_id=session_id, system=system,
        top_p=top_p, top_k=top_k, agent_id=agent.get("id"),
        max_iterations=g.get("maxIterations"),
    ):
        t = ev.get("type")
        if t == "text":
            text_parts.append(ev.get("delta", ""))
        elif t == "error":
            err = ev.get("message") or "run failed"
    return "".join(text_parts).strip(), err


class TriggerManager:
    def __init__(self, tick_seconds: int = 10):
        self._tick = tick_seconds
        self._task: asyncio.Task | None = None
        self._running = False
        self._inflight: set[str] = set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("trigger engine started (tick=%ss)", self._tick)

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
        # Let startup (model adopt, etc.) settle before the first poll.
        await asyncio.sleep(6)
        while self._running:
            try:
                self._poll_file_triggers()
            except Exception:  # noqa: BLE001
                logger.warning("trigger poll failed", exc_info=True)
            await asyncio.sleep(self._tick)

    def _poll_file_triggers(self) -> None:
        for trig in trigger_store.list_enabled("file"):
            if trig["id"] in self._inflight:
                continue
            try:
                changed = detect_file_changes(trig)
            except Exception:  # noqa: BLE001
                logger.warning("file trigger %s scan failed", trig["id"], exc_info=True)
                continue
            if not changed:
                continue
            n = len(changed)
            shown = ", ".join(changed[:10]) + ("…" if n > 10 else "")
            event = f"{n} file change(s) in {trig.get('path')}: {shown}"
            self._inflight.add(trig["id"])
            asyncio.create_task(self._fire(trig, event))

    async def run_now(self, trigger: dict, event_context: str = "") -> dict:
        """Fire a trigger immediately (the 'Run now' / test-fire button)."""
        ctx = event_context or "Manual test-fire — no real event payload."
        await self._fire(trigger, ctx)
        return trigger_store.get(trigger["id"]) or trigger

    async def fire_hook(self, token: str, payload) -> dict | None:
        """Resolve a webhook trigger by token and fire it with the request body."""
        trig = trigger_store.get_by_token(token)
        if not trig or trig["type"] != "webhook":
            return None
        if not trig["enabled"]:
            return trig
        if isinstance(payload, (dict, list)):
            ctx = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            ctx = str(payload or "")
        event = f"Inbound webhook payload:\n{ctx}"
        await self._fire(trig, event)
        return trigger_store.get(trig["id"]) or trig

    async def _fire(self, trigger: dict, event_context: str) -> None:
        tid = trigger["id"]
        self._inflight.add(tid)
        agent_id = trigger["agentId"]
        name = trigger["name"]
        err: str | None = None
        result = ""
        try:
            agent = agent_store.get_agent(agent_id)
            if not agent:
                err = "agent no longer exists"
            else:
                activity_store.log(agent_id, "note", f"⚡ Trigger fired: {name}",
                                   detail={"triggerId": tid, "event": event_context[:300]})
                prompt = _render_prompt(trigger["promptTemplate"], event_context)
                result, err = await execute_run(agent, prompt, session_id=f"trigger:{tid}")
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            logger.warning("trigger %s failed", tid, exc_info=True)
        finally:
            self._inflight.discard(tid)
            status = "error" if err else "ok"
            trigger_store.record_fire(tid, status, result=result, error=err or "",
                                      event=event_context)
            if err:
                activity_store.log(agent_id, "error",
                                   f"⚡ Trigger failed: {name} — {err[:160]}",
                                   detail={"triggerId": tid})
            else:
                snippet = result[:160] + ("…" if len(result) > 160 else "")
                activity_store.log(agent_id, "note",
                                   f"⚡ Trigger done: {name}" + (f" — {snippet}" if snippet else ""),
                                   detail={"triggerId": tid})


# Module singleton — started/stopped from app.py.
triggers = TriggerManager()
