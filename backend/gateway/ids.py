"""Short opaque id generators for native response objects.

Each protocol expects vendor-shaped ids (``msg_…``, ``resp_…``, ``fc_…``,
``call_…``). They only need to be unique within a response, so a counter + a
process-random suffix is plenty (and avoids importing time/random at call sites
which some sandboxes restrict)."""

from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_counter = 0
# A per-process suffix so ids from different runs don't collide in logs.
_suffix = os.urandom(4).hex()


def _next(prefix: str) -> str:
    global _counter
    with _lock:
        _counter += 1
        n = _counter
    return f"{prefix}_{_suffix}{n:06d}"


def msg_id() -> str:
    return _next("msg")


def resp_id() -> str:
    return _next("resp")


def fc_id() -> str:
    return _next("fc")


def call_id() -> str:
    return _next("call")
