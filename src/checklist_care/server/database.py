"""The server's database handle and its FastAPI dependency.

The server keeps one SQLite connection, opened at startup and closed on lifespan
shutdown (``server/app.py``). Route handlers run on a thread pool, so the handle
is opened with ``check_same_thread=False`` and :meth:`Database.session` lends it
to one request at a time under a lock: SQLite in WAL mode has one writer anyway,
and every transaction a route opens finishes inside its session.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from checklist_care.db import connection
from checklist_care.errors import ConfigError


class Database:
    """The one SQLite handle of a running server."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def open(self) -> None:
        """Open the handle (idempotent). A missing or empty file is a ``ConfigError``:
        the server never creates a database without its seed rows."""

        with self._lock:
            self._open_locked()

    def _open_locked(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self.path.is_file() or self.path.stat().st_size == 0:
                raise ConfigError(f"no database at {self.path}; run `checklist-care init` first")
            self._conn = connection.connect(self.path, check_same_thread=False)
        return self._conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """Lend the handle to one caller at a time, opening it if needed."""

        with self._lock:
            yield self._open_locked()

    def close(self) -> None:
        """Close the handle (idempotent)."""

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def get_database(request: Request) -> Database:
    """The running app's :class:`Database` (``app.state.database``)."""

    database: Database = request.app.state.database
    return database


DatabaseDep = Annotated[Database, Depends(get_database)]
"""Route parameter type that receives the app's :class:`Database`."""
