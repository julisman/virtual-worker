"""Process-local registry for live agent run steering messages."""

from __future__ import annotations

import threading


class Inbox:
    """Thread-safe pending message queue for one live run."""

    def __init__(self) -> None:
        self.pending: list[str] = []
        self._lock = threading.Lock()
        self.closed = False

    def drain(self) -> list[str]:
        with self._lock:
            messages = self.pending
            self.pending = []
            return messages

    def close_if_empty(self) -> tuple[list[str], bool]:
        """Atomically consume pending messages or close an empty inbox."""
        with self._lock:
            if self.pending:
                messages = self.pending
                self.pending = []
                return messages, False
            self.closed = True
            return [], True

    def _deliver(self, content: str) -> bool:
        with self._lock:
            if self.closed:
                return False
            self.pending.append(content)
            return True

    def _close(self) -> None:
        with self._lock:
            self.closed = True


_runs: dict[str, Inbox] = {}
_runs_lock = threading.Lock()


def register(run_id: str) -> Inbox:
    inbox = Inbox()
    with _runs_lock:
        previous = _runs.get(run_id)
        _runs[run_id] = inbox
    if previous is not None:
        previous._close()
    return inbox


def deliver(run_id: str, content: str) -> bool:
    with _runs_lock:
        inbox = _runs.get(run_id)
    if inbox is None:
        return False
    return inbox._deliver(content)


def unregister(run_id: str) -> None:
    """Remove and close a run inbox; safe to call repeatedly."""
    try:
        with _runs_lock:
            inbox = _runs.pop(run_id, None)
        if inbox is not None:
            inbox._close()
    except Exception:
        # Registry cleanup is best-effort and must never affect a run's result.
        return
