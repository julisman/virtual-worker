"""MCP client — connect agents to MCP servers via the official ``mcp`` SDK.

Warm pool: a process-lifetime :class:`McpPool` keeps each server's session live
and reuses it across chats. The first run that needs a server pays the spawn;
later runs reuse the warm session — so an agent's second message no longer waits
on cold ``uvx``/``npx`` starts. Sessions are keyed by server id + a fingerprint
of the config, so editing a server transparently respawns it. Servers are
connected in parallel (per-request cost is the slowest server, not the sum) and
each connect is bounded by a per-server timeout.

``McpManager`` is a thin per-scope handle onto that pool (kept so chat, worker,
triggers, and scheduler are unchanged); its context exit does *not* close the
sessions — the pool owns them and tears them down once, at app shutdown.
Discovered tools are exposed to the loop's per-request registry as
``mcp__<server-slug>__<tool>``, so no loop changes are needed (it already
dispatches via the registry).

The SDK is optional: if ``mcp`` isn't installed, everything degrades gracefully
(no MCP tools; agents still work).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from contextlib import AsyncExitStack

logger = logging.getLogger(__name__)

# One retry on a *transient* connect failure. Connecting several servers at once
# means their launchers (`uvx`, `npx`) can briefly race on their shared package
# caches (e.g. uv's "failed to rename … cache" while another `uvx` writes the
# same entry) — a fast, self-healing error. A single staggered retry rides over
# it once the other launcher has finished; a genuine timeout is NOT retried, so
# an unreachable server still skips fast.
_CONNECT_ATTEMPTS = 2


def _connect_timeout() -> float:
    """Per-server cap (seconds) on spawn + initialize + list_tools. Bounds a slow
    or unreachable server so it can't stall the whole chat — the request proceeds
    with that server skipped. Generous by default so a legit cold `uvx`/`npx`
    start isn't killed; override with VW_MCP_CONNECT_TIMEOUT."""
    try:
        return float(os.getenv("VW_MCP_CONNECT_TIMEOUT", "20"))
    except (TypeError, ValueError):
        return 20.0

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    _AVAILABLE = True
except Exception:  # noqa: BLE001 — SDK not installed
    _AVAILABLE = False

try:
    # Protocol-level (JSON-RPC) errors: the server answered, the session is
    # healthy. Anything else raised by call_tool means the transport broke.
    from mcp.shared.exceptions import McpError
except Exception:  # noqa: BLE001
    class McpError(Exception):  # placeholder when the SDK is absent
        pass


def mcp_available() -> bool:
    return _AVAILABLE


def _unwrap(exc: BaseException) -> BaseException:
    """Drill into an ExceptionGroup (raised by anyio TaskGroups inside the MCP
    SDK's transports) to find the leaf exception that actually caused the
    failure — the group's own str() is just "unhandled errors in a TaskGroup
    (N sub-exceptions)", which hides the real cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _describe(exc: BaseException) -> str:
    leaf = _unwrap(exc)
    msg = str(leaf) or repr(leaf)
    return f"{type(leaf).__name__}: {msg}"


def _stringify(result) -> str:
    """Flatten a CallToolResult's content blocks into text for the loop."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    out = "\n".join(p for p in parts if p).strip()
    if getattr(result, "isError", False):
        return f"[tool error] {out}" if out else "[tool error]"
    return out or "(no output)"


# Bin dirs that a GUI launch (Finder/launchd → the desktop app) drops from PATH.
# A packaged Virtual Worker inherits a stripped PATH (roughly /usr/bin:/bin:/usr/sbin:/sbin),
# so stdio launchers like `uvx`/`npx` — installed under ~/.local/bin or Homebrew —
# fail to resolve with "FileNotFoundError: [Errno 2] No such file or directory"
# even though they run fine from a terminal. Prepend the usual user + Homebrew
# locations so the child can find them regardless of how Virtual Worker itself was started.
_EXTRA_BIN_DIRS = (
    "~/.local/bin",
    "/opt/homebrew/bin", "/opt/homebrew/sbin",  # Apple Silicon Homebrew
    "/usr/local/bin", "/usr/local/sbin",         # Intel Homebrew / MacPorts
)


