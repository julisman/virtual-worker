"""The single agent-run entry point.

Every run surface — the chat route, the always-on worker, the scheduler, the
trigger engine, and chat channels — needs the same setup before the tool loop can run:
resolve the (possibly external) model target, ``compose()`` the agent's system
prompt + tool registry, wire the agent's attached **MCP servers** (compose does
not) and keep them open for the run, then drive ``loop.run`` with the agent's
guardrail knobs. That block used to be copy-pasted in four places, so every new
guardrail had to be threaded through each copy (and the MCP wiring was forgotten
once). ``stream_run`` is that block, once; callers differ only in how they build
messages and consume the event stream.

``stream_run`` is an async generator (streams ``loop.run`` events, for the chat
SSE endpoint and the worker's task-state reconcile); ``execute_run`` is the thin
collector on top for callers that just want ``(text, error, run_id)``.
"""

from __future__ import annotations

from typing import AsyncIterator


def merge_mcp_allow(cfg: dict, selection: list[str] | None) -> dict | None:
    """Effective allowedTools for one server cfg given the agent's selection.

    Server ceiling ∩ agent selection when both are set; either alone; else all.
    The server ceiling is ``cfg["allowedTools"]`` (empty/absent = no ceiling);
    the agent ``selection`` is the tool names it picked for this server —
    ``None`` (key absent) = all, an explicit empty list = NONE (the UI's
    "uncheck all"). Names are bare, without the ``mcp__<slug>__`` prefix.

    Returns ``cfg`` unchanged when no filter applies, else a shallow copy with
    ``allowedTools`` set to the effective list. Returns ``None`` — caller drops
    the server — when it can expose nothing: an explicit empty selection, or a
    selection wholly outside the ceiling.
    """
    ceiling = [str(t).strip() for t in (cfg.get("allowedTools") or []) if str(t).strip()]
    if selection is None:
        return cfg  # no per-agent choice → server ceiling (if any) applies unchanged
    picked = [str(t).strip() for t in selection if str(t).strip()]
    if not picked:
        return None  # explicit "none" — the server contributes no tools
    if not ceiling:
        return {**cfg, "allowedTools": picked}
    effective = [t for t in picked if t in set(ceiling)]
    if not effective:
        return None  # selection wholly outside the ceiling → nothing to expose
    return {**cfg, "allowedTools": effective}


def resolve_lazy_tools(guardrails: dict | None, target: dict | None) -> bool:
    """The ``lazyTools`` guardrail policy → the loop's ``lazy_tools`` flag.

    "off"  → always expose every MCP schema directly.
    "on"   → always hide big MCP toolsets behind search_tools.
    "auto" (default) → lazy only on a remote provider (``target`` set): a paid
    model handles the discovery hop and benefits from the smaller prompt, while
    small LOCAL models reliably fail the extra hop — they announce the action
    ("saya panggil dulu MCP…") instead of making the search_tools call.
    """
    mode = (guardrails or {}).get("lazyTools") or "auto"
    if mode == "off":
        return False
    if mode == "on":
        return True
    return target is not None


async def stream_run(
    agent: dict,
    messages: list[dict],
    *,
    session_id: str,
    extra_system: str | None = None,
    requested_model: str | None = None,
    tools_override: list[str] | None = None,
    preload_skills: list[str] | None = None,
    temperature_override: float | None = None,
    reasoning_effort_override: str | None = None,
    task_id: str | None = None,
    resolved: tuple | None = None,
    delegation_chain: tuple[str, ...] | None = None,
) -> AsyncIterator[dict]:
    """Compose the agent's full runtime and stream its ``loop.run`` events.

    ``resolved`` lets a caller that already did ``resolve_target`` (the worker,
    which needs the model for its no-model check + verify) pass it in to avoid a
    second resolve; otherwise the target is resolved here (honoring
    ``requested_model``). ``tools_override``/``preload_skills``/
    ``temperature_override`` are the chat route's per-request overrides.
    """
    # Lazy imports break the compose↔caller import cycles.
    from agent import context_window as cw
    from agent import loop
    from agent.compose import compose
    from agent.mcp_client import McpManager
    from gateway.providers import resolve_target
    from storage import mcp_store

    a = agent or {}
    model, target = resolved if resolved is not None else await resolve_target(agent, requested_model)
    system, registry, temperature, top_p, top_k, repeat_penalty, reasoning_effort = compose(
        agent, extra_system=extra_system,
        tools_override=tools_override, preload_skills=preload_skills,
        delegation_chain=delegation_chain, session_id=session_id)
    if temperature_override is not None:
        temperature = temperature_override
    if reasoning_effort_override is not None:
        # "" from the API means "explicitly default" — treat it as unset (None).
        reasoning_effort = reasoning_effort_override or None
    # Generic vision fallback: a blind agent that received an inline image gets it
    # transcribed by its vision helper before the loop runs. will_describe is the
    # cheap predicate (no decode/egress) gating the transient status event.
    from agent import vision_helper
    if await vision_helper.will_describe(a, model, target, messages):
        yield {"type": "vision_status", "state": "reading_image"}
    messages = await vision_helper.describe_message_images(a, model, target, messages)
    g = a.get("guardrails") or {}
    # Attach the agent's MCP servers (compose() does not) so a headless run has
    # the same tools as chat; kept open for the whole run. Overlay the per-agent,
    # per-server tool selection onto each server's ceiling — a server the agent
    # narrowed to tools outside its ceiling is dropped (nothing to expose).
    # PRD-23: resolve the context window ONCE per run (single source of truth,
    # shared with the agents GET route). Window mode with an unresolved window
    # falls back to chars mode (FR-15) — logged, never crashes.
    context_mode = g.get("contextMode") or "chars"
    window = window_source = None
    eff_reserve = 0
    if context_mode == "window":
        provider_id = (target or {}).get("provider") if target else None
        window, window_source = await cw.resolve_window(
            model=model, provider_id=provider_id,
            guardrail_context_tokens=g.get("contextTokens") or 0, allow_probe=True)
        if window:
            eff_reserve = cw.effective_reserve(window, g.get("reserveTokens") or 0)
        else:
            import logging
            logging.getLogger(__name__).info(
                "context window unresolved for model %s — falling back to chars mode", model)
            context_mode = "chars"

    tool_allow = a.get("mcpToolAllow") or {}
    mcp_servers = []
    for i in (a.get("mcpServers") or []):
        s = mcp_store.get_server(i)
        if not s:
            continue
        merged = merge_mcp_allow(s, tool_allow.get(i))
        if merged is not None:
            mcp_servers.append(merged)

    async with McpManager() as mcp:
        registry.update(await mcp.build_registry(mcp_servers))
        async for ev in loop.run(
            messages, model, registry, temperature,
            session_id=session_id, system=system,
            top_p=top_p, top_k=top_k, repeat_penalty=repeat_penalty,
            reasoning_effort=reasoning_effort,
            agent_id=a.get("id"), task_id=task_id,
            max_iterations=g.get("maxIterations"), wrapup_warn_at=g.get("wrapupWarnAt"),
            max_output_tokens=g.get("maxOutputTokens"),
            provider=target,
            tight_context=bool(g.get("tightContext")),
            context_chars=g.get("contextChars"),
            auto_compact=bool(g.get("autoCompact")),
            compact_at=g.get("compactAt"),
            context_mode=context_mode,
            context_window=window,
            context_window_source=window_source,
            effective_reserve=eff_reserve,
            compact_keep=g.get("compactKeep") or "messages",
            compact_keep_amount=g.get("compactKeepAmount") or 0,
            mcp_tool_cap=g.get("mcpToolResultChars"),
            wiki_tool_cap=g.get("wikiReadChars"),
            compression=g.get("compression") != "off",
            lazy_tools=resolve_lazy_tools(g, target),
            hide_reasoning=g.get("reasoning") == "off",
        ):
            yield ev


