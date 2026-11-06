"""Memory context + tools for agents — progressive, like the skill layer.

``build_memory_context(agent_id)`` renders the agent's stored notes into a
compact block for the system prompt (capped so it never bloats the context —
osaurus keeps memory injection to ~800 tokens; we cap by note count + chars).

``make_memory_tools(agent_id)`` gives the agent the verbs to manage its own
memory: ``memory_write`` (remember a fact), ``memory_search`` (recall older
notes that didn't make the injected slice), and ``memory_forget`` (drop one).
Memory is always on — it's a core pillar of the harness, not an opt-in tool.
"""

from __future__ import annotations

from storage import memory_store

# How much memory to inject into the system prompt each request (keep it small).
MAX_INJECTED_NOTES = 25
MAX_INJECTED_CHARS = 3500


def build_memory_context(agent_id: str) -> str:
    """Render the agent's most-recent memory notes as a system-prompt block.

    Returns an empty string when the agent has no memories yet."""
    if not agent_id:
        return ""
    notes = memory_store.list_for(agent_id, limit=MAX_INJECTED_NOTES)
    if not notes:
        return ""
    lines = ["## Memory",
             "These are things you chose to remember from earlier sessions with this "
             "user (most recent first). Treat them as established context. When you "
             "learn a new durable fact about the user, their projects, or their "
             "preferences, call `memory_write` to keep it. Use `memory_search` to "
             "recall older notes not shown here.",
             ""]
    used = 0
    shown = 0
    total = memory_store.count(agent_id)
    for n in notes:
        tag = f" [{n['tags']}]" if n["tags"] else ""
        entry = f"- {n['content']}{tag}"
        if used + len(entry) > MAX_INJECTED_CHARS:
            break
        lines.append(entry)
        used += len(entry)
        shown += 1
    if total > shown:
        lines.append(f"\n_({total - shown} older note(s) not shown — use memory_search.)_")
    return "\n".join(lines)


def make_memory_tools(agent_id: str) -> dict[str, dict]:
    """Build the ``{name: {schema, fn}}`` registry of memory verbs for ``agent_id``."""

    async def memory_write(content: str = "", tags: str = "") -> str:
        content = (content or "").strip()
        if not content:
            return "memory_write needs 'content' — the fact to remember."
        mem = memory_store.add(agent_id, content, tags)
        return f"remembered (id={mem['id']}): {mem['content']}"

    async def memory_search(query: str = "", limit: int = 20) -> str:
        hits = memory_store.search(agent_id, query, limit=limit)
        if not hits:
            return f"no memories match {query!r}"
        return "\n".join(f"- ({h['id']}) {h['content']}"
                         + (f" [{h['tags']}]" if h["tags"] else "") for h in hits)

    async def memory_forget(id: str = "") -> str:
        if not id:
            return "memory_forget needs the note 'id' (from memory_search)."
        return f"forgot {id}" if memory_store.delete(agent_id, id) else f"no memory with id {id!r}"

    return {
        "memory_write": {
            "fn": memory_write,
            "kind": "memory",
            "schema": {
                "type": "function",
                "function": {
                    "name": "memory_write",
                    "description": "Remember a durable fact about the user, their projects, or "
                                   "their preferences so you recall it in future sessions. Keep "
                                   "each note short and self-contained.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "the fact to remember"},
                            "tags": {"type": "string", "description": "optional comma-separated tags"},
                        },
                        "required": ["content"],
                    },
                },
            },
        },
        "memory_search": {
            "fn": memory_search,
            "parallel_safe": True,
            "kind": "memory",
            "schema": {
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "description": "Search your stored memories for older notes that weren't "
                                   "pre-loaded into context. Returns matching notes with their ids.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "words to look for"},
                            "limit": {"type": "integer", "description": "max notes to return (default 20)"},
                        },
                        "required": ["query"],
                    },
                },
            },
        },
        "memory_forget": {
            "fn": memory_forget,
            "kind": "memory",
            "schema": {
                "type": "function",
                "function": {
                    "name": "memory_forget",
                    "description": "Delete a memory note by id (from memory_search) when it's "
                                   "wrong or no longer true.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "the memory note id to delete"},
                        },
                        "required": ["id"],
                    },
                },
            },
        },
    }