def _child_env(user_env: dict | None) -> dict:
    """Environment for a spawned stdio MCP server: PATH enriched with the standard
    user/Homebrew bin dirs (deduped, existing-only), then the server's own env
    overlaid on top. Always returns a dict so the SDK merges it over its default
    environment, which is what fixes command resolution in the desktop app."""
    parts: list[str] = []
    seen: set[str] = set()
    for d in (*_EXTRA_BIN_DIRS, *os.environ.get("PATH", "").split(os.pathsep)):
        p = os.path.expanduser(d)
        if p and p not in seen and os.path.isdir(p):
            seen.add(p)
            parts.append(p)
    env = {"PATH": os.pathsep.join(parts)}
    if user_env:
        env.update(user_env)
    return env


async def _open_session(stack: AsyncExitStack, cfg: dict):
    transport = cfg.get("transport") or "stdio"
    errlog = None
    if transport == "http":
        url = cfg.get("url")
        if not url:
            raise ValueError("http MCP server needs a url")
        read, write, _ = await stack.enter_async_context(streamablehttp_client(url))
    else:
        command = cfg.get("command")
        if not command:
            raise ValueError("stdio MCP server needs a command")
        params = StdioServerParameters(
            command=command, args=cfg.get("args") or [], env=_child_env(cfg.get("env")),
        )
        # Capture the child process's stderr ourselves (the SDK otherwise just
        # echoes it to the backend's own console) so a crash — bad registry,
        # missing package, auth failure — surfaces in the API error instead of
        # a bare "BrokenResourceError" from the broken stdio pipe.
        errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        stack.callback(errlog.close)
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
    try:
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session
    except Exception as exc:
        if errlog is not None:
            errlog.seek(0)
            stderr_text = errlog.read().strip()
            if stderr_text:
                raise RuntimeError(f"{_describe(exc)} — command stderr:\n{stderr_text[-4000:]}") from exc
        raise


def _make_fn(entry, tool_name: str, slug: str):
    async def fn(**kwargs):
        try:
            result = await entry.session.call_tool(tool_name, kwargs or {})
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, McpError):
                # Transport-level failure (dead subprocess, closed pipe, …):
                # mark the pooled entry dead so alive() turns False and the
                # next run respawns it, instead of erroring forever.
                entry.error = f"session failed during {tool_name}: {_describe(exc)}"
            return f"mcp__{slug}__{tool_name} error: {exc}"
        return _stringify(result)
    return fn


def _fingerprint(cfg: dict) -> str:
    """A stable hash of the bits that decide *what process we spawn*. If any of
    these change (the user edits the server), the pooled session is stale and
    gets respawned; cosmetic fields like the name/slug don't force a respawn."""
    return json.dumps(
        {
            "transport": cfg.get("transport") or "stdio",
            "command": cfg.get("command"),
            "args": cfg.get("args") or [],
            "env": cfg.get("env") or {},
            "url": cfg.get("url"),
        },
        sort_keys=True,
        default=str,
    )


