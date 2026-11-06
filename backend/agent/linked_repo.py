"""Tier-1 linked-repository tools (linked-wiki revamp §3.1).

A linked Wiki is a *repository workspace*, not just a page store. When an agent
is attached to a linked Wiki this module grants read-only file + shell tools
rooted at the **repository root** (not the Wiki markdown root), so the agent can
explore code, scripts, and data under the checkout exactly like Claude Code /
Codex would — with zero registration ceremony.

Everything here is read-only: ``make_fs_tools(repo, readonly=True)`` (no
write/edit) and ``make_shell_tool(repo, mode="readonly")`` (the inert
read-only allow-list, deny-list still enforced). The linked-repo tools are
namespaced (``repo_read_file``, ``repo_grep``, …) so they never shadow an
agent's own working-folder tools, and each takes an optional ``wiki`` arg
(name or id; default = the first linked wiki) to pick between several linked
repositories.
"""

from __future__ import annotations

from agent import fs_tools, shell_tool
from agent.wiki import resolve_attached_wiki
from storage import wiki_store


def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def make_linked_repo_tools(agent: dict | None) -> tuple[dict[str, dict], str]:
    """Build the read-only repository tools for the linked Wikis attached to
    ``agent``. Returns ``(registry, system_prompt_note)`` — ``({}, "")`` when no
    attached wiki is a linked repository."""
    wiki_ids = [w for w in ((agent or {}).get("wikis") or []) if w]
    linked = [wid for wid in wiki_ids if wiki_store.is_linked(wid)]
    if not linked:
        return {}, ""

    shell_off = ((agent or {}).get("guardrails") or {}).get("shell") == "off"

    # Build each linked repo's underlying tool fns once. A repo that has moved or
    # is otherwise unavailable at compose time still registers tools — each call
    # returns the error string rather than crashing compose.
    bundles: dict[str, dict] = {}
    order: list[str] = []
    for wid in linked:
        w = wiki_store.get_wiki(wid) or {}
        name = w.get("name") or wid
        entry: dict = {"name": name, "repo": None, "error": None, "fs": {}, "shell": None, "tier": "readonly"}
        try:
            repo, _root = wiki_store.linked_paths(wid)
            entry["repo"] = repo
            entry["fs"] = fs_tools.make_fs_tools(str(repo), readonly=True)
            tier = wiki_store.get_trust_tier(wid)
            entry["tier"] = tier
            if not shell_off:
                # Tier-2 (repo-tooling) lets the repo's own executables run under
                # the read-only shell; tier-1 (readonly) keeps it to inert readers.
                exec_roots = (repo,) if tier == "repo-tooling" else ()
                entry["shell"] = shell_tool.make_shell_tool(str(repo), mode="readonly",
                                                            exec_roots=exec_roots)["fn"]
        except (wiki_store.WikiError, fs_tools.FsError) as exc:
            entry["error"] = str(exc)
        bundles[wid] = entry
        order.append(wid)

    def _resolve(wiki: str) -> str | None:
        return resolve_attached_wiki(wiki, order)

    def _bad(wiki: str) -> str:
        opts = ", ".join(f"{bundles[w]['name']} ({w})" for w in order)
        return f"no linked wiki {wiki!r} attached to this agent. Linked: {opts}."

    def _fs_dispatcher(tool_name: str):
        async def tool(wiki: str = "", **kwargs) -> str:
            wid = _resolve(wiki)
            if not wid:
                return _bad(wiki)
            b = bundles[wid]
            if b["error"]:
                return f"linked repository “{b['name']}” is unavailable: {b['error']}"
            entry = b["fs"].get(tool_name)
            if not entry:
                return f"{tool_name} is not available for “{b['name']}”."
            return await entry["fn"](**kwargs)
        return tool

    async def repo_shell(command: str = "", wiki: str = "") -> str:
        wid = _resolve(wiki)
        if not wid:
            return _bad(wiki)
        b = bundles[wid]
        if b["shell"] is None:
            return f"read-only shell is not available for “{b['name']}”."
        result = await b["shell"](command=command)
        # The read-only shell prefixes every rejection with "refused"; log it as a
        # demand signal for a future writable tier.
        if isinstance(result, str) and result.startswith("refused"):
            wiki_store.log_shell_refusal(wid, command, result)
        return result

    _wiki_prop = {"wiki": {"type": "string", "description": "which linked wiki (name or id); optional"}}
    registry: dict[str, dict] = {
        "repo_read_file": {"fn": _fs_dispatcher("read_file"), "kind": "read", "schema": _schema(
            "repo_read_file", "Read text from a linked repository (read-only). Three modes: "
            "(1) a whole file — `path` alone. (2) a line range — `path` plus "
            "`start_line`/`end_line` (1-based, inclusive). (3) a batch — `files`, up to 8 refs "
            "\"path\", \"path:LINE\", or \"path:START-END\" (the same path:line shape repo_grep "
            "prints, so paste hits straight in). Prefer ranges/batches over whole files.",
            {"path": {"type": "string", "description": "path relative to the repository root"},
             "start_line": {"type": "integer", "description": "first line to read (1-based, inclusive)"},
             "end_line": {"type": "integer", "description": "last line to read (1-based, inclusive; omit for end-of-file)"},
             "files": {"type": "array", "items": {"type": "string"},
                       "description": "batch: up to 8 refs, each \"path\", \"path:LINE\", or \"path:START-END\""},
             **_wiki_prop}, [])},
        "repo_list_dir": {"fn": _fs_dispatcher("list_dir"), "kind": "repo", "schema": _schema(
            "repo_list_dir", "List files and folders in a directory of a linked repository.",
            {"path": {"type": "string", "description": "directory path (default: the repository root)"},
             **_wiki_prop}, [])},
        "repo_glob": {"fn": _fs_dispatcher("glob"), "kind": "repo", "schema": _schema(
            "repo_glob", "Find files by name pattern (e.g. **/*.py) within a linked repository.",
            {"pattern": {"type": "string", "description": "a glob pattern"}, **_wiki_prop}, ["pattern"])},
        "repo_grep": {"fn": _fs_dispatcher("grep"), "kind": "repo", "schema": _schema(
            "repo_grep", "Search file contents for substrings (case-insensitive) within a linked "
            "repository. Returns path:line: text matches you can paste into repo_read_file. Pass "
            "`patterns` (a list) to match ANY of several substrings in one pass.",
            {"pattern": {"type": "string", "description": "substring to search for"},
             "patterns": {"type": "array", "items": {"type": "string"},
                          "description": "match a line if ANY of these substrings occurs (unioned with `pattern`)"},
             "glob": {"type": "string", "description": "optional file filter, e.g. *.py"}, **_wiki_prop},
            [])},
    }
    # Everything in this module is read-only by design (readonly fs tools + the
    # read-only shell tier) — safe for the loop to dispatch concurrently when
    # the model emits several calls in one turn.
    for entry in registry.values():
        entry["parallel_safe"] = True
    if not shell_off:
        registry["repo_shell"] = {"fn": repo_shell, "parallel_safe": True, "kind": "shell", "schema": _schema(
            "repo_shell", "Run a read-only command pinned to a linked repository's root (the "
            "read-only shell tier — destructive/network/publish commands are refused). You may "
            "pipe commands (cmd1 | cmd2) and chain pipelines with && or ; (at most 8 per call) — "
            "prefer one chained call over several round-trips. No redirects, no ||, no command "
            "substitution.",
            {"command": {"type": "string", "description": "the command to run"}, **_wiki_prop},
            ["command"])}

    note_lines = []
    for wid in order:
        b = bundles[wid]
        root = str(b["repo"]) if b["repo"] else "(currently unavailable)"
        if shell_off:
            shell_hint = ""
        elif b.get("tier") == "repo-tooling":
            shell_hint = " and repo_shell (read-only shell + the repo's own executables, e.g. ./scripts/…; tier: repo-tooling)"
        else:
            shell_hint = " and repo_shell (read-only shell; tier: readonly)"
        note_lines.append(
            f'Linked repository “{b["name"]}” at `{root}` — explore it like a codebase with '
            f"repo_read_file/repo_list_dir/repo_glob/repo_grep{shell_hint}.")
    note = ("## Linked repositories\n"
            "These attached Wikis are read-only repository checkouts. Explore them with your "
            "repo_* tools; follow each repo's own AGENTS.md/CLAUDE.md.\n" + "\n".join(note_lines))
    return registry, note
