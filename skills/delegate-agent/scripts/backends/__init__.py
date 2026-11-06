from __future__ import annotations

from .base import Backend
from .claude import ClaudeBackend
from .codex import CodexBackend


BACKENDS: dict[str, Backend] = {
    "codex": CodexBackend(),
    "claude": ClaudeBackend(),
}


def get_backend(backend_id: str) -> Backend:
    try:
        return BACKENDS[backend_id]
    except KeyError as exc:
        raise ValueError(f"unknown backend: {backend_id}") from exc


def probe_backends(include_hidden: bool = False) -> list[dict]:
    return [backend.probe(include_hidden=include_hidden).as_dict() for backend in BACKENDS.values()]
