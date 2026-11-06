"""Filesystem toolkit — sandbox guard + read/write/edit/list/glob/grep."""

import asyncio

import pytest

from agent import fs_tools


def _reg(tmp_path):
    return fs_tools.make_fs_tools(str(tmp_path))


def _call(reg, name, **kw):
    return asyncio.run(reg[name]["fn"](**kw))


def test_make_fs_tools_blank_root_is_empty():
    assert fs_tools.make_fs_tools("") == {}
    assert fs_tools.make_fs_tools("   ") == {}


def test_make_fs_tools_rejects_file_working_folder(tmp_path):
    # A working folder pointed at an existing *file* must raise a clear FsError,
    # not a cryptic mkdir FileExistsError ([Errno 17]) that crashes agent compose.
    f = tmp_path / "team-roster.md"
    f.write_text("hi")
    with pytest.raises(fs_tools.FsError) as exc:
        fs_tools.make_fs_tools(str(f))
    assert "not a directory" in str(exc.value)


def test_compose_degrades_when_working_folder_is_a_file(tmp_path):
    # The real-world bug: an agent's working folder set to a file crashed both
    # chat (HTTP 500) and scheduled runs ([Errno 17]). Compose must degrade
    # gracefully — no file tools, a note explaining why, and no exception.
    from agent.compose import compose

    f = tmp_path / "team-roster.md"
    f.write_text("hi")
    system, registry, *_ = compose({"id": "grant", "workingDir": str(f)})
    assert not (set(registry) & set(fs_tools.FS_TOOL_NAMES))  # no file tools
    assert "unusable" in system.lower() and "disabled" in system.lower()


def test_compose_grants_file_tools_for_a_real_folder(tmp_path):
    from agent.compose import compose

    system, registry, *_ = compose({"id": "grant", "workingDir": str(tmp_path)})
    assert set(fs_tools.FS_TOOL_NAMES) <= set(registry)  # file tools present
    assert "file tools" in system.lower()


def test_registry_has_all_fs_tools(tmp_path):
    reg = _reg(tmp_path)
    assert set(reg.keys()) == set(fs_tools.FS_TOOL_NAMES)
    for t in reg.values():
        assert t["schema"]["function"]["name"]


def test_write_read_edit_roundtrip(tmp_path):
    reg = _reg(tmp_path)
    assert "wrote" in _call(reg, "write_file", path="notes/a.txt", content="hello world")
    assert _call(reg, "read_file", path="notes/a.txt") == "hello world"
    out = _call(reg, "edit_file", path="notes/a.txt", old="world", new="there")
    assert "1 replacement" in out
    assert _call(reg, "read_file", path="notes/a.txt") == "hello there"


def test_edit_requires_unique_match(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="x.txt", content="aa aa")
    assert "not unique" in _call(reg, "edit_file", path="x.txt", old="aa", new="b")
    assert "not found" in _call(reg, "edit_file", path="x.txt", old="zzz", new="b")


def test_list_dir_and_glob(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="src/main.py", content="print(1)")
    _call(reg, "write_file", path="src/util.py", content="x=2")
    _call(reg, "write_file", path="readme.md", content="# hi")
    listing = _call(reg, "list_dir", path=".")
    assert "src/" in listing and "readme.md" in listing
    hits = _call(reg, "glob", pattern="**/*.py")
    assert "src/main.py" in hits and "src/util.py" in hits and "readme.md" not in hits


