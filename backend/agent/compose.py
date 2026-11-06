"""Compose an agent's run — assemble its system prompt + tool registry.

Shared by the chat route (mentor-driven turns) and the always-on worker
(autonomous turns) so both run the agent identically. Layers, in order:

  built-in tools → load_skill (if skills) → file tools (if working folder)
  → memory tools (always) → intern board tools + shell (if the agent has a role)

and injects the skill catalog, stored memory, and board snapshot into the system
prompt. Returns
``(system, registry, temperature, top_p, top_k, repeat_penalty, reasoning_effort)``.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from datetime import datetime

import config
from agent import loop
from agent import browser_control
from agent.browser_control import make_recording_tools, make_screenshot_tool
from agent.crusher import make_retrieve_tool
from agent.custom_tools import make_custom_tools
from agent.delivery import make_deliver_tool
from agent.channel_tools import make_channel_tools
from agent.doc_tools import DOC_TOOL_NAMES, make_doc_tools
from agent.fs_tools import FS_TOOL_NAMES, FsError, make_fs_tools
from agent.intern import build_board_context, make_intern_tools
from agent.linked_repo import make_linked_repo_tools
from agent.knowledge import build_kb_context, make_kb_tools
from agent.memory import build_memory_context, make_memory_tools
from agent.plan_tool import make_plan_tool
from agent.scheduler import make_schedule_tools
from agent.shell_tool import make_shell_tool
from agent.skill_factory import build_skill_factory_context, make_skill_factory_tools
from agent.skills import (
    build_connector_native_note,
    build_preloaded_skills,
    build_skill_context,
    collect_wiki_skills,
    make_load_skill_resource_tool,
    make_load_skill_tool,
)
from agent.repo_wiki import make_repo_wiki_tools
from agent.team import build_team_context, make_team_tools
from agent.inbox_tools import make_inbox_tools
from agent.tools import make_search_tool
from agent.wiki import build_wiki_context, make_wiki_registrar_tools, make_wiki_tools
from storage import settings

logger = logging.getLogger(__name__)

# ── session clock ─────────────────────────────────────────────────────────────
# The prompt used to carry no clock at all: per-turn "now" text invalidated the
# llama.cpp prefix cache on every turn and confused small local models. But a
# clock-free prompt meant agents answered time questions from training data
# (reported as a bug). Compromise: ONE clock line, pinned per session_id and
# refreshed at most every 15 minutes — byte-identical prompts within a chat
# burst (cache-safe), fresh time for headless runs (scheduler/worker reuse
# their session ids but fire well apart), bounded staleness in long chats.
# The line is appended as the LAST block of the system prompt so everything
# before it stays prefix-cache reusable even across cold sessions.
_CLOCK_REFRESH_SECONDS = 15 * 60
_CLOCK_CACHE_MAX = 1024

# "Focus output" — opt-in action-first response style (guardrails.focusOutput),
# adapted from ayghri/i-have-adhd. Static text: safe anywhere above the clock.
_FOCUS_STYLE = """Output style — action-first:
- Lead with the answer or the next action; no preamble, no closing pleasantries.
- Number multi-step work; one bounded action per step.
- End with exactly one concrete next step doable in under two minutes.
- Finish the current issue before mentioning anything tangential.
- In a multi-turn task, restate in one line where we are.
- Give specific estimates ("~15 min"), never vague ones.
- State errors plainly: cause, then fix.
- Cap lists at 5 items; split longer ones into priority tiers.
Exceptions: requested explanations, destructive actions, or genuine ambiguity — handle those fully first."""
_clock_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()


def _now_local() -> datetime:
    """Machine-local wall clock (monkeypatched in tests)."""
    return datetime.now().astimezone()


def clock_line(session_id: str | None) -> str:
    """The dated line injected into every system prompt, pinned per session."""
    key = session_id or "?"
    now = _now_local()
    cached = _clock_cache.get(key)
    if cached and now.timestamp() - cached[0] < _CLOCK_REFRESH_SECONDS:
        _clock_cache.move_to_end(key)
        return cached[1]
    line = ("Current date and time: "
            f"{now.strftime('%A, %Y-%m-%d %H:%M (UTC%z)')} — this machine's "
            "clock, refreshed every few minutes.")
    _clock_cache[key] = (now.timestamp(), line)
    _clock_cache.move_to_end(key)
    while len(_clock_cache) > _CLOCK_CACHE_MAX:
        _clock_cache.popitem(last=False)
    return line


def _shell_env(tool_names: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Env vars the agent's shell subprocesses carry so a CLI can detect it is
    running inside a Virtual Worker agent (and drive the agent's browser, not the human's):

    - ``VW_API_BASE``: this backend's base URL. Honors an explicit VW_API_BASE,
      else reconstructs it from BACKEND_PORT (dev.sh's uvicorn --port), else 8888.
    - ``VW_AGENT_BROWSER``: "1" when the agent's enabled tools include any
      browser-control tool, else "0".
    """
    api_base = os.environ.get("VW_API_BASE") or f"http://127.0.0.1:{os.environ.get('BACKEND_PORT', '8888')}"
    has_browser = bool(set(tool_names) & browser_control.CONTROL_TOOL_NAMES)
    return {"VW_API_BASE": api_base, "VW_AGENT_BROWSER": "1" if has_browser else "0"}


