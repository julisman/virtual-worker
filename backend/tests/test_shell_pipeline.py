"""Pipes-only pipeline support in the shell tool: `cmd1 | cmd2` runs (shell=False,
os.pipe junctions), one pipeline per call, no redirects/chaining/substitution.
Per-segment gating is identical to a single command; the first failing segment
refuses the whole pipeline. Hermetic — real echo/grep/tr + in-repo stub scripts,
no network, no model."""

import asyncio
import shutil

import pytest

from agent import shell_tool
from agent.shell_tool import make_shell_tool


def _call(tool, **kw):
    return asyncio.run(tool["fn"](**kw))


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    return r


def _script(repo, name, body):
    p = repo / "scripts" / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


# --- happy path ------------------------------------------------------------- #

def test_simple_pipe_runs(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo hi | grep hi")
    assert "refused" not in out and "hi" in out


def test_stdin_flows_through_the_pipe_tier2(repo):
    # A tier-2 repo script that echoes stdin — data must flow across the junction.
    _script(repo, "kb", "cat")
    t = make_shell_tool(str(repo), mode="readonly", exec_roots=(repo,))
    out = _call(t, command="printf '%s' hello | ./scripts/kb recall log")
    assert "refused" not in out and "hello" in out


def test_allowed_command_bypasses_in_a_piped_segment(tmp_path):
    # `tr` is not a built-in read-only reader; the operator allow-list lets the
    # piped segment through and it executes.
    t = make_shell_tool(str(tmp_path), mode="readonly", allowed_commands=["tr"])
    out = _call(t, command="echo abc | tr a-z A-Z")
    assert "refused" not in out and "ABC" in out


# --- refusals: redirects / substitution / backgrounding ---------------------- #

@pytest.mark.parametrize("cmd", [
    "ls || ls", "ls & ls",
    "ls > f", "ls >> f", "cat < f", "echo $(ls)", "echo `ls`",
])
def test_redirects_and_disallowed_operators_refused(tmp_path, cmd):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command=cmd)
    assert out == shell_tool._PIPELINE_REFUSAL


# --- && / ; chaining --------------------------------------------------------- #

def test_and_chain_runs_both(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo one && echo two")
    assert "refused" not in out and "one" in out and "two" in out


def test_semicolon_chain_runs_both(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo one ; echo two")
    assert "refused" not in out and "one" in out and "two" in out


def test_and_chain_skips_after_failure(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="grep needle missing-file && echo never")
    assert "skipped: previous command failed" in out
    assert "\nnever" not in out


def test_semicolon_chain_continues_after_failure(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="grep needle missing-file ; echo still-here")
    assert "still-here" in out


def test_chained_pipelines_with_pipes(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo abc | grep abc && echo def | grep def")
    assert "refused" not in out and "abc" in out and "def" in out


def test_refused_segment_refuses_whole_chain_before_anything_runs(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo x && rm -rf /")
    assert out.startswith("refused") and "rm" in out


def test_chain_cap_refused(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    n = shell_tool.MAX_CHAINED_PIPELINES + 1
    out = _call(t, command=" ; ".join(["echo x"] * n))
    assert out.startswith("refused") and str(shell_tool.MAX_CHAINED_PIPELINES) in out


# --- per-segment gating ----------------------------------------------------- #

def test_refused_segment_refuses_whole_pipeline_and_names_it(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo x | xargs rm -rf /")
    assert out.startswith("refused")
    assert "xargs" in out          # the offending segment is named


def test_extra_denied_blocks_a_piped_segment(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly", extra_denied=["grep"])
    out = _call(t, command="ls | grep x")
    assert out.startswith("refused") and "grep" in out


def test_empty_segment_refused(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo x | | grep x")
    assert "empty pipeline segment" in out


# --- sqlite3 must stay read-only in a pipe ---------------------------------- #

def test_piped_sqlite3_without_readonly_refused(tmp_path):
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command="echo x | sqlite3 db.sqlite 'select 1'")
    assert out.startswith("refused") and "-readonly" in out


def test_piped_sqlite3_with_readonly_allowed(tmp_path):
    if not shutil.which("sqlite3"):
        pytest.skip("sqlite3 CLI not installed")
    import sqlite3
    db = tmp_path / "db.sqlite"
    sqlite3.connect(str(db)).close()      # a real, empty db so -readonly can open it
    t = make_shell_tool(str(tmp_path), mode="readonly")
    out = _call(t, command=f"echo x | sqlite3 -readonly {db} 'select 1'")
    assert "refused" not in out


# --- timeout kills the whole pipeline --------------------------------------- #

def test_pipeline_timeout_message(repo, monkeypatch):
    monkeypatch.setattr(shell_tool, "TIMEOUT_S", 0.3)
    _script(repo, "sleeper", "sleep 5")
    t = make_shell_tool(str(repo), mode="readonly", exec_roots=(repo,))
    out = _call(t, command="printf x | ./scripts/sleeper")
    assert "timed out" in out


def test_description_states_the_chaining_rule(tmp_path):
    desc = make_shell_tool(str(tmp_path), mode="readonly")["schema"]["function"]["description"]
    # True behavior: pipes and && / ; chaining allowed; redirects/||/& rejected.
    assert "chained with `&&` or `;`" in desc
    assert "redirects" in desc and "rejected" in desc


def test_description_states_the_timeout(tmp_path):
    # The kill timeout is stated up front (default and per-agent override), so
    # the model can plan slow queries around it instead of discovering it by kill.
    desc = make_shell_tool(str(tmp_path), mode="readonly")["schema"]["function"]["description"]
    assert f"killed after {shell_tool.TIMEOUT_S}s" in desc
    desc180 = make_shell_tool(str(tmp_path), mode="readonly",
                              timeout_s=180)["schema"]["function"]["description"]
    assert "killed after 180s" in desc180


def test_timeout_s_clamped_and_defaulted(tmp_path):
    def _desc(**kw):
        return make_shell_tool(str(tmp_path), mode="readonly", **kw)["schema"]["function"]["description"]
    assert f"killed after {shell_tool.TIMEOUT_S}s" in _desc(timeout_s=None)   # None → default
    assert f"killed after {shell_tool.TIMEOUT_S}s" in _desc(timeout_s=0)      # 0 → default
    assert "killed after 10s" in _desc(timeout_s=3)                           # clamped to min
    assert "killed after 600s" in _desc(timeout_s=9999)                       # clamped to max


def test_configured_timeout_kills_a_slow_command_with_guidance(tmp_path, monkeypatch):
    # A command outrunning the per-agent timeout is killed with a message that
    # tells the model the failure is retryable (narrow the query), not fatal.
    monkeypatch.setattr(shell_tool, "TIMEOUT_MIN_S", 1)   # allow a fast test timeout
    t = make_shell_tool(str(tmp_path), mode="readonly", timeout_s=1,
                        allowed_commands=["sleep"])
    out = asyncio.run(t["fn"](command="sleep 5"))
    assert "timed out after 1s" in out and "narrow it" in out
