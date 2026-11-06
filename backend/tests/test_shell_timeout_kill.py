"""A shell command that hits the timeout must be killed and reaped — the old
path returned "timed out" but left the subprocess running as a permanent
orphan (every hung build/serve command leaked a process)."""

import asyncio
import os
import subprocess

from agent import shell_tool


def test_timeout_kills_the_process(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_tool, "TIMEOUT_S", 0.3)
    tool = shell_tool.make_shell_tool(str(tmp_path))
    out = asyncio.run(tool["fn"](command="sleep 987"))
    assert "timed out" in out
    # The timed-out child must be gone (killed + reaped), not still sleeping.
    procs = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "sleep 987"],
                           capture_output=True, text=True)
    assert procs.stdout.strip() == ""