async def execute_run(agent: dict, prompt: str, session_id: str,
                      history: list[dict] | None = None,
                      extra_system: str | None = None,
                      on_event=None) -> tuple[str, str | None, str]:
    """Collect a full run into ``(text, error, run_id)``. ``history`` (prior
    ``{role, content}`` turns) is prepended for multi-turn context; ``run_id`` is
    the Debug trace id so a caller can deep-link it."""
    return await execute_run_streamed(agent, prompt, session_id, history=history,
                                      extra_system=extra_system, on_event=on_event)


async def execute_run_streamed(agent: dict, prompt: str, session_id: str,
                               history: list[dict] | None = None,
                               extra_system: str | None = None,
                               on_delta=None, on_event=None) -> tuple[str, str | None, str]:
    """Like ``execute_run``, but also awaits ``on_delta(cumulative_text)`` after
    every text delta — lets a caller mirror progress into an external
    "typewriter" message (chat-channel streaming) as the run generates, without
    re-implementing the ``stream_run`` consumption loop. With ``on_delta=None``
    this is exactly ``execute_run``."""
    from agent import loop
    messages = list(history or []) + [{"role": "user", "content": prompt}]
    text_parts: list[str] = []
    last_narration = ""  # last substantive pre-tool-call segment, kept as a fallback
    err: str | None = None
    run_id = ""
    async for ev in stream_run(agent, messages, session_id=session_id,
                               extra_system=extra_system):
        t = ev.get("type")
        if t == "text":
            text_parts.append(ev.get("delta", ""))
            if on_delta:
                await on_delta("".join(text_parts))
        elif t in {"tool_call", "tool_result"}:
            if t == "tool_call":
                # Text streamed before a tool call is the model narrating its
                # next step ("Let me read…"), not the answer. Every caller of
                # this collector delivers ONE final message (chat channels, trigger
                # delivery), so keep only the segment after the LAST tool call
                # — otherwise every interim narration line leaks into the reply.
                # The dropped segment is stashed, NOT lost: reasoning models
                # (DeepSeek) sometimes end a long run with a content-empty final
                # turn, and without the stash a 2.5-minute investigation collapses
                # to "(no response)" even though the narration held real findings.
                # Announcement-only lines ("Let me check…") are never stashed —
                # delivered as a fallback they'd read as the answer — so an
                # earlier substantive finding survives later announcements.
                if (seg := "".join(text_parts).strip()) and not (
                        len(seg) < 600 and loop._ANNOUNCE_RE.search(seg)):
                    last_narration = seg
                text_parts.clear()
            if on_event:
                # Keep the raw loop event available to lightweight progress UIs.
                # In particular, tool_call is emitted before the tool runs and
                # carries the name needed for a deterministic status line.
                await on_event(ev)
        elif t == "error":
            err = ev.get("message") or "run failed"
        if ev.get("runId"):
            run_id = ev["runId"]
    final = "".join(text_parts).strip()
    if not final and not err and last_narration:
        # The final turn produced no visible text (e.g. a reasoning-only wrap-up).
        # Deliver the last substantive narration instead of an empty reply — a
        # degraded partial answer beats "(no response)" for every consumer.
        final = (last_narration
                 + "\n\n_(the run stopped before writing a final answer — the above "
                   "is the last progress it reported)_")
    return final, err, run_id
