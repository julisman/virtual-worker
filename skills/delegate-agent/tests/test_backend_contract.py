from __future__ import annotations

from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from backends import BACKENDS  # noqa: E402


def test_registered_backends_expose_contract() -> None:
    methods = {"probe", "resolve", "build_launch", "build_resume", "normalize_event", "extract_session_id", "classify_exit"}
    assert {"codex", "claude"} <= set(BACKENDS)
    for backend in BACKENDS.values():
        assert all(callable(getattr(backend, method, None)) for method in methods)
