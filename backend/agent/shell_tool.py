"""A `shell` tool so the intern can "manage the laptop" — bounded, not gated.

Two tiers, chosen automatically:

- **workspace** (the intern has a working folder): runs side-effectful commands
  too — builds, tests, git commit, package installs, file generation — with the
  command's working directory pinned to the working folder. This is the
  "autonomous within its workspace" mode: no human approval, real capability.
- **read-only** (no working folder): only the inherently read-only allow-list.

In *both* tiers a hard **deny-list** is refused outright — destructive,
privilege-escalating, exfiltrating, or publishing commands the agent must never
run unattended (rm, dd, mkfs, shutdown, sudo, chmod/chown, curl/wget, ssh/scp,
git push, npm publish, …). Commands run with ``shell=False`` (argv via shlex) —
pipelines are built from ``os.pipe()`` junctions and may be chained with
``&&``/``;`` (every segment gated first); redirects, ``||``, backgrounding and
command substitution stay inert. See docs/INTERN_AGENT_PRD.md decision D3.

SECURITY NOTE — this is a guardrail, not a sandbox. The deny-list matches on the
program's basename, so it stops *casual/accidental* destructive commands, but it
is **not** a security boundary against a determined or prompt-injected agent in
**workspace** mode: workspace mode is intentionally permissive (arbitrary
programs, including interpreters, run by design), so an agent that ingests
untrusted input there can run arbitrary code. Real confinement must be OS-level
(a locked-down user, a container, seccomp). The **read-only** tier is the only
place this list acts as a boundary, so it is kept to genuinely inert readers —
general-purpose language interpreters (python/node/go/java) are deliberately
NOT in it, because `python3 -c ...` would otherwise defeat the whole deny-list.
The intern's Verify pillar is a post-hoc quality check, not an action gate.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

# Default per-command kill timeout. Overridable per agent via the
# ``shellTimeoutS`` guardrail (threaded through ``make_shell_tool``) — slow but
# legitimate commands (e.g. a proxied log search) need more than 60s, and the
# per-agent knob keeps that a deliberate choice instead of a global loosening.
TIMEOUT_S = int(os.environ.get("VW_SHELL_TIMEOUT_S", "60"))
TIMEOUT_MIN_S, TIMEOUT_MAX_S = 10, 600


def _timeout_message(seconds: float) -> str:
    return (f"command timed out after {seconds:.0f}s (process killed) — if this was "
            "a slow query, narrow it (smaller time window / limit) and retry.")
MAX_OUTPUT = 8000

# Always refused, in any tier — destructive / escalation / exfil / publish.
_DENY = {
    "rm", "rmdir", "dd", "mkfs", "fdisk", "diskutil", "format", "shred",
    "shutdown", "reboot", "halt", "poweroff", "kill", "killall", "pkill",
    "sudo", "su", "doas", "chmod", "chown", "chgrp", "mount", "umount",
    "curl", "wget", "nc", "ncat", "netcat", "telnet", "ssh", "scp", "sftp", "rsync",
    "launchctl", "systemctl", "crontab", "defaults", "open",
}
# Read-only programs (the only ones allowed when there's no working folder).
# NB: general-purpose language interpreters (python/node/go/java) are
# intentionally absent — in the read-only tier this list is a security boundary,
# and `python3 -c "..."` / `node -e "..."` would let an agent run arbitrary code
# (deleting files, opening sockets) and defeat the entire deny-list. They remain
# available in workspace mode, which is permissive by design.
_READONLY = {
    "ls", "pwd", "cat", "head", "tail", "wc", "tree", "file", "stat", "du", "df",
    "echo", "printf", "nl", "date", "whoami", "uname", "hostname", "which", "type",
    "ps", "top", "uptime", "id", "groups", "sort", "uniq", "cut", "find",
    "grep", "egrep", "fgrep", "rg", "diff", "basename", "dirname", "realpath",
    "jq", "column", "sqlite3",
}
_GIT_READONLY = {"status", "log", "diff", "show", "branch", "ls-files",
                 "rev-parse", "describe", "blame", "shortlog", "tag"}
# Refused even in workspace mode (outward publish / remote push).
_GIT_BLOCKED = {"push"}
_DANGER_FLAGS = {"-delete", "-exec", "-execdir", "-ok"}
# git global options that consume the FOLLOWING token as their value; if we
# don't skip that value we might mistake it for the subcommand (e.g.
# `git -C . push` would read `.` as the subcommand and let the push through).
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                   "--super-prefix", "--exec-path", "--config-env"}


def _git_defines_alias(argv: list[str]) -> bool:
    """True when a git invocation defines or edits an alias — `git -c
    alias.p=push p` (or `git config alias.p push`, then `git p`) would smuggle a
    blocked subcommand past the deny check, so alias definition is refused."""
    it = iter(argv[1:])
    for tok in it:
        if tok in ("-c", "--config-env"):
            if (next(it, "") or "").lower().startswith("alias."):
                return True
        elif tok.startswith("--config-env="):
            if tok.split("=", 1)[1].lower().startswith("alias."):
                return True
        elif tok.startswith("-c") and tok != "-c":
            # glued form: -calias.p=push — the key starts right after "-c"
            if tok[2:].lower().startswith("alias."):
                return True
        elif not tok.startswith("-"):
            # Reached the subcommand: also refuse `git config … alias.*`.
            return tok == "config" and any(a.lower().startswith("alias.") for a in argv[1:])
    return False


def _git_subcommand(argv: list[str]) -> str:
    """The real git subcommand, skipping global options and any values they
    consume. Returns "" if none is found."""
    it = iter(argv[1:])
    for tok in it:
        if not tok.startswith("-"):
            return tok
        # `--opt=value` is self-contained; a bare value-taking option eats the
        # next token so it isn't misread as the subcommand.
        if tok in _GIT_VALUE_OPTS:
            next(it, None)
    return ""


def _within(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or lives under it (both already resolved)."""
    try:
        root = root.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def _classify(argv: list[str], *, workspace: bool,
              exec_roots: tuple[Path, ...] = (), cwd: Path | None = None) -> tuple[bool, str]:
    """(allowed, reason). Deny-list always wins; workspace mode is otherwise
    permissive. ``exec_roots`` enables tier-2 (repo-tooling): a PATH-form program
    (containing ``/``) that resolves to an executable file inside one of the roots
    may run even in the read-only tier — the repo's own tooling."""
    if not argv:
        return False, "empty command"
    base = os.path.basename(argv[0])
    if base in _DENY:
        return False, f"`{base}` is on the deny-list (destructive/privileged/network) — never run unattended."
    if base == "git":
        if _git_defines_alias(argv):
            return False, "defining a git alias is refused — an alias can smuggle a blocked subcommand like `push`."
        sub = _git_subcommand(argv)
        if sub in _GIT_BLOCKED:
            return False, f"`git {sub}` publishes/pushes — refused."
        if workspace or sub in _GIT_READONLY:
            return True, ""
        return False, f"`git {sub}` modifies the repo — needs a working folder (workspace mode)."
    if base == "npm":
        sub = next((a for a in argv[1:] if not a.startswith("-")), "")
        if sub == "publish":
            return False, "`npm publish` is an outward publish — refused."
    if workspace:
        if any(f in argv for f in _DANGER_FLAGS):
            return False, f"`{base}` with a delete/exec flag is refused."
        return True, ""
    # read-only tier
    # Tier-2 (repo-tooling): a program addressed in PATH-form (contains "/") that
    # resolves to an executable file inside an exec_root runs — e.g. `./scripts/kb`.
    # Bare names (python3, kb) are never qualified here: they resolve via PATH, not
    # the repo, so `python3 -c …` and friends stay refused. Deny-list already won
    # above, and _DANGER_FLAGS still apply.
    if exec_roots and "/" in argv[0]:
        raw = Path(argv[0])
        target = (cwd / raw) if (cwd is not None and not raw.is_absolute()) else raw
        try:
            resolved = target.resolve()          # follow symlinks — an in-repo link out is refused
        except OSError:
            resolved = target
        if any(_within(resolved, r) for r in exec_roots) and resolved.is_file() and os.access(resolved, os.X_OK):
            if any(f in argv for f in _DANGER_FLAGS):
                return False, f"`{base}` with a delete/exec flag is refused."
            return True, ""
        # Not a qualifying in-repo executable — fall through to the read-only rules.
    if base == "sqlite3":
        # sqlite3 can write (INSERT/UPDATE/attach) — force the read-only flag so
        # it stays an inert reader in this tier.
        if "-readonly" not in argv[1:]:
            return False, "`sqlite3` must be run with `-readonly` in the read-only tier — re-run as `sqlite3 -readonly <db> …`"
        return True, ""
    if base in _READONLY:
        if any(f in argv for f in _DANGER_FLAGS):
            return False, f"`{base}` with a mutating flag is not allowed without a working folder."
        return True, ""
    return False, (f"`{base}` is not allowed in the read-only shell — only inert readers "
                   "(ls, cat, head, tail, rg, grep, jq, sqlite3 -readonly, …) and this repo's "
                   "own scripts (invoked by path, e.g. ./scripts/x) run here.")