def test_grep_matches_with_line_numbers(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.py", content="alpha\nBETA target\ngamma")
    out = _call(reg, "grep", pattern="target")
    assert "a.py:2:" in out and "BETA target" in out
    assert "no matches" in _call(reg, "grep", pattern="nope")


def test_grep_respects_glob_filter(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.py", content="needle")
    _call(reg, "write_file", path="b.txt", content="needle")
    out = _call(reg, "grep", pattern="needle", glob="*.py")
    assert "a.py" in out and "b.txt" not in out


def test_path_traversal_is_blocked(tmp_path):
    reg = _reg(tmp_path)
    assert "escapes" in _call(reg, "read_file", path="../../etc/passwd")
    assert "escapes" in _call(reg, "write_file", path="../outside.txt", content="x")
    # An absolute path outside the root is also refused.
    assert "escapes" in _call(reg, "read_file", path="/etc/hosts")


def test_read_missing_file(tmp_path):
    reg = _reg(tmp_path)
    assert "not found" in _call(reg, "read_file", path="ghost.txt")


# --- readonly mode (linked repos) ------------------------------------------- #

def test_readonly_registry_omits_write_tools(tmp_path):
    reg = fs_tools.make_fs_tools(str(tmp_path), readonly=True)
    assert set(reg) == set(fs_tools.FS_READONLY_TOOL_NAMES)
    assert "write_file" not in reg and "edit_file" not in reg


def test_readonly_missing_root_raises_and_does_not_create(tmp_path):
    ghost = tmp_path / "ghost"
    with pytest.raises(fs_tools.FsError) as exc:
        fs_tools.make_fs_tools(str(ghost), readonly=True)
    assert "does not exist" in str(exc.value)
    assert not ghost.exists()   # readonly mode must never mkdir the root


# --- rg-backed grep + graceful fallback ------------------------------------- #

def test_grep_python_fallback_when_no_rg(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_tools, "_rg_binary", lambda: None)
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.py", content="alpha\nBETA target\ngamma")
    out = _call(reg, "grep", pattern="target")
    assert "a.py:2:" in out and "BETA target" in out


def test_grep_uses_rg_output(tmp_path, monkeypatch):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.py", content="ignored by the fake")
    monkeypatch.setattr(fs_tools, "_rg_binary", lambda: "/fake/rg")

    class _Fake:
        returncode = 0
        stdout = b"./a.py:7:matched line here\n"

    monkeypatch.setattr(fs_tools.subprocess, "run", lambda *a, **k: _Fake())
    out = _call(reg, "grep", pattern="matched")
    assert "a.py:7: matched line here" in out


def test_grep_falls_back_on_rg_error_exit(tmp_path, monkeypatch):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.py", content="needle here")
    monkeypatch.setattr(fs_tools, "_rg_binary", lambda: "/fake/rg")

    class _Err:
        returncode = 2          # rg error → caller must fall back to Python scan
        stdout = b""

    monkeypatch.setattr(fs_tools.subprocess, "run", lambda *a, **k: _Err())
    out = _call(reg, "grep", pattern="needle")
    assert "a.py:1:" in out and "needle" in out


# --- read_file: ranged reads ------------------------------------------------ #

def test_ranged_read_is_numbered_and_inclusive(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.txt", content="l1\nl2\nl3\nl4\nl5")
    out = _call(reg, "read_file", path="a.txt", start_line=2, end_line=4)
    assert "== a.txt:2-4 ==" in out
    assert "2\tl2" in out and "3\tl3" in out and "4\tl4" in out
    assert "1\tl1" not in out and "5\tl5" not in out


def test_ranged_read_clamps_and_reports_beyond_eof(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.txt", content="l1\nl2\nl3\nl4\nl5")
    # start < 1 clamps to 1; end past EOF clamps to the last line.
    out = _call(reg, "read_file", path="a.txt", start_line=-3, end_line=999)
    assert "== a.txt:1-5 ==" in out and "1\tl1" in out and "5\tl5" in out
    # start past EOF is a clear message, not an empty block.
    beyond = _call(reg, "read_file", path="a.txt", start_line=99)
    assert "file has 5 lines" in beyond


def test_ranged_read_start_to_eof_when_end_omitted(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.txt", content="l1\nl2\nl3")
    out = _call(reg, "read_file", path="a.txt", start_line=2)
    assert "== a.txt:2-3 ==" in out and "2\tl2" in out and "3\tl3" in out
    assert "1\tl1" not in out


def test_plain_read_is_raw_no_headers_or_numbers(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.txt", content="hello\nworld")
    out = _call(reg, "read_file", path="a.txt")
    assert out == "hello\nworld"          # verbatim — edit_file's copy contract
    assert "==" not in out and "\t" not in out


# --- read_file: batch mode -------------------------------------------------- #

def test_batch_read_refs_spaces_line_and_fallback(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="my file.md", content="one\ntwo")
    _call(reg, "write_file", path="a.txt", content="x1\nx2\nx3")
    _call(reg, "write_file", path="notes:v2.md", content="kept")
    out = _call(reg, "read_file", files=["my file.md", "a.txt:2", "notes:v2.md"])
    # filename with a space, read whole
    assert "== my file.md ==" in out and "1\tone" in out
    # single-line ref path:LINE
    assert "== a.txt:2-2 ==" in out and "2\tx2" in out and "1\tx1" not in out
    # rightmost-colon suffix isn't digits -> whole string is the path
    assert "== notes:v2.md ==" in out and "1\tkept" in out


def test_batch_read_per_ref_error_isolation(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="good.txt", content="alive")
    out = _call(reg, "read_file", files=["ghost.txt", "good.txt"])
    assert "not found: ghost.txt" in out          # bad ref inline
    assert "== good.txt ==" in out and "1\talive" in out   # good ref still returned


def test_batch_read_refuses_more_than_8_refs(tmp_path):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.txt", content="x")
    out = _call(reg, "read_file", files=[f"a.txt"] * 9)
    assert "8" in out and ("too many" in out.lower())


def test_batch_read_shares_budget_and_truncates(tmp_path):
    reg = _reg(tmp_path)
    big = "\n".join(f"line-{i}" for i in range(500))
    _call(reg, "write_file", path="big.txt", content=big)
    out = _call(reg, "read_file", files=["big.txt"], max_bytes=60)
    assert "truncated" in out and "budget" in out
    assert len(out) < 400          # the small budget actually bounded the output


# --- grep: multi-pattern ---------------------------------------------------- #

def test_grep_multi_pattern_python_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_tools, "_rg_binary", lambda: None)
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.py", content="alpha here\nbeta here\ngamma here")
    out = _call(reg, "grep", patterns=["alpha", "gamma"])
    assert "a.py:1:" in out and "a.py:3:" in out and "a.py:2:" not in out
    # `pattern` and `patterns` are unioned
    out2 = _call(reg, "grep", pattern="beta", patterns=["alpha"])
    assert "a.py:1:" in out2 and "a.py:2:" in out2


def test_grep_multi_pattern_rg_args(tmp_path, monkeypatch):
    reg = _reg(tmp_path)
    _call(reg, "write_file", path="a.py", content="ignored by the fake")
    monkeypatch.setattr(fs_tools, "_rg_binary", lambda: "/fake/rg")
    seen = {}

    class _Fake:
        returncode = 0
        stdout = b"./a.py:1:hit\n"

    def _run(args, *a, **k):
        seen["args"] = args
        return _Fake()

    monkeypatch.setattr(fs_tools.subprocess, "run", _run)
    _call(reg, "grep", patterns=["foo", "bar"])
    args = seen["args"]
    assert "-e" in args and "foo" in args and "bar" in args
    i = args.index("--max-count")
    assert args[i + 1] == "200"
