"""Read-only shell tier additions (linked-wiki revamp §3.2): jq/column allowed,
sqlite3 only with -readonly."""

import shlex

from agent import shell_tool


def _classify(cmd, *, workspace=False):
    return shell_tool._classify(shlex.split(cmd), workspace=workspace)


def test_jq_and_column_allowed_readonly():
    assert _classify("jq . data.json")[0]
    assert _classify("column -t report.txt")[0]


def test_sqlite3_without_readonly_is_refused():
    allowed, reason = _classify("sqlite3 index.db 'select 1'")
    assert not allowed
    assert "-readonly" in reason


def test_sqlite3_with_readonly_is_allowed():
    assert _classify("sqlite3 -readonly index.db 'select 1'")[0]


def test_printf_nl_find_allowed_readonly():
    assert _classify("printf '%s' x")[0]
    assert _classify("nl notes.txt")[0]
    assert _classify("find . -name '*.md'")[0]


def test_find_delete_refused_readonly():
    allowed, reason = _classify("find . -delete")
    assert not allowed


def test_fallthrough_message_names_readonly_shell():
    allowed, reason = _classify("xxd data.bin")
    assert not allowed
    assert "read-only shell" in reason
    allowed2, reason2 = _classify("strings blob")
    assert not allowed2 and "read-only shell" in reason2
