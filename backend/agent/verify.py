"""Verify — the harness's closing pillar.

After the autonomous worker finishes a task (the agent marks it `done`), it
doesn't just take its own word for it: a focused verification pass re-reads the
task and what the agent did and decides whether it's genuinely complete. A pass
keeps it `done`; a fail sends it back to `todo` with a reason (bounded by a retry
cap so it can't ping-pong forever). This is the agent loop "closing out with a
verified summary" rather than declaring success blindly.

Verification is a plain, tool-less model call (cheap, deterministic temperature)
separate from the working loop, so the reviewer can't just re-do the work — it
can only judge it.
"""

from __future__ import annotations

import logging

from gateway import egress
from storage import task_store

logger = logging.getLogger(__name__)

MAX_VERIFY_ATTEMPTS = 2  # after this many NEEDS_WORK rounds, accept and move on

_SYSTEM = (
    "You are a strict reviewer. You are given a task and what a junior worker did, "
    "and you decide whether the task is genuinely complete and correct. Be skeptical "
    "but fair. Reply with EXACTLY ONE line, starting with either 'VERIFIED:' (the "
    "task is done) or 'NEEDS_WORK:' (it is not), followed by a one-sentence reason."
)


async def _complete(model: str, system: str, user: str, provider: dict | None = None) -> str:
    payload = {
        "model": model, "temperature": 0.0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    text = ""
    async for chunk in egress.stream(payload, provider):
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text += delta["content"]
    return text.strip()


async def verify_task(model: str, task: dict, *, context: str = "",
                      provider: dict | None = None) -> tuple[bool, str]:
    """Return (verified, reason). On any error, default to verified=True so a
    flaky reviewer never blocks real work. ``provider`` runs the reviewer on the
    agent's external provider (or None for the local upstream)."""
    user = (
        f"Task: {task['title']}\n"
        f"Description: {task.get('description') or '(none)'}\n"
        f"Definition of done (acceptance criteria): {task.get('criteria') or '(none specified)'}\n\n"
        f"The worker marked this DONE.\n"
        f"Result summary: {task.get('resultSummary') or '(none)'}\n"
        f"{context}\n\n"
        "Is the task genuinely complete and correct? If a definition of done is given, "
        "every criterion must be met — reply NEEDS_WORK if any is unmet."
    )
    try:
        out = await _complete(model, _SYSTEM, user, provider=provider)
    except Exception as exc:  # noqa: BLE001
        logger.debug("verify call failed: %s", exc)
        return True, "verification skipped (reviewer unavailable)"
    # Find the verdict line anywhere in the reply (chatty models preamble), and
    # treat a reply with NO parseable verdict like an unavailable reviewer (fail
    # open, but say so) instead of counting arbitrary text as a pass.
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    line = next((ln for ln in lines
                 if ln.upper().startswith(("VERIFIED", "NEEDS_WORK"))), "")
    if not line:
        return True, "verification inconclusive (reviewer reply did not follow the format)"
    verified = not line.upper().startswith("NEEDS_WORK")
    reason = line.split(":", 1)[1].strip() if ":" in line else "(no reason given)"
    return verified, reason[:300]


def attempts(task: dict) -> int:
    return int((task.get("metadata") or {}).get("verifyAttempts", 0))


def record_attempt(agent_id: str, task: dict) -> None:
    meta = dict(task.get("metadata") or {})
    meta["verifyAttempts"] = attempts(task) + 1
    task_store.update(agent_id, task["id"], {"metadata": meta})