class _ServerSession:
    """One MCP server's transport, owned for the pool's lifetime by a single
    asyncio task. The task opens the session, publishes it, then parks on a close
    event — so the transport's anyio cancel scopes are entered *and* exited on the
    same task (clean teardown), while chat-loop tasks call tools on the session
    concurrently (the SDK's streams are task-agnostic for send/receive)."""

    def __init__(self, cfg: dict, fingerprint: str):
        self.slug = cfg.get("slug") or "server"
        self.fingerprint = fingerprint
        self.ready = asyncio.Event()
        self.session = None
        self.tools = None
        self.error: str | None = None
        self._close = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(cfg))

    async def _run(self, cfg: dict) -> None:
        try:
            async with AsyncExitStack() as stack:
                session, tools = await self._connect(stack, cfg)
                self.session, self.tools = session, tools
                self.ready.set()
                # Hold the transport open until the pool closes this server; chat
                # loops call tools on the session in the meantime.
                await self._close.wait()
        except Exception as exc:  # noqa: BLE001 — incl. TimeoutError from asyncio.timeout
            # A *connect* failure (before ready) is reported to the caller so the
            # server is skipped and re-tried next time. A failure while parked
            # (e.g. a benign BrokenResourceError at close) must not overwrite a
            # session that already served requests — re-raise so close() logs it.
            if not self.ready.is_set():
                self.error = _describe(exc)
                self.ready.set()
            else:
                raise

    async def _connect(self, stack: AsyncExitStack, cfg: dict):
        """Open the session + list tools, retrying once on a transient failure.
        Each attempt runs in its own stack so a failed one is torn down at once;
        the successful attempt's resources are adopted into ``stack`` so the
        transport stays live for the pool's lifetime."""
        for attempt in range(_CONNECT_ATTEMPTS):
            trial = AsyncExitStack()
            try:
                async with asyncio.timeout(_connect_timeout()):
                    session = await _open_session(trial, cfg)
                    resp = await session.list_tools()
                await stack.enter_async_context(trial.pop_all())
                return session, resp.tools
            except TimeoutError:
                await trial.aclose()
                raise  # genuinely unresponsive — skip fast, don't retry
            except Exception as exc:  # noqa: BLE001
                await trial.aclose()
                if attempt == _CONNECT_ATTEMPTS - 1:
                    raise
                logger.info("MCP server %s connect attempt %d failed (%s); retrying",
                            self.slug, attempt + 1, _describe(exc))
                # Stagger so servers that just collided don't re-collide.
                await asyncio.sleep(random.uniform(0.2, 0.6))

    def alive(self) -> bool:
        """Usable from the current loop? False once the owning task has ended, if
        it failed to connect, or if we've crossed into a different event loop
        (e.g. a fresh test loop) where this session's primitives don't belong."""
        try:
            on_loop = self._loop is asyncio.get_running_loop()
        except RuntimeError:
            on_loop = False
        return on_loop and self.error is None and not self._task.done()

    async def close(self) -> None:
        self._close.set()
        for r in await asyncio.gather(self._task, return_exceptions=True):
            if isinstance(r, Exception):
                logger.warning("MCP session cleanup raised (ignored): %s", _describe(r))


class McpPool:
    """Process-lifetime pool of live MCP sessions, keyed by server id + a
    fingerprint of its spawn config. Connects an agent's servers in parallel on
    first use and reuses the warm sessions on every later run."""

    def __init__(self):
        self._servers: dict[str, _ServerSession] = {}
        self._lock = asyncio.Lock()
        self._reaping: set[asyncio.Task] = set()

    async def build_registry(self, servers: list[dict]) -> dict[str, dict]:
        """Acquire every enabled server (warm reuse or parallel cold spawn), list
        its tools, and return registry entries keyed ``mcp__<slug>__<tool>``. A
        server that fails or times out is skipped (logged) — never fatal."""
        registry: dict[str, dict] = {}
        if not _AVAILABLE:
            if servers:
                logger.warning("MCP servers configured but the 'mcp' SDK is not installed")
            return registry
        enabled = [c for c in servers if c and c.get("enabled", True)]
        if not enabled:
            return registry
        acquired = await asyncio.gather(*(self._acquire(cfg) for cfg in enabled))
        for cfg, entry in zip(enabled, acquired):
            if not entry:
                continue
            slug, tools = entry.slug, entry.tools
            # Per-server allowlist: expose only the named tools. Empty/absent =
            # no filter (every advertised tool), so pre-allowlist configs are
            # unchanged. Names are the bare tool names, without the mcp__ prefix.
            allowed = {str(t).strip() for t in (cfg.get("allowedTools") or []) if str(t).strip()}
            for tool in tools:
                if allowed and tool.name not in allowed:
                    continue
                qualified = f"mcp__{slug}__{tool.name}"
                registry[qualified] = {
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": qualified,
                            "description": tool.description or f"{tool.name} (via {slug})",
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        },
                    },
                    "fn": _make_fn(entry, tool.name, slug),
                    # MCP schemas are big and numerous — an agent may attach several
                    # servers (dozens of tools) it only occasionally calls. Mark them
                    # "deferred" so loop.run() can hide them behind a search_tools
                    # meta-tool and keep the per-turn prompt small. See agent/loop.py.
                    "deferred": True,
                }
        return registry

    async def _acquire(self, cfg: dict):
        """Return ``(slug, session, tools)`` for one server — reusing the warm
        session when possible, else spawning it — or ``None`` if it won't connect."""
        key = str(cfg.get("id") or cfg.get("slug") or "server")
        fp = _fingerprint(cfg)
        # The lock only guards the dict check-and-insert (no I/O inside), so
        # spawns of *different* servers still run in parallel; a concurrent
        # request for the *same* key finds the in-flight entry and awaits it.
        async with self._lock:
            entry = self._servers.get(key)
            if entry is not None and (entry.fingerprint != fp or not entry.alive()):
                self._servers.pop(key, None)
                self._reap(entry)
                entry = None
            if entry is None:
                entry = _ServerSession(cfg, fp)
                self._servers[key] = entry
        await entry.ready.wait()
        if entry.error is not None:
            # Don't cache a failed connect — drop it so a later run retries fresh.
            async with self._lock:
                if self._servers.get(key) is entry:
                    self._servers.pop(key, None)
            logger.warning("MCP server %s connect/list failed: %s", entry.slug, entry.error)
            return None
        return entry

    def _reap(self, entry: _ServerSession) -> None:
        """Close a stale entry in the background (best-effort). If it belongs to a
        now-defunct loop there's nothing to await — just let it be collected."""
        try:
            if entry._loop is not asyncio.get_running_loop():
                return
        except RuntimeError:
            return
        task = asyncio.create_task(entry.close())
        self._reaping.add(task)
        task.add_done_callback(self._reaping.discard)

    async def invalidate(self, server_id: str) -> None:
        """Drop and close a pooled server by id (e.g. when it's deleted), so its
        subprocess doesn't linger. A no-op if it isn't pooled."""
        async with self._lock:
            entry = self._servers.pop(server_id, None)
        if entry is not None and entry.alive():
            await entry.close()

    async def close_all(self) -> None:
        async with self._lock:
            entries = list(self._servers.values())
            reaping = list(self._reaping)
            self._servers.clear()
        await asyncio.gather(
            *(e.close() for e in entries if e.alive()), *reaping, return_exceptions=True
        )