# Pipelines may be chained with `&&` / `;` (bounded), but never redirected,
# backgrounded, or built via command substitution — those either write, escape
# the timeout, or smuggle an unvalidated command past the per-segment gate.
_PIPELINE_REFUSAL = ("refused: this shell runs pipelines (cmd1 | cmd2) chained only with "
                     "`&&` or `;` — no redirects, no `||`/`&`, no command substitution; "
                     "split into separate calls.")
# Most chained pipelines one call may run — keeps a whole chain inside the one
# TIMEOUT_S / MAX_OUTPUT budget instead of letting a model stuff 50 commands in.
MAX_CHAINED_PIPELINES = 8
_PUNCT_CHARS = set("|&;<>")


def _split_chain(command: str) -> tuple[list[tuple[str, list[list[str]]]] | None, str]:
    """Tokenize ``command`` with operator awareness and split it into pipelines
    chained by ``&&`` / ``;``. Returns ``(chain, "")`` on success — each entry is
    ``(op, segments)`` where ``op`` is ``""`` for the first pipeline and the
    linking operator (``&&`` or ``;``) after that — or ``(None, msg)`` when the
    command is refused or unparseable. ``shell=False`` throughout — the tokens
    are argv lists, never handed to a shell."""
    if "$(" in command or "`" in command:
        return None, _PIPELINE_REFUSAL
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""  # '#' is a literal char, not a comment (match shlex.split)
        tokens = list(lexer)
    except ValueError as exc:
        return None, f"could not parse command: {exc}"
    chain: list[tuple[str, list[list[str]]]] = []
    op = ""
    segments: list[list[str]] = []
    cur: list[str] = []

    def _close_pipeline(next_op: str) -> str:
        nonlocal op, segments, cur
        segments.append(cur)
        cur = []
        if any(not seg for seg in segments):
            return "could not parse command: empty pipeline segment"
        chain.append((op, segments))
        op, segments = next_op, []
        return ""

    for t in tokens:
        if t and all(ch in _PUNCT_CHARS for ch in t):
            if t == "|":
                segments.append(cur)
                cur = []
            elif t in ("&&", ";"):
                err = _close_pipeline(t)
                if err:
                    return None, err
            else:  # ||, &, redirects, heredocs, ;; and other operator runs
                return None, _PIPELINE_REFUSAL
        else:
            cur.append(t)
    err = _close_pipeline("")
    if err:
        return None, err
    if len(chain) > MAX_CHAINED_PIPELINES:
        return None, f"refused: at most {MAX_CHAINED_PIPELINES} chained pipelines per call."
    return chain, ""


