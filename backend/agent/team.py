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
  - **cycle**  — a visited-chain guard refuses A→B→A handoff loops.

Every delegation is journaled on BOTH agents' activity feeds (type
``delegation``) so the org chart in motion is fully auditable.
"""

from __future__ import annotations

import logging

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
    return (next((m for m in mates if (m["name"] or "").lower() == low), None)
            or next((m for m in mates if (m["name"] or "").lower().startswith(low)), None))


def make_team_tools(caller_agent_id: str, *, depth: int = 0,
                    chain: tuple[str, ...] | None = None,
                    budget: dict | None = None) -> dict[str, dict]:
    """Build the ``{name: {schema, fn}}`` delegation registry for ``caller_agent_id``.

    ``depth``/``chain``/``budget`` thread the loop guards through nested
    ``ask_agent`` calls. Top-level (a normal agent run) starts at depth 0 with a
    fresh per-run budget; a nested consult bumps depth and extends the chain.
    """
    chain = chain if chain is not None else (caller_agent_id,)
    budget = budget if budget is not None else {"asks": 0, "assigns": 0}

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

        budget["assigns"] += 1
        caller_name = _caller_name()
        t = task_store.create(
            target["id"],
            {"title": task_title, "description": task_description, "priority": priority,
             "status": "todo",
             "metadata": {"assignedBy": caller_agent_id, "assignedByName": caller_name}},
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
        if budget["asks"] >= MAX_ASKS_PER_RUN:
            return f"refused: consult budget ({MAX_ASKS_PER_RUN} per run) exhausted."
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
        from gateway.models import resolve_model

        try:
            model = target.get("model") or await resolve_model(None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("resolve_model failed: %s", exc)
            model = None
        if not model:
            return "could not consult: no active model. Activate one in Models."

        system, registry, temperature, top_p, top_k = compose(target, extra_system=_ASK_DIRECTIVE)
        # Re-bind the teammate's team tools so a nested ask is one level deeper and
        # carries the visited chain — this is the recursion/cycle guard in action.
        registry.update(make_team_tools(
            target["id"], depth=depth + 1, chain=chain + (target["id"],), budget=budget))

        activity_store.log(caller_agent_id, "delegation",
                           f"Asked {target['name']}: {_summarize(question, 80)}",
                           detail={"to": target["id"], "kind": "ask"})

        text_parts: list[str] = []
        err: str | None = None
        try:
            async for ev in loop.run(
                [{"role": "user", "content": question}], model, registry, temperature,
                session_id=f"ask:{caller_agent_id}->{target['id']}", system=system,
                top_p=top_p, top_k=top_k, agent_id=target["id"]):
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
