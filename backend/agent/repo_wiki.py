"""Generate a Knowledge Wiki from a code repository (PRD-13, repo→wiki).

DeepWiki/OpenWiki-style: point an autonomous agent at a local checkout and have
it *read the code* and *write an OKF wiki* describing it — an architecture
overview, one page per major module/subsystem, the key flows, and a glossary,
all cross-linked with ``[[wikilinks]]``. Nothing new is indexed or embedded: the
agent explores with the scoped file tools (``make_fs_tools`` — read/list/glob/
grep, confined to the repo) and records what it learns with the wiki tools
(``make_wiki_tools`` — search/read/write). The pages land in the same OKF bundle
any other agent then reads via ``build_wiki_context``, so a later debugging run
starts already understanding the codebase.

This is a *compose-and-drive* of two existing toolkits, not a new subsystem: it
merges their registries and runs the standard ``agent/loop`` with a documentation
brief. Awaited synchronously — a local-first, single-user operation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent import loop
from agent.fs_tools import make_fs_tools
from agent.wiki import make_wiki_tools
from gateway.providers import resolve_target
from storage import wiki_store

logger = logging.getLogger(__name__)

# A repo pass reads many files and writes many pages — far more tool turns than a
# chat. Give it a generous default ceiling (still bounded so it can't run away).
DEFAULT_MAX_ITERATIONS = 40

# Documentation is a describe-what-exists task, not a creative one — keep the
# model close to deterministic so the pages read as reference, not prose.
_TEMPERATURE = 0.3

_BRIEF = """\
You are documenting a code repository into a knowledge wiki so other agents can
understand this codebase quickly — especially when triaging a bug or picking up a
task. Work autonomously; do not ask questions.

Explore the repository with your file tools (`list_dir`, `glob`, `grep`,
`read_file`): start from the root, read the README and manifests (package.json,
pyproject.toml, requirements.txt, go.mod, …) and the top-level folders to learn
the layout, then read the code that matters. Do NOT read every file — sample
enough to describe each area accurately.

Record what you learn as wiki pages with `wiki_write`. Aim for:
- an **overview** page: what the project is, its stack, how the pieces fit, and
  how to run it.
- one page per **major module / subsystem** (a folder or service): its purpose,
  key files, entry points, and how it connects to the rest.
- the **key flows** (a request from entry to response, the build/deploy path,
  background jobs) as their own pages.
- a short **glossary** of domain terms.

Rules:
- One concept per page. Give each a short `title` and `description` in frontmatter
  and a `type` (e.g. Overview, Module, Flow, Glossary).
- Cross-link related pages with `[[page-path]]` (e.g. `[[modules/backend.md]]`) —
  a well-linked wiki with no orphan pages is the goal.
- Cite real file paths from the repo in the body (as `path/to/file.py:line`) so a
  reader can jump straight to the code.
- Before writing, `wiki_list`/`wiki_search` to avoid duplicating an existing page —
  update it instead.
- Write pages as you go, not all at the end. When you have covered the repo, stop
  and give a one-paragraph summary of what you documented.