def _format_output(command: str, out: bytes | None, rc: int | None) -> str:
    text = (out or b"").decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n…(+{len(text) - MAX_OUTPUT} chars truncated)"
    return (f"$ {command}\n(exit {rc})\n" if rc else f"$ {command}\n") + (text or "(no output)")


async def _kill_all(procs: list) -> None:
    """Kill every still-running process and reap all of them (no zombies)."""
    for p in procs:
        if p.returncode is None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    for p in procs:
        try:
            await p.wait()
        except Exception:  # noqa: BLE001
            pass


async def _exec_pipeline(segments: list[list[str]], cwd: str,
                         timeout_s: float = TIMEOUT_S,
                         env: dict[str, str] | None = None) -> tuple[str | None, bytes | None, int | None]:
    """Chain ``segments`` with ``os.pipe()`` junctions (no shell). Each segment's
    stdout feeds the next segment's stdin; only the last segment's stderr is
    captured (merged into stdout, like a typical shell), intermediates' stderr is
    discarded. The whole spawn+read is bounded by ``timeout_s``. Returns
    ``(error_message, None, None)`` on failure to run, else ``(None, out, rc)``
    so the caller can format the output and chain on the exit code."""
    procs: list = []
    prev_read: int | None = None
    try:
        for i, seg in enumerate(segments):
            last = i == len(segments) - 1
            r = w = None
            if not last:
                r, w = os.pipe()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *seg, cwd=cwd, env=env,  # env=None inherits the parent's, unchanged
                    stdin=prev_read,  # None for the first segment
                    stdout=(w if not last else asyncio.subprocess.PIPE),
                    stderr=(asyncio.subprocess.DEVNULL if not last else asyncio.subprocess.STDOUT),
                )
            except FileNotFoundError:
                for fd in (prev_read, r, w):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                await _kill_all(procs)
                return f"command not found: {seg[0]}", None, None
            procs.append(proc)
            # The child has dup'd the fds it needs — the parent must drop its copy
            # so EOF propagates when the writer exits.
            if prev_read is not None:
                os.close(prev_read)
                prev_read = None
            if not last:
                os.close(w)
                prev_read = r
        out, _ = await asyncio.wait_for(procs[-1].communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        if prev_read is not None:
            try:
                os.close(prev_read)
            except OSError:
                pass
        await _kill_all(procs)
        return _timeout_message(timeout_s), None, None
    except Exception as exc:  # noqa: BLE001
        if prev_read is not None:
            try:
                os.close(prev_read)
            except OSError:
                pass
        await _kill_all(procs)
        return f"shell error: {exc}", None, None
    # The last segment is done and its output read — upstream output can go
    # nowhere useful now, so kill any upstream still running (an EPIPE-ignoring
    # writer would otherwise hang this await forever) and reap all of them.
    await _kill_all(procs[:-1])
    return None, out, procs[-1].returncode


async def _exec_single(argv: list[str], cwd: str,
                       timeout_s: float = TIMEOUT_S,
                       env: dict[str, str] | None = None) -> tuple[str | None, bytes | None, int | None]:
    """Run one plain command; same ``(error, out, rc)`` contract as ``_exec_pipeline``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env,  # env=None inherits the parent's, unchanged
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        # Kill + reap, or the hung command lives on as an orphan forever.
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return _timeout_message(timeout_s), None, None
    except FileNotFoundError:
        return f"command not found: {argv[0]}", None, None
    except Exception as exc:  # noqa: BLE001
        return f"shell error: {exc}", None, None
    return None, out, proc.returncode


def make_shell_tool(working_dir: str | None = None, *, mode: str = "auto",
                    extra_denied: list[str] | tuple[str, ...] = (),
                    allowed_commands: list[str] | tuple[str, ...] = (),
                    exec_roots: tuple[Path | str, ...] = (),
                    timeout_s: int | None = None,
                    extra_env: dict[str, str] | None = None) -> dict:
    """``mode`` (a per-agent guardrail): "auto" (workspace if a working folder is
    set, else read-only), "readonly" (force read-only), or "workspace" (force
    side-effectful — only honored when a working folder is set). ``extra_denied``
    adds commands to the always-on deny-list (tighten only). ``allowed_commands``
    is an operator-set allow-list: a program named here runs regardless of the
    tier or the built-in deny-list — an explicit, per-agent escape hatch the
    operator opts into (e.g. permit `curl` for one agent). A command listed in
    *both* ``extra_denied`` and ``allowed_commands`` is refused — deny wins over
    allow so a tightening can't be silently undone. ``timeout_s`` (the
    ``shellTimeoutS`` guardrail) overrides the per-command kill timeout;
    None/0 keeps the module default, values clamp to [10, 600]."""
    wd = (working_dir or "").strip()
    if mode == "readonly":
        workspace = False
    elif mode == "workspace":
        workspace = bool(wd)
    else:  # auto
        workspace = bool(wd)
    base_cwd = wd or str(Path.home())
    tier = "workspace" if workspace else "read-only"
    extra = {str(c).strip() for c in extra_denied if str(c).strip()}
    allow = {str(c).strip() for c in allowed_commands if str(c).strip()} - extra
    roots = tuple(Path(r) for r in exec_roots)
    try:
        _t = int(timeout_s or 0)
    except (TypeError, ValueError):
        _t = 0
    kill_after = TIMEOUT_S if _t <= 0 else max(TIMEOUT_MIN_S, min(_t, TIMEOUT_MAX_S))
    # ``extra_env`` (e.g. VW_API_BASE / VW_AGENT_BROWSER announcing the agent
    # context to a CLI) is layered ON TOP of the inherited environment; when
    # empty we pass env=None so the child inherits the parent's env untouched.
    child_env = {**os.environ, **extra_env} if extra_env else None

    async def shell(command: str = "") -> str:
        command = (command or "").strip()
        if not command:
            return "shell needs a 'command'."
        chain, err = _split_chain(command)
        if chain is None:
            return err
        # Gate every segment of every pipeline as its own command (deny wins
        # over allow) BEFORE anything runs; the first failing segment refuses
        # the whole call — a chain must never partially execute past a refusal.
        multi = len(chain) > 1 or len(chain[0][1]) > 1
        for _op, segments in chain:
            for seg in segments:
                base = os.path.basename(seg[0])
                if base in extra:
                    return f"refused: `{base}` is blocked by this agent's guardrails."
                # Operator allow-list: an explicitly permitted program bypasses the
                # tier + deny-list. Everything else goes through the automatic bounds.
                if base in allow:
                    continue
                allowed, reason = _classify(seg, workspace=workspace, exec_roots=roots, cwd=Path(base_cwd))
                if not allowed:
                    if multi:
                        return f"refused: {reason} (segment: {shlex.join(seg)})"
                    return f"refused: {reason}"
        blocks: list[str] = []
        last_rc = 0
        for op, segments in chain:
            # Keep the user's own text as the header for a lone pipeline (the
            # original contract); reconstruct per-pipeline headers for a chain.
            cmd_str = command if len(chain) == 1 else " | ".join(shlex.join(seg) for seg in segments)
            if op == "&&" and last_rc != 0:
                blocks.append(f"$ {cmd_str}\n(skipped: previous command failed)")
                continue
            if len(segments) > 1:
                run_err, out, rc = await _exec_pipeline(segments, base_cwd, timeout_s=kill_after, env=child_env)
            else:
                run_err, out, rc = await _exec_single(segments[0], base_cwd, timeout_s=kill_after, env=child_env)
            if run_err is not None:
                blocks.append(run_err if len(chain) == 1 else f"$ {cmd_str}\n{run_err}")
                last_rc = 1
            else:
                blocks.append(_format_output(cmd_str, out, rc))
                last_rc = rc or 0
        return "\n\n".join(blocks)

    return {
        "fn": shell,
        "kind": "shell",
        "schema": {
            "type": "function",
            "function": {
                "name": "shell",
                "description": f"Run a shell command on the machine and return its output "
                               f"(tier: {tier}). You may pipe commands (cmd1 | cmd2) and chain "
                               f"pipelines with && or ; (at most {MAX_CHAINED_PIPELINES} per call) "
                               f"— prefer one chained call over several round-trips. Commands may "
                               f"be piped with `|` and chained with `&&` or `;` only — redirects "
                               f"(`>`, `<`), `||`, `&` (backgrounding), and command substitution "
                               f"(`$(...)`, backticks) are rejected. Each command is killed "
                               f"after {kill_after}s — plan slow queries (e.g. log searches) "
                               f"with narrow windows/limits so they fit. In workspace mode "
                               f"you can build, test, run git, and generate files inside your "
                               f"working folder. Destructive, "
                               f"privileged, network, and publish commands are always refused"
                               + (f", except these the operator has explicitly allowed: "
                                  f"{', '.join(sorted(allow))}." if allow else "."),
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "the command to run"}},
                    "required": ["command"],
                },
            },
        },
    }
