"""SQLite connection helper (plan §2, §3.3, §3.6).

Every connection opened through :func:`connect` applies the four pragmas
(WAL, ``synchronous=NORMAL``, ``foreign_keys=ON``, ``busy_timeout=5000``),
uses ``sqlite3.Row`` and runs in autocommit mode (``isolation_level=None``),
so writes happen only inside an explicit :func:`transaction`
(``BEGIN IMMEDIATE``). Pragmas follow ``toybox/src/toybox/db/connection.py``.

On open, ``PRAGMA user_version`` 0 means "apply ``schema.sql`` and set it to
:data:`SCHEMA_VERSION`". Any other unknown version exits 2 with ``CC_E_CONFIG``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Final

from checklist_care.errors import ConfigError

SCHEMA_VERSION: Final = 1
SCHEMA_RESOURCE: Final = "schema.sql"

PRAGMAS: Final = (
    # busy_timeout first: switching to WAL takes a lock that a concurrent first
    # open must wait for, not fail on.
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)

TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
"""Stored timestamps: UTC ``YYYY-MM-DDTHH:MM:SSZ``, written by the app."""


def utc_now() -> datetime:
    """The current UTC time to the second. Tests replace this to control time."""

    return datetime.now(UTC).replace(microsecond=0)


def format_timestamp(moment: datetime) -> str:
    """``moment`` as a stored timestamp (``YYYY-MM-DDTHH:MM:SSZ``, UTC)."""

    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def schema_sql() -> str:
    """The text of the packaged ``schema.sql``."""

    resource = resources.files("checklist_care.db").joinpath(SCHEMA_RESOURCE)
    return resource.read_text(encoding="utf-8")


def schema_statements(script: str | None = None) -> list[str]:
    """Split a schema script into complete statements (triggers stay whole)."""

    text = schema_sql() if script is None else script
    statements: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    leftover = [
        line for line in buffer.splitlines() if line.strip() and not line.strip().startswith("--")
    ]
    if leftover:
        raise ValueError(f"schema ends with an incomplete statement: {leftover[0]!r}")
    return statements


def schema_version(conn: sqlite3.Connection) -> int:
    """The database's ``PRAGMA user_version``."""

    return int(conn.execute("PRAGMA user_version").fetchone()[0])


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One ``BEGIN IMMEDIATE`` transaction: commit on success, roll back on error."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One read transaction (``BEGIN``, deferred): every query inside it sees the
    same committed state, and no write lock is taken."""

    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


def connect(path: Path) -> sqlite3.Connection:
    """Open the database at ``path``, applying the pragmas and the schema.

    Creates the file if it is missing; callers that must not create a database
    check for it first. The parent directory must exist.
    """

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            conn.execute(pragma)
        _apply_schema(conn, path)
    except sqlite3.DatabaseError as error:
        conn.close()
        if error.sqlite_errorcode == sqlite3.SQLITE_NOTADB:
            raise ConfigError(f"{path} is not a SQLite database: {error}") from error
        raise
    except BaseException:
        conn.close()
        raise
    return conn


def _apply_schema(conn: sqlite3.Connection, path: Path) -> None:
    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        return
    _require_empty_version(version, path)
    with transaction(conn):
        # Re-read under the write lock: another process may have just created it.
        version = schema_version(conn)
        if version == SCHEMA_VERSION:
            return
        _require_empty_version(version, path)
        existing = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        if existing:
            raise ConfigError(
                f"database {path} has schema version 0 but already holds tables; "
                "it is not a checklist-care database"
            )
        for statement in schema_statements():
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _require_empty_version(version: int, path: Path) -> None:
    if version != 0:
        raise ConfigError(
            f"database {path} has schema version {version}; this checklist-care "
            f"knows only version {SCHEMA_VERSION}. Upgrade checklist-care."
        )
