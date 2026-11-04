"""Intern tools — the verbs that let an agent run its own Kanban board.

When an agent is configured as an intern (has a job ``role``), these tools are
composed onto the harness loop so it can see its tasks, claim the next one, move
work across the board, file new tasks on its own initiative (decision D1), and
log progress. Every action also writes an activity row so the mentor can see
what happened. See docs/INTERN_AGENT_PRD.md §8.3.
"""

from __future__ import annotations

from storage import activity_store, task_store


def build_board_context(agent_id: str) -> str:
    """A compact snapshot of the open board for the intern's system prompt."""
    if not agent_id:
        return ""
    open_tasks = [t for t in task_store.list_for(agent_id)
                  if t["status"] not in ("done", "cancelled")]
    lines = ["## Your task board",
             "You are an intern. You manage your own work as tasks on a Kanban board. "
             "Use `task_list` to see it, `task_claim` to start the next task, "
             "`task_update` to move a task (in_progress → review when done, with a "
             "result summary; blocked if you're stuck), `task_create` to file new work "
             "you discover, and `task_comment` to log progress for your mentor. Move a "
             "finished task to `review` (not `done`) so your mentor can sign off.", ""]
    if not open_tasks:
        lines.append("_No open tasks right now._")
        return "\n".join(lines)
    for t in open_tasks[:15]:
        lines.append(f"- [{t['status']}] ({t['priority']}) `{t['id']}` — {t['title']}")
    return "\n".join(lines)


def _summarize(text: str, n: int = 120) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def make_intern_tools(agent_id: str) -> dict[str, dict]:
    """Build the ``{name: {schema, fn}}`` registry of board verbs for ``agent_id``."""

    async def task_list(status: str = "") -> str:
        tasks = task_store.list_for(agent_id, status=status or None)
        if not tasks:
            return "no tasks" + (f" with status {status!r}" if status else "")
        return "\n".join(
            f"[{t['status']}] ({t['priority']}) {t['id']} — {t['title']}"
            + (f" :: {_summarize(t['description'])}" if t["description"] else "")
            for t in tasks)

    async def task_create(title: str = "", description: str = "", priority: str = "medium") -> str:
        if not title.strip():
            return "task_create needs a 'title'."
        t = task_store.create(agent_id, {"title": title, "description": description,
                                         "priority": priority, "status": "todo"}, origin="intern")
        activity_store.log(agent_id, "task_created", f"Filed task: {t['title']}",
                           task_id=t["id"], detail={"priority": t["priority"], "by": "intern"})
        return f"created task {t['id']} ({t['priority']}) — {t['title']}"

    async def task_claim(id: str = "") -> str:
        t = task_store.get(agent_id, id)
        if not t:
            return f"no task with id {id!r}"
        task_store.update(agent_id, id, {"status": "in_progress"})
        activity_store.log(agent_id, "task_claimed", f"Started: {t['title']}", task_id=id)
        return f"claimed {id} — now in_progress: {t['title']}"

    async def task_update(id: str = "", status: str = "", note: str = "",
                          result_summary: str = "") -> str:
        t = task_store.get(agent_id, id)
        if not t:
            return f"no task with id {id!r}"
        if status and status not in task_store.STATUSES:
            return f"unknown status {status!r}. Use one of: {', '.join(task_store.STATUSES)}"
        patch: dict = {}
        if status:
            patch["status"] = status
        if result_summary:
            patch["resultSummary"] = result_summary
        if patch:
            task_store.update(agent_id, id, patch)
        if note:
            task_store.add_comment(id, "intern", note)
        if status:
            activity_store.log(agent_id, "task_moved",
                               f"{t['title']}: {t['status']} → {status}", task_id=id,
                               detail={"from": t["status"], "to": status})
        return f"updated {id}" + (f" → {status}" if status else "") + (" (+note)" if note else "")

    async def task_comment(id: str = "", text: str = "") -> str:
        if not task_store.get(agent_id, id):
            return f"no task with id {id!r}"
        if not text.strip():
            return "task_comment needs 'text'."
        task_store.add_comment(id, "intern", text)
        activity_store.log(agent_id, "note", f"Note on task: {_summarize(text)}", task_id=id)
        return f"commented on {id}"

    def _tool(fn, name, desc, props, required):
        return {"fn": fn, "schema": {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}}

    return {
        "task_list": _tool(
            task_list, "task_list", "List your tasks, optionally filtered by status.",
            {"status": {"type": "string", "description": "optional: backlog|todo|in_progress|blocked|review|done|cancelled"}}, []),
        "task_create": _tool(
            task_create, "task_create",
            "File a new task on your board (use this when you discover work to do).",
            {"title": {"type": "string"}, "description": {"type": "string"},
             "priority": {"type": "string", "description": "low|medium|high|urgent"}}, ["title"]),
        "task_claim": _tool(
            task_claim, "task_claim", "Start a task — moves it to in_progress.",
            {"id": {"type": "string", "description": "the task id"}}, ["id"]),
        "task_update": _tool(
            task_update, "task_update",
            "Move a task to a new status. When finished, set status='review' with a "
            "result_summary so your mentor can sign off; use 'blocked' if you're stuck.",
            {"id": {"type": "string"}, "status": {"type": "string"},
             "note": {"type": "string", "description": "optional progress note (logged as a comment)"},
             "result_summary": {"type": "string", "description": "what you did, for the mentor's review"}}, ["id"]),
        "task_comment": _tool(
            task_comment, "task_comment", "Add a progress note to a task's thread.",
            {"id": {"type": "string"}, "text": {"type": "string"}}, ["id", "text"]),
    }