def compose(agent: dict | None, *, extra_system: str | None = None,
            tools_override: list[str] | None = None,
            preload_skills: list[str] | None = None,
            delegation_chain: tuple[str, ...] | None = None,
            session_id: str | None = None):
    agent_id = (agent or {}).get("id") or ""
    persona = (agent or {}).get("persona") or ""
    skills = (agent or {}).get("skills") or []
    working_dir = (agent or {}).get("workingDir") or ""
    role = ((agent or {}).get("role") or "").strip()

    tool_names = tools_override if tools_override is not None else ((agent or {}).get("tools") or [])
    registry = loop.registry_from_names(tool_names)

    # Web search is provider-pluggable per agent (SearXNG / Tavily / Brave / DDG).
    if "browser_search" in registry:
        registry["browser_search"] = make_search_tool((agent or {}).get("webSearch"))

    # PRD-17: compressed tool results carry <<vw:HASH …>> markers; retrieve_full
    # recovers the stored original. Injected unless compression is off for this
    # agent (guardrail) or globally (VW_COMPRESSION=0).
    guardrails = (agent or {}).get("guardrails") or {}
    if loop.COMPRESSION_ENABLED and guardrails.get("compression") != "off":
        registry.update(make_retrieve_tool())

    system = persona
    if extra_system and extra_system.strip():
        system = f"{system}\n\n{extra_system.strip()}".strip()

    # ponytail: one-line prompt guard for text-generation loops (no harness hook covers this)
    loop_guard = ("If a tool keeps returning the same error, stop retrying — report the "
                  "blocker and wait for input instead of looping.")
    system = f"{system}\n\n{loop_guard}".strip() if system else loop_guard

    if guardrails.get("focusOutput"):
        system = f"{system}\n\n{_FOCUS_STYLE}".strip()

    if skills:
        catalog = build_skill_context(skills)
        if catalog:
            system = f"{system}\n\n{catalog}".strip()
            registry["load_skill"] = make_load_skill_tool(skills)
            registry["load_skill_resource"] = make_load_skill_resource_tool(skills)
        # Slash-invoked skills: inject the full body so the model applies them
        # this turn without first calling load_skill. Scoped to the agent's own
        # skills inside build_preloaded_skills.
        if preload_skills:
            preloaded = build_preloaded_skills(preload_skills, skills)
            if preloaded:
                system = f"{system}\n\n{preloaded}".strip()

    # "Skill factory" — an opt-in agent can author its own skills (PRD-14).
    # Off by default (self-modifying, writes to the shared library); gated by the
    # skillFactory guardrail. Independent of whether any skill is attached.
    if ((agent or {}).get("guardrails") or {}).get("skillFactory"):
        registry.update(make_skill_factory_tools(agent_id))
        system = f"{system}\n\n{build_skill_factory_context()}".strip()

    # "Step checklist" — an opt-in set_plan tool that lets the agent publish a
    # live step list for multi-step work (shown to the user by chat surfaces that render it).
    # Per-run + stateless: the model owns the plan via full-replacement calls;
    # nothing is persisted. Off by default; gated by the stepChecklist guardrail.
    if ((agent or {}).get("guardrails") or {}).get("stepChecklist"):
        registry.update(make_plan_tool())
        system = (f"{system}\n\nYou have a set_plan tool. For multi-step work "
                  "(3+ distinct steps), call set_plan first with your step list, and "
                  "call it again as steps start/complete. Revise the plan when findings "
                  "change it — the plan serves the answer, not the reverse. Never use it "
                  "for simple lookups. Steps are shown to the user: write them in the "
                  "user's language and keep each short.").strip()

    # "Your files" — a working folder automatically grants scoped file tools.
    if working_dir.strip():
        try:
            registry.update(make_fs_tools(working_dir))
            registry.update(make_screenshot_tool(working_dir, agent_id))
            registry.update(make_recording_tools(working_dir))
            system = (f"{system}\n\nYou have file tools ({', '.join(FS_TOOL_NAMES)}), document "
                      f"tools ({', '.join(DOC_TOOL_NAMES)} — for PDF/Word/Excel/PowerPoint/CSV) "
                      f"and browser_screenshot / browser_record_start / browser_record_stop "
                      f"tools scoped to the working folder "
                      f"`{working_dir.strip()}`. All paths are relative to it.").strip()
        except FsError as exc:
            # A misconfigured working folder (e.g. pointed at a file, or an
            # unusable path) must not take down the whole run — that surfaced as an
            # HTTP 500 on chat and a "scheduled run failed" in the background.
            # Degrade to no file tools and tell the agent why, so it can report it.
            logger.warning("working folder unusable for agent %s: %s", agent_id or "?", exc)
            system = (f"{system}\n\n⚠️ Your configured working folder `{working_dir.strip()}` is "
                      f"unusable ({exc}); file tools are disabled for this run. Tell the user to "
                      "fix the working folder in this agent's settings.").strip()

    # "Your documents" — PDF/Office/CSV readers scoped to the working folder and
    # to chat-channel attachments (<home>/channels/*/media).
    _doc_roots = ([working_dir.strip()] if working_dir.strip() else []) + [str(config.APP_HOME / "channels")]
    registry.update(make_doc_tools(_doc_roots))
    if not working_dir.strip():
        system = (f"{system}\n\nYou can read documents (PDF, Word, Excel, PowerPoint, CSV, text) with "
                  f"{', '.join(DOC_TOOL_NAMES)}; files people send you in chat arrive as absolute paths.").strip()

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
        # "Your chat" — opt-in: message people on connected chat channels (WhatsApp, Telegram, …).
        if ((agent or {}).get("guardrails") or {}).get("channelSend"):
            registry.update(make_channel_tools(agent_id))
        vault = settings.get("obsidianVault")
        if vault:
            system = (f"{system}\n\nDefault Obsidian vault: `{vault}`. Use this as the "
                      "target when calling deliver(channel='obsidian') unless the user "
                      "specifies a different path.").strip()
        # "Your team" — delegate to other agents (assign_to / ask_agent / team_list).
        # Always on at depth 0; the loop guards live inside the tool factory.
        # ``delegation_chain`` (from the worker, via the task's metadata) threads
        # the visited chain back in so a board handoff can't loop across runs.
        registry.update(make_team_tools(agent_id, chain=delegation_chain, session_id=session_id))
        registry.update(make_inbox_tools(agent_id, session_id))
        team = build_team_context(agent_id)
        if team:
            system = f"{system}\n\n{team}".strip()
        # "Document a repo" — a wiki_ingest_repo tool so the agent can turn a local
        # code folder into a wiki on request ("make a wiki from /path"), and keep it
        # current from a schedule. Runs its own generation loop on this agent's model.
        registry.update(make_repo_wiki_tools(agent))
        registrar_tools = make_wiki_registrar_tools(agent)
        if registrar_tools:
            registry.update(registrar_tools)
            system = (f"{system}\n\nTo link a human-maintained repository as a Wiki, ask the user for the repo path "
                      "and a display name (default: the repo's folder name), then call `wiki_link_propose`. Lead "
                      "with the repo's setup/sync/check actions, not trust settings: if it declares no setup/sync "
                      "command, inspect it (README, Makefile, scripts/) and pass concrete commands in `operations` "
                      "for the user to review. Default the trust tier to repo-tooling; raise read-only only if the "
                      "user distrusts the repo. You cannot approve — tell the user to approve the card in the Wiki "
                      "tab or chat, then poll `wiki_proposal_status`. Post-link changes go through `wiki_ops_propose`. "
                      "Do not use `wiki_ingest_repo` for this: it generates a separate Virtual Worker-managed Wiki.").strip()

    # "Your knowledge" — attached KBs grant a kb_search retrieval tool (RAG).
    knowledge_bases = (agent or {}).get("knowledgeBases") or []
    if knowledge_bases:
        registry.update(make_kb_tools(agent))
        kb_ctx = build_kb_context(knowledge_bases)
        if kb_ctx:
            system = f"{system}\n\n{kb_ctx}".strip()

    # "Your wiki" — attached OKF wikis grant read/search/write tools (PRD-13).
    wikis = (agent or {}).get("wikis") or []
    if wikis:
        registry.update(make_wiki_tools(agent))
        wiki_ctx = build_wiki_context(wikis)
        if wiki_ctx:
            system = f"{system}\n\n{wiki_ctx}".strip()
        # A linked Wiki is a repository workspace — grant read-only fs + shell
        # tools rooted at the repo (Tier 1), so the agent explores it like code.
        repo_tools, repo_note = make_linked_repo_tools(agent)
        if repo_tools:
            registry.update(repo_tools)
            system = f"{system}\n\n{repo_note}".strip()
        # Repo-local skills (§7): a linked repo can ship .claude/skills / .vw/skills.
        # Surface them through load_skill, merged with the agent's own skills. Handle
        # both cases: the skills block already installed the tools (rebuild with the
        # merged sources) or the agent has none (install fresh + add the catalog).
        wiki_skills = collect_wiki_skills(agent)
        if wiki_skills:
            registry["load_skill"] = make_load_skill_tool(skills, wiki_skills=wiki_skills)
            registry["load_skill_resource"] = make_load_skill_resource_tool(skills, wiki_skills=wiki_skills)
            repo_skill_catalog = build_skill_context([], wiki_skills=wiki_skills)
            if repo_skill_catalog:
                system = f"{system}\n\n{repo_skill_catalog}".strip()

    # Intern — task board + shell, bounded by the agent's guardrails.
    if agent_id and role:
        registry.update(make_intern_tools(agent_id))
        g = (agent or {}).get("guardrails") or {}
        shell_mode = g.get("shell", "auto")
        if shell_mode != "off":
            registry["shell"] = make_shell_tool(
                working_dir, mode=shell_mode, extra_denied=g.get("extraDenied") or [],
                allowed_commands=g.get("allowedCommands") or [],
                timeout_s=g.get("shellTimeoutS"),
                extra_env=_shell_env(tool_names))
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

    # The session-pinned machine clock (see the module header note) goes LAST:
    # it is the only volatile line in the prompt, so placing it after the
    # semi-static blocks (skills, memory, team, board) keeps them prefix-cache
    # reusable across cold sessions — a new session re-prefills only this line
    # plus the (new anyway) conversation.
    system = f"{system}\n\n{clock_line(session_id)}".strip()

    temperature = (agent or {}).get("temperature", 0.7)
    top_p = (agent or {}).get("topP")
    top_k = (agent or {}).get("topK")
    repeat_penalty = (agent or {}).get("repeatPenalty")  # None = env default; local upstream only
    reasoning_effort = (agent or {}).get("reasoningEffort") or None  # "" -> None; remote providers only
    return (system or None), registry, temperature, top_p, top_k, repeat_penalty, reasoning_effort
