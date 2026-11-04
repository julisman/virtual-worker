"""Single app settings file: ``<VW_HOME>/settings.json``.

One place for Virtual Worker's persisted preferences (active model today; more later).
Tiny JSON key/value store, read/written on demand (low frequency), thread-safe.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _path():
    config.ensure_dir(config.APP_HOME)
    p = config.APP_HOME / "settings.json"
    # One-time migration from the pre-rename filename, so an existing install
    # keeps its persisted settings (e.g. the active model).
    legacy = config.APP_HOME / "ai-studio.json"
    if not p.exists() and legacy.exists():
        try:
            legacy.rename(p)
        except OSError:
            pass
    return p


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    try:
        _path().write_text(json.dumps(data, indent=2))
    except OSError as exc:
        logger.warning("could not write settings.json: %s", exc)


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - small KV API
    """Set a key; passing ``None`` removes it."""
    with _lock:
        data = load()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        _save(data)
