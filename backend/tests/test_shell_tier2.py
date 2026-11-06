"""Tier-2 (repo-tooling) shell: a repo's own executables run under the read-only
tier when addressed in PATH-form and resolving inside an exec_root. Deny-list and
_DANGER_FLAGS still win; bare names and outside-repo programs stay refused."""

import shlex

import pytest

from agent import shell_tool


def _classify(cmd, *, roots=(), cwd=None):
    return shell_tool._classify(shlex.split(cmd), workspace=False, exec_roots=tuple(roots), cwd=cwd)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    kb = r / "scripts" / "kb"
    kb.write_text("#!/bin/sh\necho hi\n")
    kb.chmod(0o755)
    return r


def test_relative_repo_script_allowed(repo):
    allowed, _ = _classify("./scripts/kb chain x", roots=(repo,), cwd=repo)
    assert allowed


def test_bare_name_refused(repo):
    allowed, _ = _classify("kb chain x", roots=(repo,), cwd=repo)
    assert not allowed


def test_python_c_still_refused(repo):
    allowed, _ = _classify("python3 -c print(1)", roots=(repo,), cwd=repo)
    assert not allowed


def test_in_repo_executable_named_rm_refused_deny_wins(repo):
    rm = repo / "scripts" / "rm"
    rm.write_text("#!/bin/sh\n")
    rm.chmod(0o755)
    allowed, reason = _classify("./scripts/rm -rf x", roots=(repo,), cwd=repo)
    assert not allowed and "deny-list" in reason


def test_danger_flag_refused(repo):
    allowed, _ = _classify("./scripts/kb -exec foo", roots=(repo,), cwd=repo)
    assert not allowed


def test_script_outside_exec_root_refused(tmp_path, repo):
    outside = tmp_path / "evil.sh"
    outside.write_text("#!/bin/sh\n")
    outside.chmod(0o755)
    allowed, _ = _classify(f"{outside} run", roots=(repo,), cwd=repo)
    assert not allowed


def test_symlink_inside_repo_to_outside_refused(tmp_path, repo):
    target = tmp_path / "outside.sh"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    link = repo / "scripts" / "linked"
    link.symlink_to(target)
    allowed, _ = _classify("./scripts/linked go", roots=(repo,), cwd=repo)
    assert not allowed


def test_no_exec_roots_refuses_repo_script(repo):
    # Tier-1 (readonly): no exec_roots ⇒ the repo script is not runnable.
    allowed, _ = _classify("./scripts/kb chain x", roots=(), cwd=repo)
    assert not allowed
