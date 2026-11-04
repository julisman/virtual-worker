"""The always-on worker — an intern that pulls tasks and works them itself.

While an agent is **on duty**, a per-agent background loop pulls the next `todo`
task, runs it through the harness (same compose as chat), and lets the agent
finish by moving the task to `done` (or `blocked`). It works **one task at a
time**, with no human in the loop.

Autonomy is bounded by *automatic guardrails*, not a review gate (decision: skip
M2, autonomous-within-workspace):
  - duty defaults **off**; nothing runs until the mentor presses Start (D2),
  - a **kill switch** — setting duty off cancels the loop promptly,
  - **caps** — MAX_ITERATIONS per task (in the loop) + a daily pass cap,
  - **idle backoff** when there are no `todo` tasks,
  - the shell/file deny-list + working-folder confinement (in the tools).

See docs/INTERN_AGENT_PRD.md milestone M3.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from agent import loop, verify
from agent.compose import compose
from gateway.models import resolve_model
from storage import activity_store, agent_store, task_store

logger = logging.getLogger(__name__)

IDLE_BACKOFF_S = 15          # how long to wait when there are no todo tasks
ERROR_BACKOFF_S = 30         # back off after an upstream/model error
MAX_PASSES_PER_DAY = 60      # daily cap on harness passes per agent

_WORKER_DIRECTIVE = (
    "You are working AUTONOMOUSLY on exactly one assigned task — no human will "
    "approve your steps. Use your tools (files, shell, web, memory, task_*) to "
    "complete it. Work inside your working folder. When the task is finished, call "
    "task_update with status='done' and a clear result_summary. If you truly "
    "cannot proceed, call task_update with status='blocked' and a note saying why. "
    "Be decisive and finish within this session. Your finished work is checked by a "
    "reviewer before it's accepted, so make sure it is actually complete and correct."
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class WorkerManager:
    """Owns the background loops, one per on-duty agent."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._current: dict[str, dict | None] = {}
        self._passes: dict[str, dict] = {}  # agent_id -> {"day": str, "n": int}

    # ---- lifecycle ---- #

    def is_running(self, agent_id: str) -> bool:
        t = self._tasks.get(agent_id)
        return bool(t and not t.done())

    def current(self, agent_id: str) -> dict | None:
        return self._current.get(agent_id)

    async def start(self, agent_id: str) -> None:
        if self.is_running(agent_id):
            return
        agent_store.update_agent(agent_id, {"dutyStatus": "idle"})
        activity_store.log(agent_id, "status_change", "On duty — worker started.")
        self._tasks[agent_id] = asyncio.create_task(self._run(agent_id))

    async def stop(self, agent_id: str) -> None:
        t = self._tasks.pop(agent_id, None)
        self._current.pop(agent_id, None)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        agent_store.update_agent(agent_id, {"dutyStatus": "off"})
        activity_store.log(agent_id, "status_change", "Off duty — worker stopped.")

    async def stop_all(self) -> None:
        for agent_id in list(self._tasks):
            await self.stop(agent_id)

    # ---- the loop ---- #

    def _bump_passes(self, agent_id: str) -> int:
        p = self._passes.get(agent_id)
        if not p or p["day"] != _today():
            p = {"day": _today(), "n": 0}
        p["n"] += 1
        self._passes[agent_id] = p
        return p["n"]

    async def _run(self, agent_id: str) -> None:
        try:
            while True:
                agent = agent_store.get_agent(agent_id)
                if not agent or (agent.get("dutyStatus") or "off") == "off":
                    return

                daily_cap = (agent.get("guardrails") or {}).get("dailyCap", MAX_PASSES_PER_DAY)
                if self._bump_passes(agent_id) > daily_cap:
                    agent_store.update_agent(agent_id, {"dutyStatus": "paused"})
                    activity_store.log(agent_id, "status_change",
                                       f"Paused — hit the daily cap of {daily_cap} passes.")
                    return

                task = task_store.next_todo(agent_id)
                if not task:
                    self._current[agent_id] = None
                    agent_store.update_agent(agent_id, {"dutyStatus": "idle"})
                    await asyncio.sleep(IDLE_BACKOFF_S)
                    continue

                await self._work_one(agent, task)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("worker loop crashed for %s", agent_id, exc_info=True)
            try:
                agent_store.update_agent(agent_id, {"dutyStatus": "off"})
                activity_store.log(agent_id, "error", "Worker loop crashed — set off duty.")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._current.pop(agent_id, None)

    async def _work_one(self, agent: dict, task: dict) -> None:
        agent_id = agent["id"]
        task_id = task["id"]
        task_store.update(agent_id, task_id, {"status": "in_progress"})
        self._current[agent_id] = {"taskId": task_id, "title": task["title"]}
        agent_store.update_agent(agent_id, {"dutyStatus": "working"})
        activity_store.log(agent_id, "task_claimed", f"Picked up: {task['title']}", task_id=task_id)

        try:
            model = agent.get("model") or await resolve_model(None)
        except Exception as exc:  # noqa: BLE001
            model = None
            logger.debug("resolve_model failed: %s", exc)
        if not model:
            task_store.update(agent_id, task_id, {"status": "blocked"})
            task_store.add_comment(task_id, "intern", "No active model — activate one in Models, then set me on duty.")
            activity_store.log(agent_id, "error", "No active model; task blocked.", task_id=task_id)
            await asyncio.sleep(ERROR_BACKOFF_S)
            return

        guardrails = agent.get("guardrails") or {}
        system, registry, temperature, top_p, top_k = compose(agent, extra_system=_WORKER_DIRECTIVE)
        messages = [{
            "role": "user",
            "content": (f"Work this task now.\n\nTitle: {task['title']}\n"
                        f"Task id: {task_id}\n\n{task.get('description') or '(no description)'}"),
        }]

        err = None
        try:
            async for ev in loop.run(messages, model, registry, temperature,
                                     session_id=f"worker:{agent_id}", system=system,
                                     top_p=top_p, top_k=top_k, agent_id=agent_id, task_id=task_id,
                                     max_iterations=guardrails.get("maxIterations")):
                if ev.get("type") == "error":
                    err = ev.get("message")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            err = str(exc)

        # Reconcile: the agent should have closed the task itself. If it didn't,
        # park it so it isn't silently stuck and won't be re-pulled.
        after = task_store.get(agent_id, task_id)
        if err:
            task_store.update(agent_id, task_id, {"status": "blocked"})
            task_store.add_comment(task_id, "intern", f"Hit an error: {err[:300]}")
            activity_store.log(agent_id, "error", f"Task errored: {err[:200]}", task_id=task_id)
            await asyncio.sleep(ERROR_BACKOFF_S)
        elif after and after["status"] == "in_progress":
            task_store.update(agent_id, task_id, {"status": "blocked"})
            task_store.add_comment(task_id, "intern", "Finished a work pass without closing the task — needs attention.")
            activity_store.log(agent_id, "task_moved", f"{task['title']}: parked (not closed)", task_id=task_id,
                               detail={"from": "in_progress", "to": "blocked"})
        elif after and after["status"] == "done" and guardrails.get("verify", True):
            # Verify pillar — don't take the agent's word for it (unless disabled).
            await self._verify(agent_id, model, after)

    async def _verify(self, agent_id: str, model: str, task: dict) -> None:
        """Closing pillar: confirm a 'done' task really is done, or send it back."""
        task_id = task["id"]
        if verify.attempts(task) >= verify.MAX_VERIFY_ATTEMPTS:
            activity_store.log(agent_id, "verify",
                               f"Accepted after {verify.MAX_VERIFY_ATTEMPTS} verify attempts: {task['title']}",
                               task_id=task_id)
            return
        recent = activity_store.list_for(agent_id, task_id=task_id, limit=12)
        context = "Recent actions:\n" + "\n".join(f"- {a['type']}: {a['summary']}" for a in reversed(recent))
        ok, reason = await verify.verify_task(model, task, context=context)
        if ok:
            activity_store.log(agent_id, "verify", f"Verified: {task['title']} — {reason}", task_id=task_id)
        else:
            verify.record_attempt(agent_id, task)
            task_store.update(agent_id, task_id, {"status": "todo"})
            task_store.add_comment(task_id, "intern", f"Verification failed — sending back: {reason}")
            activity_store.log(agent_id, "verify", f"Sent back (needs work): {reason}", task_id=task_id,
                               detail={"from": "done", "to": "todo"})


# Process-wide singleton.
manager = WorkerManager()
