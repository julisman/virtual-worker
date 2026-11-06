"""Shared SQLite connection behavior for storage modules."""

from __future__ import annotations

import sqlite3
from typing import Any


class ClosingConnection(sqlite3.Connection):
    """SQLite connection that closes after transaction context exit.

    ``sqlite3.Connection`` uses its context manager for commit/rollback only;
    it does not close the underlying file descriptors. Storage call sites use
    ``with connection`` for transaction boundaries, so close in ``finally``
    while delegating transaction handling to the stdlib implementation.
    """

    def begin_initialization(self) -> None:
        self._initializing = True

    def mark_initialized(self) -> None:
        self._initializing = False

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().execute(*args, **kwargs)
        except BaseException:
            if getattr(self, "_initializing", False):
                self.close()
            raise

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executemany(*args, **kwargs)
        except BaseException:
            if getattr(self, "_initializing", False):
                self.close()
            raise

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executescript(*args, **kwargs)
        except BaseException:
            if getattr(self, "_initializing", False):
                self.close()
            raise


def connect(database: Any, **kwargs: Any) -> ClosingConnection:
    """Open a connection whose ``with`` block always closes it."""
    conn = sqlite3.connect(database, factory=ClosingConnection, **kwargs)
    conn.begin_initialization()
    return conn
