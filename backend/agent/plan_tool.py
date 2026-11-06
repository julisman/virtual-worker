"""Step checklist — an opt-in ``set_plan`` tool the agent uses to publish a live
step list for multi-step work (rendered by chat surfaces that support it).

Per-run and **stateless**: the model owns the plan and every call *replaces* the
whole thing (full replacement, no diffs). The backend persists nothing — the
tool only validates + acknowledges; a chat surface renders whatever the last
call carried. Off by default; gated by
the ``stepChecklist`` guardrail in ``agent/compose.py`` (mirrors ``skillFactory``).
"""

from __future__ import annotations

MAX_STEPS = 10
MAX_TEXT_CHARS = 80
_STATUSES = ("pending", "active", "done")


def make_plan_tool() -> dict[str, dict]:
    """``{"set_plan": {schema, fn, kind}}`` — the step-checklist tool.

    Stateless: ``fn`` validates and acknowledges only; the plan is surfaced by
    the caller (a chat surface may intercept the ``set_plan`` tool_call event).
    """

    async def set_plan_fn(steps=None) -> str:
        if not isinstance(steps, list) or not steps:
            return ("set_plan needs 'steps' — a non-empty array of "
                    "{text, status: pending|active|done}. Full replacement: "
                    "each call replaces the entire plan.")
        clean: list[dict] = []
        for raw in steps[:MAX_STEPS]:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()[:MAX_TEXT_CHARS]
            if not text:
                continue
            status = raw.get("status")
            status = status if status in _STATUSES else "pending"
            clean.append({"text": text, "status": status})
        if not clean:
            return ("set_plan got no usable steps — each step needs a non-empty "
                    "'text'. Full replacement: each call replaces the entire plan.")
        return f"ok ({len(clean)} steps)"

    return {
        "set_plan": {
            "fn": set_plan_fn,
            "kind": "plan",
            "schema": {
                "type": "function",
                "function": {
                    "name": "set_plan",
                    "description": (
                        "Publish a step checklist for multi-step work — shown live to "
                        "the user inside your progress card. FULL REPLACEMENT: every "
                        "call replaces the entire plan, so pass the complete step list "
                        "each time (call again as steps start and finish to advance "
                        "them). Up to 10 steps; keep each short. Not for simple "
                        "lookups."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "steps": {
                                "type": "array",
                                "description": "the complete ordered step list (replaces any prior plan)",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string", "description": "short step label, in the user's language"},
                                        "status": {"type": "string", "enum": list(_STATUSES),
                                                   "description": "pending | active | done"},
                                    },
                                    "required": ["text", "status"],
                                },
                            },
                        },
                        "required": ["steps"],
                    },
                },
            },
        },
    }
