"""Drain agent inbox handoffs and deliver them into their origin conversation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from agent import runner
from storage import activity_store, agent_store, chat_store, inbox_store, task_store

logger = logging.getLogger(__name__)

# A delegated task flows back to its assigner on its first transition into any
# of these statuses. `review` counts as closed for flow-back: the assignee is
# done but wants a sign-off (the mentor still moves it to done on the board).
CLOSING_STATUSES = {"done", "blocked", "review"}


def flow_back_if_closed(agent_id: str, task_id: str) -> dict | None:
    """Queue one flow-back item for a delegated task that just reached a closing
    state. Safe to call repeatedly and from sync route/tool handlers — the
    ``flowBackAt`` marker keeps it to one notice per task. Returns the created
    inbox row, or ``None`` if nothing was enqueued. Never raises.
    """
    try:
        task = task_store.get(agent_id, task_id)
        if not task or task["status"] not in CLOSING_STATUSES:
            return None
        metadata = task.get("metadata") or {}
        reply_to = metadata.get("replyTo")
        if not reply_to or metadata.get("flowBackAt"):
            return None
        assigner_id = metadata.get("assignedBy")
        if not assigner_id:
            return None
        worker = agent_store.get_agent(agent_id) or {}
        assigner = agent_store.get_agent(assigner_id) or {}
        result_head = ""
        for line in (task.get("resultSummary") or "").splitlines():
            if line.strip():
                result_head = line.strip()[:300]
                break
        payload = {
            "taskId": task_id,
            "title": task.get("title") or "",
            "status": task["status"],
            "fromAgentName": worker.get("name") or agent_id,
            "resultHead": result_head,
        }
        item = inbox_store.create(assigner_id, {
            "fromAgentId": agent_id,
            "taskId": task_id,
            "originSessionId": reply_to.get("sessionId"),
            "kind": "task_closed",
            "payload": payload,
        })
        metadata = {**metadata, "flowBackAt": datetime.now(timezone.utc).isoformat()}
        task_store.update(agent_id, task_id, {"metadata": metadata})
        assigner_name = metadata.get("assignedByName") or assigner.get("name") or assigner_id
        activity_store.log(assigner_id, "delegation",
                           f"Flow-back queued to {assigner_name}", task_id=task_id)
        return item
    except Exception:  # noqa: BLE001
        logger.warning("flow-back finalize failed for %s/%s", agent_id, task_id,
                       exc_info=True)
        return None


def _first_summary(task: dict | None, payload: dict) -> str:
    summary = (task or {}).get("resultSummary") or ""
    for line in summary.splitlines():
        if line.strip():
            return line.strip()[:300]
    if task is not None:
        return "(no result summary)"
    return (payload.get("resultHead") or "").strip()[:300] or "(no result summary)"


def _event_text(item: dict, task: dict | None) -> str:
    payload = item.get("payload") or {}
    status = payload.get("status") or (task or {}).get("status") or "done"
    status = status if status in CLOSING_STATUSES else "done"
    from_name = payload.get("fromAgentName") or item.get("fromAgentId") or "agent"
    task_id = item.get("taskId") or payload.get("taskId") or "?"
    title = payload.get("title") or (task or {}).get("title") or ""
    return (
        f'[handoff result] {from_name} finished task {task_id} "{title}" — status: {status}\n'
        f"{_first_summary(task, payload)}\n"
        f'Use task_status("{task_id}") for the full result or relay_task_result("{task_id}") '
        "to forward it verbatim to this chat."
    )


# How far back to scan a session for an existing handoff bubble. A manual retry
# (POST /inbox/{id}/retry) resets attempts to 0, so an attempts-based guard would
# re-append the same bubble — gate on the session actually already carrying an
# `inbox` message for this task instead.
_INBOX_BUBBLE_LOOKBACK = 20


def _has_inbox_bubble(session: dict | None, task_id: str) -> bool:
    """True if this session already carries an ``inbox`` handoff bubble for this
    task within its last N messages."""
    messages = (session or {}).get("messages") or []
    for message in messages[-_INBOX_BUBBLE_LOOKBACK:]:
        meta = message.get("meta") or {}
        if meta.get("kind") == "inbox" and meta.get("taskId") == task_id:
            return True
    return False


class InboxManager:
    """Always-on, bounded retry loop for flow-back inbox rows."""

    def __init__(self, tick_seconds: float = 2.0, claim_limit: int = 8):
        self._tick = tick_seconds
        self._claim_limit = claim_limit
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("agent inbox started (tick=%ss)", self._tick)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                cur = asyncio.current_task()
                if cur is not None and cur.cancelling():
                    raise
            except Exception:  # noqa: BLE001
                logger.debug("agent inbox stop failed", exc_info=True)
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.warning("agent inbox tick failed", exc_info=True)
            await asyncio.sleep(self._tick)

    @staticmethod
    def _activity(agent_id: str, summary: str, *, task_id: str | None = None,
                  detail: dict | None = None) -> None:
        try:
            activity_store.log(agent_id, "delegation", summary,
                               task_id=task_id, detail=detail)
        except Exception:  # noqa: BLE001
            logger.debug("agent inbox activity log failed", exc_info=True)

    async def _process(self, item: dict) -> bool:
        task = task_store.get(item["fromAgentId"], item["taskId"])
        reply_to = (task or {}).get("metadata", {}).get("replyTo") or {}
        if task is None and not reply_to:
            origin_session = item.get("originSessionId") or ""
            if origin_session.startswith("chat:"):
                reply_to = {"kind": "in_app", "sessionId": origin_session}
            elif origin_session.count(":") >= 2:
                ch, acct, conv = origin_session.split(":", 2)
                reply_to = {"kind": "channel", "sessionId": origin_session, "channel": ch,
                            "accountId": acct, "conversationId": conv}
        if not reply_to:
            error = "cannot deliver flow-back: task has no replyTo origin"
            inbox_store.mark(item["agentId"], item["id"], "failed", error=error)
            self._activity(item["agentId"], error, task_id=item.get("taskId"))
            return False

        event_text = _event_text(item, task)
        kind = reply_to.get("kind")
        if kind == "channel":
            from channels.manager import manager as channel_manager
            await channel_manager.inject_turn(
                item["agentId"], reply_to.get("accountId") or "",
                reply_to.get("conversationId") or "", event_text,
                reply_to=reply_to.get("messageId"), session_id=reply_to.get("sessionId"))
        elif kind == "in_app":
            session_id = reply_to.get("sessionId") or item.get("originSessionId")
            if not session_id:
                raise RuntimeError("cannot deliver flow-back: in-app origin has no sessionId")
            session = chat_store.get_session(session_id)
            # History is the conversation as it stood before this handoff — capture it
            # before appending the handoff bubble below.
            history = [
                {"role": message["role"], "content": message.get("content") or ""}
                for message in (session or {}).get("messages", [])
            ]
            agent = agent_store.get_agent(item["agentId"])
            if not agent:
                # Pre-handoff failure (the run never started) — safe to auto-retry.
                raise RuntimeError("cannot deliver flow-back: receiving agent no longer exists")
            # Append the handoff bubble BEFORE the run so it sits above anything the
            # agent relays into this session mid-run (relay_task_result posts here via
            # the in_app delivery channel). Guarded on the session not already carrying
            # this task's bubble so a manual retry (attempts reset to 0) re-runs the
            # agent without double-posting the same handoff bubble.
            if not _has_inbox_bubble(session, item["taskId"]):
                chat_store.append_message(
                    session_id, "user", event_text,
                    meta={"kind": "inbox", "taskId": item["taskId"],
                          "fromAgentId": item["fromAgentId"]},
                    agent_id=item["agentId"],
                )
            reply, error, run_id = await runner.execute_run(
                agent, event_text, session_id, history=history)
            if error:
                # The run itself errored, but the agent may already have relayed a
                # result into this chat (relay_task_result posts via the in_app
                # channel) before failing — a re-run would risk a duplicate relay.
                # So a run error after handoff is terminal: surface it in the chat,
                # mark the item failed, and do NOT auto-retry. The Inbox tab's Retry
                # button (POST /inbox/{id}/retry, attempts→0) is the explicit human
                # re-run path; it re-appends nothing because the bubble already
                # exists (see _has_inbox_bubble above).
                chat_store.append_message(
                    session_id, "assistant",
                    f"Handoff notice delivered, but my follow-up run failed: {error}",
                    meta={"kind": "inbox_reply", "runId": run_id, "error": str(error)},
                    agent_id=item["agentId"],
                )
                inbox_store.mark(item["agentId"], item["id"], "failed", error=str(error))
                self._activity(item["agentId"], f"Flow-back run failed (terminal): {error}",
                               task_id=item.get("taskId"),
                               detail={"kind": kind, "runId": run_id})
                return False
            chat_store.append_message(
                session_id, "assistant", reply or "",
                meta={"kind": "inbox_reply", "runId": run_id},
                agent_id=item["agentId"],
            )
        else:
            error = f"cannot deliver flow-back: unsupported origin kind {kind!r}"
            inbox_store.mark(item["agentId"], item["id"], "failed", error=error)
            self._activity(item["agentId"], error, task_id=item.get("taskId"))
            return False

        self._activity(item["agentId"], "Flow-back delivered", task_id=item.get("taskId"),
                       detail={"kind": kind, "originSessionId": item.get("originSessionId")})
        return True

    async def tick(self) -> None:
        try:
            items = inbox_store.claim_pending(self._claim_limit)
        except Exception:  # noqa: BLE001
            logger.warning("agent inbox claim failed", exc_info=True)
            return
        for item in items:
            try:
                if await self._process(item):
                    inbox_store.mark(item["agentId"], item["id"], "processed")
            except Exception as exc:  # noqa: BLE001
                error = str(exc) or exc.__class__.__name__
                logger.warning("agent inbox item %s failed: %s", item.get("id"), error,
                               exc_info=True)
                try:
                    attempts = inbox_store.bump_attempt(item["agentId"], item["id"])
                    status = "failed" if attempts >= 3 else "pending"
                    inbox_store.mark(item["agentId"], item["id"], status, error=error)
                    self._activity(item["agentId"], f"Flow-back delivery failed: {error}",
                                   task_id=item.get("taskId"),
                                   detail={"attempts": attempts, "status": status})
                except Exception:  # noqa: BLE001
                    logger.warning("agent inbox retry bookkeeping failed for %s",
                                   item.get("id"), exc_info=True)


inbox_manager = InboxManager()
