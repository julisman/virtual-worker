"""Team tools — the verbs that let one agent delegate to another (PRD-02).

Virtual Worker agents normally work alone. These tools give any agent a thin set of
agent-to-agent primitives, composed onto its harness loop just like the schedule
and intern tools:

  - ``team_list``          — who else can I delegate to? (id, name, role)
  - ``assign_to``          — file a task on a *teammate's* board (async handoff);
                             the teammate's always-on worker picks it up.
  - ``ask_agent``          — run a teammate SYNCHRONOUSLY for a one-shot answer
                             and read its reply back into my turn.

The managed runtime serves **one model at a time**, so ``ask_agent`` does not try
to parallelise — it simply ``await``s a single nested ``loop.run`` (one model call
in flight at a time, naturally serialized on the inference server).

Loops are the danger with agent-to-agent calls, so delegation is bounded by three
guards (PRD §6.4 FR-14):

  - **depth**  — ``MAX_DELEGATION_DEPTH``: how deep synchronous asks may nest.
  - **budget** — ``MAX_ASKS_PER_RUN`` / ``MAX_ASSIGNS_PER_RUN``: per-run caps so a
                 single turn can't fan out into a swarm.
  - **cycle**  — a visited-chain guard refuses A→B→A loops. For synchronous
                 asks the chain lives in the call stack; for board handoffs it is
                 PERSISTED on the task (``metadata.delegationChain``) and threaded
                 back in by the worker, so the guard also holds across runs.

Every delegation is journaled on BOTH agents' activity feeds (type
``delegation``) so the org chart in motion is fully auditable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from storage import activity_store, agent_store, task_store

logger = logging.getLogger(__name__)

# --- Loop / budget guards -------------------------------------------------- #
MAX_DELEGATION_DEPTH = 2   # depth 0 = top-level agent; refuse asks at/after this
MAX_ASKS_PER_RUN = 3       # synchronous consults a single run may make
MAX_ASSIGNS_PER_RUN = 5    # board handoffs a single run may make

_ASK_DIRECTIVE = (
    "You are being consulted SYNCHRONOUSLY by a teammate agent. Answer the "
    "question below directly and concisely using the tools you have, then stop. "
    "Your text reply is handed straight back to the teammate — do not delegate "
    "this onward unless you genuinely must."
)


def origin_from_session(session_id: str | None, inbound: dict | None = None) -> dict | None:
    """Build the task reply target captured by an interactive conversation."""
    if not session_id:
        return None
    if session_id.startswith("chat:"):
        return {"kind": "in_app", "sessionId": session_id,
                "capturedAt": datetime.now(timezone.utc).isoformat()}
    # A chat-channel session: "<channel>:<accountId>:<conversationId>" (see
    # channels.manager). The live inbound message, when available, carries the
    # exact conversation + message id to reply to.
    parts = session_id.split(":", 2)
    if len(parts) == 3 and parts[0] not in ("worker", "schedule", "trigger", "golden"):
        origin = {"kind": "channel", "sessionId": session_id, "channel": parts[0],
                  "accountId": parts[1], "conversationId": parts[2],
                  "capturedAt": datetime.now(timezone.utc).isoformat()}
        if inbound is not None:
            origin["conversationId"] = getattr(inbound, "conversation_id", None) or origin["conversationId"]
            origin["messageId"] = getattr(inbound, "event_id", None)
            origin["chatKind"] = getattr(inbound, "kind", None)
        return {key: value for key, value in origin.items() if value is not None}
    return None


def _summarize(text: str, n: int = 100) -> str:
    return (text or "").strip().replace("\n", " ")[:n]


def build_team_context(agent_id: str) -> str:
    """A short prompt note listing teammates the agent can delegate to."""
    if not agent_id:
        return ""
    mates = [a for a in agent_store.list_agents() if a["id"] != agent_id]
    if not mates:
        return ""
    lines = ["## Your team",
             "You can work with other agents on this machine. Use `team_list` to see "
             "them, `assign_to` to hand a task to a teammate's board (they pick it up "
             "themselves), and `ask_agent` to get a one-shot answer from a teammate "
             "right now. Delegation is depth- and budget-limited to prevent loops.", ""]
    for a in mates[:12]:
        role = a.get("role") or "agent"
        lines.append(f"- `{a['id']}` — {a['name']} ({role})")
    return "\n".join(lines)


def _resolve(agent_ref: str, caller_id: str) -> dict | None:
    """Find a teammate by id (preferred) or by exact/`startswith` name match."""
    ref = (agent_ref or "").strip()
    if not ref:
        return None
    a = agent_store.get_agent(ref)
    if a:
        return a
    mates = [m for m in agent_store.list_agents() if m["id"] != caller_id]
    low = ref.lower()
    exact = next((m for m in mates if (m["name"] or "").lower() == low), None)
    if exact:
        return exact
    pre = [m for m in mates if (m["name"] or "").lower().startswith(low)]
    # A prefix match is only safe when it's unambiguous — "Ana" must not
    # silently pick between "Ana Belle" and "Anabel".
    return pre[0] if len(pre) == 1 else None


def make_team_tools(caller_agent_id: str, *, depth: int = 0,
                    chain: tuple[str, ...] | None = None,
                    budget: dict | None = None,
                    ask_cap: int | None = None,
                    session_id: str | None = None) -> dict[str, dict]:
    """Build the ``{name: {schema, fn}}`` delegation registry for ``caller_agent_id``.

    ``depth``/``chain``/``budget`` thread the loop guards through nested
    ``ask_agent`` calls. Top-level (a normal agent run) starts at depth 0 with a
    fresh per-run budget; a nested consult bumps depth and extends the chain.

    ``ask_cap`` is the synchronous-consult (ask) limit for the whole run — the
    ORIGINAL caller's (run owner's) ``askBudget`` if set, else
    ``MAX_ASKS_PER_RUN``. It is resolved once at the top level and threaded
    unchanged through nested consults so a teammate can't widen the owner's cap.
    """
    chain = chain if chain is not None else (caller_agent_id,)
    if caller_agent_id not in chain:
        chain = chain + (caller_agent_id,)
    budget = budget if budget is not None else {"asks": 0, "assigns": 0}
    if ask_cap is None:
        owner = agent_store.get_agent(caller_agent_id)
        owner_budget = (owner or {}).get("askBudget")
        ask_cap = owner_budget if isinstance(owner_budget, int) else MAX_ASKS_PER_RUN

    def _caller_name() -> str:
        a = agent_store.get_agent(caller_agent_id)
        return (a or {}).get("name") or caller_agent_id

    async def team_list() -> str:
        mates = [a for a in agent_store.list_agents() if a["id"] != caller_agent_id]
        if not mates:
            return "no other agents to delegate to yet."
        return "\n".join(
            f"- {a['id']} — {a['name']} ({a.get('role') or 'agent'})"
            f" [{a.get('dutyStatus') or 'off'}]"
            for a in mates)

    async def assign_to(agent: str = "", task_title: str = "",
                        task_description: str = "", priority: str = "medium") -> str:
        if depth >= MAX_DELEGATION_DEPTH:
            return (f"refused: delegation depth limit ({MAX_DELEGATION_DEPTH}) reached — "
                    "cannot hand off further from here.")
        if budget["assigns"] >= MAX_ASSIGNS_PER_RUN:
            return f"refused: assignment budget ({MAX_ASSIGNS_PER_RUN} per run) exhausted."
        if not task_title.strip():
            return "assign_to needs a 'task_title'."
        target = _resolve(agent, caller_agent_id)
        if not target:
            return f"no such agent {agent!r}. Use team_list to see who's available."
        if target["id"] == caller_agent_id:
            return "refused: cannot assign a task to yourself — use task_create."
        if target["id"] in chain:
            # Cross-run loop guard: the chain arrived via the task this run is
            # working (metadata.delegationChain), so an A→B→A assignment
            # ping-pong across worker runs is refused here.
            return (f"refused: delegation cycle detected ({' → '.join(chain)} → "
                    f"{target['id']}). Breaking the loop.")

        budget["assigns"] += 1
        caller_name = _caller_name()
        inbound = None
        if session_id and not session_id.startswith("chat:"):
            try:
                from channels.manager import CURRENT_INBOUND
                inbound = CURRENT_INBOUND.get()
            except Exception:  # noqa: BLE001
                inbound = None
        origin = origin_from_session(session_id, inbound=inbound)
        metadata = {"assignedBy": caller_agent_id, "assignedByName": caller_name,
                    "delegationChain": list(chain) + [target["id"]]}
        if origin is not None:
            metadata["replyTo"] = origin
        t = task_store.create(
            target["id"],
            {"title": task_title, "description": task_description, "priority": priority,
             "status": "todo",
             "metadata": metadata},
            origin="agent")
        # Journal on BOTH feeds (PRD §6.6 FR-19) so the handoff is auditable.
        activity_store.log(target["id"], "delegation",
                           f"Assigned by {caller_name}: {t['title']}", task_id=t["id"],
                           detail={"from": caller_agent_id, "kind": "assign"})
        activity_store.log(caller_agent_id, "delegation",
                           f"Delegated to {target['name']}: {t['title']}", task_id=t["id"],
                           detail={"to": target["id"], "kind": "assign"})
        return (f"assigned task {t['id']} to {target['name']} — it's on their board now and "
                "their worker will pick it up when on duty.")

    async def ask_agent(agent: str = "", question: str = "") -> str:
        if depth >= MAX_DELEGATION_DEPTH:
            return (f"refused: delegation depth limit ({MAX_DELEGATION_DEPTH}) reached — "
                    "cannot consult another agent from here.")
        if budget["asks"] >= ask_cap:
            return f"refused: consult budget ({ask_cap} per run) exhausted."
        if not question.strip():
            return "ask_agent needs a 'question'."
        target = _resolve(agent, caller_agent_id)
        if not target:
            return f"no such agent {agent!r}. Use team_list to see who's available."
        if target["id"] == caller_agent_id:
            return "refused: cannot ask yourself."
        if target["id"] in chain:
            return (f"refused: delegation cycle detected ({' → '.join(chain)} → "
                    f"{target['id']}). Breaking the loop.")

        budget["asks"] += 1
        caller_name = _caller_name()

        # Lazy imports break the compose↔team cycle (compose imports this module).
        from agent import loop
        from agent.compose import compose
        from gateway.providers import resolve_target

        # Resolve the TEAMMATE onto its own configured backend (local upstream or
        # an external provider) — mirrors runner/worker so a remote-only setup can
        # consult a provider-backed teammate instead of failing on the local :8080.
        provider_target = None
        try:
            model, provider_target = await resolve_target(target)
        except Exception as exc:  # noqa: BLE001
            logger.debug("resolve_target failed: %s", exc)
            model = None
        # A provider-backed teammate (target set) needs no local active model.
        if not model and provider_target is None:
            return "could not consult: no active model. Activate one in Models."

        # Honour the TEAMMATE's own guardrails on the consult path (agent_store
        # merges defaults on read, so this is always populated) — mirrors runner
        # so a consulted agent isn't silently capped at loop.run's MAX_ITERATIONS
        # fallback. 0 stays "unlimited (backstopped)", exactly as runner passes it.
        g = target.get("guardrails") or {}

        ask_session_id = f"ask:{caller_agent_id}->{target['id']}"
        system, registry, temperature, top_p, top_k, repeat_penalty, reasoning_effort = compose(
            target, extra_system=_ASK_DIRECTIVE, session_id=ask_session_id)
        # Re-bind the teammate's team tools so a nested ask is one level deeper and
        # carries the visited chain — this is the recursion/cycle guard in action.
        registry.update(make_team_tools(
            target["id"], depth=depth + 1, chain=chain + (target["id"],),
            budget=budget, ask_cap=ask_cap))

        activity_store.log(caller_agent_id, "delegation",
                           f"Asked {target['name']}: {_summarize(question, 80)}",
                           detail={"to": target["id"], "kind": "ask"})

        text_parts: list[str] = []
        err: str | None = None
        try:
            async for ev in loop.run(
                [{"role": "user", "content": question}], model, registry, temperature,
                session_id=ask_session_id, system=system,
                top_p=top_p, top_k=top_k, repeat_penalty=repeat_penalty,
                reasoning_effort=reasoning_effort, agent_id=target["id"],
                max_iterations=g.get("maxIterations"), wrapup_warn_at=g.get("wrapupWarnAt"),
                max_output_tokens=g.get("maxOutputTokens"),
                provider=provider_target):
                if ev.get("type") == "text":
                    text_parts.append(ev.get("delta", ""))
                elif ev.get("type") == "error":
                    err = ev.get("message")
        except Exception as exc:  # noqa: BLE001
            err = str(exc)

        answer = "".join(text_parts).strip()
        if err and not answer:
            activity_store.log(target["id"], "error",
                               f"Consult from {caller_name} failed: {_summarize(err)}",
                               detail={"from": caller_agent_id, "kind": "ask"})
            return f"{target['name']} could not answer: {err}"
        activity_store.log(target["id"], "delegation",
                           f"Answered {caller_name}: {_summarize(answer or '(no text)')}",
                           detail={"from": caller_agent_id, "kind": "ask"})
        return answer or "(the teammate returned no text)"

    def _tool(fn, name, desc, props, required):
        return {"fn": fn, "schema": {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}}

    return {
        "team_list": _tool(
            team_list, "team_list",
            "List the other agents you can delegate to (id, name, role, duty state).",
            {}, []),
        "assign_to": _tool(
            assign_to, "assign_to",
            "Hand a task to a teammate by filing it on THEIR board. Asynchronous: the "
            "teammate's own worker picks it up. Use for work you want done in the "
            "background by a specialist.",
            {"agent": {"type": "string", "description": "teammate id or name (see team_list)"},
             "task_title": {"type": "string"},
             "task_description": {"type": "string"},
             "priority": {"type": "string", "description": "low|medium|high|urgent"}},
            ["agent", "task_title"]),
        "ask_agent": _tool(
            ask_agent, "ask_agent",
            "Ask a teammate a question and get their answer back right now "
            "(synchronous, one-shot). Use for a focused input you need before you can "
            "continue. Bounded by delegation depth and a per-run budget.",
            {"agent": {"type": "string", "description": "teammate id or name (see team_list)"},
             "question": {"type": "string"}},
            ["agent", "question"]),
    }
