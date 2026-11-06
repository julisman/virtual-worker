"""Focus output guardrail — opt-in action-first style block in the prompt.

``guardrails.focusOutput`` appends a static style directive (adapted from
ayghri/i-have-adhd) above the clock line so the prompt prefix stays cache-safe.
The key has no DB column: it rides the guardrails JSON passthrough like
``compression`` and ``lazyTools``.
"""

import pytest

import config
from agent.compose import _FOCUS_STYLE, compose


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)


def _system(guardrails):
    system, *_ = compose({"persona": "You help.", "guardrails": guardrails},
                         session_id="chat:s1")
    return system


def test_focus_output_appends_style_block_above_clock(home):
    system = _system({"focusOutput": True})
    assert "Output style — action-first:" in system
    assert system.index(_FOCUS_STYLE) < system.index("Current date and time:")


def test_focus_output_off_by_default(home):
    assert _FOCUS_STYLE not in _system({})
    assert _FOCUS_STYLE not in _system({"focusOutput": False})
