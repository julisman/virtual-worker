"""Compose an agent's run — assemble its system prompt + tool registry.

Shared by the chat route (mentor-driven turns) and the always-on worker
(autonomous turns) so both run the agent identically. Layers, in order:

  built-in tools → load_skill (if skills) → file tools (if working folder)
  → memory tools (always) → intern board tools + shell (if the agent has a role)

and injects the skill catalog, stored memory, and board snapshot into the system
prompt. Returns ``(system, registry, temperature, top_p, top_k)``.
"""

from __future__ import annotations

from datetime import datetime

from agent import loop
from agent.custom_tools import make_custom_tools
from agent.channel_tools import make_channel_tools
from agent.delivery import make_deliver_tool
from agent.fs_tools import FS_TOOL_NAMES, make_fs_tools
from agent.intern import build_board_context, make_intern_tools
from agent.knowledge import build_kb_context, make_kb_tools
from agent.memory import build_memory_context, make_memory_tools
from agent.scheduler import make_schedule_tools
from agent.shell_tool import make_shell_tool
from agent.skills import build_skill_context, make_load_skill_resource_tool, make_load_skill_tool
from agent.team import build_team_context, make_team_tools


def compose(agent: dict | None, *, extra_system: str | None = None,
            tools_override: list[str] | None = None):
    agent_id = (agent or {}).get("id") or ""
    persona = (agent or {}).get("persona") or ""
    skills = (agent or {}).get("skills") or []
    working_dir = (agent or {}).get("workingDir") or ""
    role = ((agent or {}).get("role") or "").strip()

    tool_names = tools_override if tools_override is not None else ((agent or {}).get("tools") or [])
    registry = loop.registry_from_names(tool_names)

    system = persona
    if extra_system and extra_system.strip():
        system = f"{system}\n\n{extra_system.strip()}".strip()

    # Current local time — so the agent knows "now" and can turn relative times
    # ("in 2 minutes", "tomorrow 9am") into correct absolute values when it
    # schedules. Without this the model guesses the date/time.
    now = datetime.now().astimezone()
    time_ctx = (
        f"Current date & time: {now:%Y-%m-%d %H:%M} ({now:%A}, {now:%Z} UTC{now:%z}). "
        "This is your local time — use it for scheduling, and write a one-time `at` "
        "as 'YYYY-MM-DDTHH:MM' in this local time."
    )
    system = f"{system}\n\n{time_ctx}".strip() if system else time_ctx

    if skills:
        catalog = build_skill_context(skills)
        if catalog:
            system = f"{system}\n\n{catalog}".strip()
            registry["load_skill"] = make_load_skill_tool(skills)
            registry["load_skill_resource"] = make_load_skill_resource_tool(skills)

    # "Your files" — a working folder automatically grants scoped file tools.
    if working_dir.strip():
        registry.update(make_fs_tools(working_dir))
        system = (f"{system}\n\nYou have file tools ({', '.join(FS_TOOL_NAMES)}) scoped to the "
                  f"working folder `{working_dir.strip()}`. All paths are relative to it.").strip()

    # "Your memory" — always on.
    if agent_id:
        registry.update(make_memory_tools(agent_id))
        mem = build_memory_context(agent_id)
        if mem:
            system = f"{system}\n\n{mem}".strip()
        # "Your schedule" — let the agent set up its own timed runs.
        registry.update(make_schedule_tools(agent_id))
        # "Your voice" — let the agent push a result to an explicit destination.
        registry.update(make_deliver_tool(agent_id))
        # "Your chat" — opt-in: message people on a connected chat channel.
        if ((agent or {}).get("guardrails") or {}).get("channelSend"):
            registry.update(make_channel_tools(agent_id))
        # "Your team" — delegate to other agents (assign_to / ask_agent / team_list).
        # Always on at depth 0; the loop guards live inside the tool factory.
        registry.update(make_team_tools(agent_id))
        team = build_team_context(agent_id)
        if team:
            system = f"{system}\n\n{team}".strip()

    # "Your knowledge" — attached KBs grant a kb_search retrieval tool (RAG).
    knowledge_bases = (agent or {}).get("knowledgeBases") or []
    if knowledge_bases:
        registry.update(make_kb_tools(agent))
        kb_ctx = build_kb_context(knowledge_bases)
        if kb_ctx:
            system = f"{system}\n\n{kb_ctx}".strip()

    # Intern — task board + shell, bounded by the agent's guardrails.
    if agent_id and role:
        registry.update(make_intern_tools(agent_id))
        g = (agent or {}).get("guardrails") or {}
        shell_mode = g.get("shell", "auto")
        if shell_mode != "off":
            registry["shell"] = make_shell_tool(
                working_dir, mode=shell_mode, extra_denied=g.get("extraDenied") or [])
        board = build_board_context(agent_id)
        if board:
            system = f"{system}\n\n{board}".strip()

    # Custom tools — operator-declared HTTP tools assigned to this agent. Added
    # with setdefault so a built-in/MCP/self-tool name always wins (PRD-10 §10:
    # built-in names take precedence; custom tools never shadow them).
    custom_tool_ids = (agent or {}).get("customTools") or []
    if custom_tool_ids:
        for name, entry in make_custom_tools(custom_tool_ids).items():
            registry.setdefault(name, entry)

    temperature = (agent or {}).get("temperature", 0.7)
    top_p = (agent or {}).get("topP")
    top_k = (agent or {}).get("topK")
    return (system or None), registry, temperature, top_p, top_k
