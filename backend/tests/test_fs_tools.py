"""Filesystem toolkit — sandbox guard + read/write/edit/list/glob/grep."""

import asyncio

from agent import fs_tools


def _reg(tmp_path):
    return fs_tools.make_fs_tools(str(tmp_path))


def _call(reg, name, **kw):
    return asyncio.run(reg[name]["fn"](**kw))


def test_make_fs_tools_blank_root_is_empty():
    assert fs_tools.make_fs_tools("") == {}
    assert fs_tools.make_fs_tools("   ") == {}


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
