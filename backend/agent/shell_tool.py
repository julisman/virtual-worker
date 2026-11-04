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
git push, npm publish, …). These bounds are automatic guardrails, not a review
gate. Commands run with ``shell=False`` (argv via shlex), so pipes/redirects/
chaining are inert. See docs/INTERN_AGENT_PRD.md decision D3 + the M3 reorder.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

TIMEOUT_S = 60
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
_READONLY = {
    "ls", "pwd", "cat", "head", "tail", "wc", "tree", "file", "stat", "du", "df",
    "echo", "date", "whoami", "uname", "hostname", "which", "type", "env", "printenv",
    "ps", "top", "uptime", "id", "groups", "sort", "uniq", "cut", "awk", "sed",
    "grep", "egrep", "fgrep", "rg", "diff", "basename", "dirname", "realpath",
    "node", "python", "python3", "go", "java",
}
_GIT_READONLY = {"status", "log", "diff", "show", "branch", "ls-files",
                 "rev-parse", "describe", "blame", "shortlog", "tag"}
# Refused even in workspace mode (outward publish / remote push).
_GIT_BLOCKED = {"push"}
_DANGER_FLAGS = {"-delete", "-exec", "-execdir", "-ok"}


def _classify(argv: list[str], *, workspace: bool) -> tuple[bool, str]:
    """(allowed, reason). Deny-list always wins; workspace mode is otherwise permissive."""
    if not argv:
        return False, "empty command"
    base = os.path.basename(argv[0])
    if base in _DENY:
        return False, f"`{base}` is on the deny-list (destructive/privileged/network) — never run unattended."
    if base == "git":
        sub = next((a for a in argv[1:] if not a.startswith("-")), "")
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
    if base in _READONLY:
        if any(f in argv for f in _DANGER_FLAGS):
            return False, f"`{base}` with a mutating flag is not allowed without a working folder."
        return True, ""
    return False, f"`{base}` needs a working folder (workspace mode); only read-only commands run without one."


def make_shell_tool(working_dir: str | None = None, *, mode: str = "auto",
                    extra_denied: list[str] | tuple[str, ...] = ()) -> dict:
    """``mode`` (a per-agent guardrail): "auto" (workspace if a working folder is
    set, else read-only), "readonly" (force read-only), or "workspace" (force
    side-effectful — only honored when a working folder is set). ``extra_denied``
    adds commands to the always-on deny-list (tighten only)."""
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

    async def shell(command: str = "") -> str:
        command = (command or "").strip()
        if not command:
            return "shell needs a 'command'."
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"could not parse command: {exc}"
        if argv and os.path.basename(argv[0]) in extra:
            return f"refused: `{os.path.basename(argv[0])}` is blocked by this agent's guardrails."
        allowed, reason = _classify(argv, workspace=workspace)
        if not allowed:
            return f"refused: {reason}"
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=base_cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        except asyncio.TimeoutError:
            return f"command timed out after {TIMEOUT_S}s"
        except FileNotFoundError:
            return f"command not found: {argv[0]}"
        except Exception as exc:  # noqa: BLE001
            return f"shell error: {exc}"
        text = (out or b"").decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n…(+{len(text) - MAX_OUTPUT} chars truncated)"
        rc = proc.returncode
        return (f"$ {command}\n(exit {rc})\n" if rc else f"$ {command}\n") + (text or "(no output)")

    return {
        "fn": shell,
        "schema": {
            "type": "function",
            "function": {
                "name": "shell",
                "description": f"Run a shell command on the machine and return its output "
                               f"(tier: {tier}). In workspace mode you can build, test, run git, "
                               f"and generate files inside your working folder. Destructive, "
                               f"privileged, network, and publish commands are always refused.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "the command to run"}},
                    "required": ["command"],
                },
            },
        },
    }