# One pool per event loop. In production that's a single pool for the process's
# lifetime; each test runs on its own loop and transparently gets a fresh pool,
# so the pool's asyncio primitives never leak across loops.
_POOL: "McpPool | None" = None
_POOL_LOOP = None


def get_pool() -> McpPool:
    global _POOL, _POOL_LOOP
    loop = asyncio.get_running_loop()
    if _POOL is None or _POOL_LOOP is not loop:
        _POOL = McpPool()
        _POOL_LOOP = loop
    return _POOL


async def close_pool() -> None:
    """Tear down the current loop's pool (app shutdown; test cleanup)."""
    global _POOL, _POOL_LOOP
    pool, _POOL, _POOL_LOOP = _POOL, None, None
    if pool is not None:
        await pool.close_all()


class McpManager:
    """Thin per-scope handle onto the process-wide warm :class:`McpPool`. Kept as
    an async context manager so existing call sites (chat, worker, triggers,
    scheduler) are unchanged — but the context exit does *not* close sessions;
    they stay warm in the pool for the next run and are torn down once, at app
    shutdown via :func:`close_pool`."""

    async def __aenter__(self) -> "McpManager":
        return self

    async def __aexit__(self, *exc) -> bool | None:
        return False

    async def build_registry(self, servers: list[dict]) -> dict[str, dict]:
        return await get_pool().build_registry(servers)


async def list_server_tools(cfg: dict) -> list[dict]:
    """Connect once, list tools, disconnect. Powers the 'test connection' endpoint."""
    if not _AVAILABLE:
        raise RuntimeError("the 'mcp' Python SDK is not installed")
    result: list[dict] | None = None
    try:
        async with AsyncExitStack() as stack:
            session = await _open_session(stack, cfg)
            resp = await session.list_tools()
            result = [{"name": t.name, "description": t.description or "",
                       "inputSchema": t.inputSchema or {}} for t in resp.tools]
    except Exception as exc:  # noqa: BLE001
        if result is not None:
            # The tools were already listed successfully — this exception came
            # from closing the session (a stdio close can race benignly with a
            # trailing stdout notification), not from the connection itself.
            logger.warning("MCP server %r: benign cleanup error after a successful list: %s",
                            cfg.get("slug") or cfg.get("name"), _describe(exc))
            return result
        logger.exception("MCP test connection failed for server %r", cfg.get("slug") or cfg.get("name"))
        raise RuntimeError(_describe(exc)) from exc
    return result