"""


async def generate_repo_wiki(
    repo_path: str,
    wiki_id: str,
    *,
    model: str | None = None,
    provider: dict | None = None,
    max_iterations: int | None = None,
    agent_id: str | None = None,
) -> dict:
    """Drive an autonomous agent to document the repo at ``repo_path`` into the
    wiki ``wiki_id``.

    ``model``/``provider`` are the egress target for the generation run (as from
    ``resolve_target``); when ``model`` is omitted the active local model is
    resolved. Returns a run summary: ``{wikiId, repoPath, model, pagesBefore,
    pagesAfter, pagesWritten, wikiWrites, toolCalls, runId, error}``. Raises
    ``ValueError`` for a missing wiki, an unreadable repo path, or no active
    model (the caller maps these to 4xx).
    """
    wiki = wiki_store.get_wiki(wiki_id)
    if not wiki:
        raise ValueError(f"no such wiki: {wiki_id!r}")

    root = Path(repo_path).expanduser()
    if not root.is_dir():
        raise ValueError(f"repo path is not a directory: {repo_path!r}")

    # No explicit model ⇒ resolve the active local model (same chain the chat
    # route/worker use, agentless). A caller with a configured agent passes its
    # resolved (model, provider) so the run honors that agent's upstream.
    if not model:
        model, provider = await resolve_target({})
    if not model:
        raise ValueError("no active model — activate one in Models first")

    # Compose the two toolkits into one registry: read the repo + write the wiki.
    registry = make_fs_tools(str(root))
    registry.update(make_wiki_tools({"wikis": [wiki_id]}))

    messages = [{"role": "user",
                 "content": f"Document the repository at `{root}` into the wiki.\n\n{_BRIEF}"}]

    pages_before = len(wiki_store.list_pages(wiki_id))
    tool_calls = 0
    wiki_writes = 0
    run_id: str | None = None
    error: str | None = None

    async for ev in loop.run(
        messages, model, registry,
        temperature=_TEMPERATURE,
        session_id=f"repo-wiki:{wiki_id}",
        agent_id=agent_id,
        max_iterations=max_iterations or DEFAULT_MAX_ITERATIONS,
        provider=provider,
    ):
        et = ev.get("type")
        if et == "tool_call":
            tool_calls += 1
        elif et == "tool_result" and ev.get("name") == "wiki_write":
            wiki_writes += 1
        elif et == "error":
            error = ev.get("message")
        if ev.get("runId"):
            run_id = ev["runId"]

    pages_after = len(wiki_store.list_pages(wiki_id))
    result = {
        "wikiId": wiki_id,
        "repoPath": str(root),
        "model": model,
        "pagesBefore": pages_before,
        "pagesAfter": pages_after,
        "pagesWritten": pages_after - pages_before,
        "wikiWrites": wiki_writes,
        "toolCalls": tool_calls,
        "runId": run_id,
        "error": error,
    }
    logger.info("repo→wiki %s: %d page(s) written from %s (%d tool calls)%s",
                wiki_id, result["pagesWritten"], root, tool_calls,
                f" — error: {error}" if error else "")
    return result


# --------------------------------------------------------------------------- #
# Agent-facing tool — "document a repo into a wiki" from a chat turn (or a
# scheduled run, since the scheduler runs the same compose + registry).
# --------------------------------------------------------------------------- #

def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def make_repo_wiki_tools(agent: dict | None) -> dict[str, dict]:
    """A ``wiki_ingest_repo`` tool: point the agent at a local repo folder and it
    documents the code into a wiki (creating the wiki if needed, updating it in
    place on a re-run). Lets a user just say “make a wiki from /path/to/repo”, and
    lets a weekly schedule keep it current — the generation runs on the agent's
    own model (``resolve_target``)."""
    agent = agent or {}
    agent_id = agent.get("id") or None

    def _find_wiki(wiki: str) -> dict | None:
        wiki = (wiki or "").strip()
        if not wiki:
            return None
        w = wiki_store.get_wiki(wiki)                       # by id
        if w:
            return w
        for row in wiki_store.list_wikis():                 # by name (case-insensitive)
            if row["name"].lower() == wiki.lower():
                return wiki_store.get_wiki(row["id"])
        return None

    async def wiki_ingest_repo(repo_path: str = "", wiki: str = "",
                               max_iterations: int = 0) -> str:
        repo_path = (repo_path or "").strip()
        if not repo_path:
            return "wiki_ingest_repo needs 'repo_path' — a local folder to document."
        root = Path(repo_path).expanduser()
        if not root.is_dir():
            return f"not a directory: {repo_path!r} — pass a local repo folder path."

        # Resolve/create the target wiki. An omitted name resolves to a stable
        # repo-derived name so a weekly refresh ("update the wiki from /dir")
        # reuses the same wiki instead of creating a new one each run. A matching
        # existing wiki is updated in place; otherwise a new wiki is created and
        # attached to this agent so it becomes context on future runs.
        target_name = (wiki or "").strip() or f"{root.name} (repo)"
        existing = _find_wiki(target_name)
        created = False
        if existing:
            w = existing
        else:
            w = wiki_store.create_wiki(target_name, f"Auto-generated from the repo at {root}.")
            created = True
            if agent_id:
                attached = list((agent.get("wikis") or []))
                if w["id"] not in attached:
                    attached.append(w["id"])
                    try:
                        from storage import agent_store
                        agent_store.update_agent(agent_id, {"wikis": attached})
                        agent["wikis"] = attached
                    except Exception:  # noqa: BLE001 — attach is best-effort
                        logger.debug("auto-attach wiki to agent failed", exc_info=True)

        # Run on this agent's configured upstream (local by default).
        try:
            model, provider = await resolve_target(agent)
        except Exception:  # noqa: BLE001
            model, provider = None, None
        if not model and provider is None:
            return ("no active model — activate one in Models (or configure this agent's "
                    "provider) before ingesting a repo.")

        try:
            res = await generate_repo_wiki(
                str(root), w["id"], model=model, provider=provider,
                max_iterations=int(max_iterations) or None, agent_id=agent_id)
        except ValueError as exc:
            return f"wiki_ingest_repo failed: {exc}"

        lead = f"{'Created' if created else 'Updated'} wiki “{w['name']}” ({w['id']})"
        note = f" — now attached to you as context." if created else "."
        tail = (f" Wrote {res['pagesWritten']} new page(s) ({res['wikiWrites']} writes) from "
                f"{root}. Total pages: {res['pagesAfter']}.")
        if res.get("error"):
            tail += f" NOTE: the run reported an error: {res['error']}"
        return lead + note + tail

    return {
        "wiki_ingest_repo": {"fn": wiki_ingest_repo, "kind": "docs", "schema": _schema(
            "wiki_ingest_repo",
            "Document a local code repository into a knowledge wiki: reads the repo and writes "
            "cross-linked pages (overview, per-module, key flows, glossary). Creates the wiki if "
            "the name is new (and attaches it to you), or updates it in place if it already exists "
            "— so it can be re-run on a schedule to keep the wiki current.",
            {"repo_path": {"type": "string", "description": "path to a local repo folder to document"},
             "wiki": {"type": "string",
                      "description": "target wiki name or id; omit to name it after the repo folder"},
             "max_iterations": {"type": "integer",
                                "description": "optional cap on generation tool-loop turns"}},
            ["repo_path"])},
    }
