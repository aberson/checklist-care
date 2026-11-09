"""Typed queries on the ``source`` table (plan §3.3, §3.5, §4.4).

Reads take any connection. :func:`upsert_fetched` writes, and its caller runs it
inside ``connection.transaction`` together with the packet write, so the
"already reviewed" check and the upsert see the same row. :func:`mark_reviewed`
is the proposal import's write, inside that import's one transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from checklist_care.errors import AlreadyReviewedError

_COLUMNS: Final = "id, kind, ref, title, url, revision, mode, fetched_at, reviewed_at"


@dataclass(frozen=True)
class SourceRow:
    id: int
    kind: str
    ref: str
    title: str | None
    url: str | None
    revision: str | None
    mode: str
    fetched_at: str | None
    reviewed_at: str | None

    @property
    def reviewed(self) -> bool:
        return self.reviewed_at is not None


def _row(row: sqlite3.Row) -> SourceRow:
    return SourceRow(
        id=int(row["id"]),
        kind=str(row["kind"]),
        ref=str(row["ref"]),
        title=row["title"],
        url=row["url"],
        revision=row["revision"],
        mode=str(row["mode"]),
        fetched_at=row["fetched_at"],
        reviewed_at=row["reviewed_at"],
    )


def get_by_ref(conn: sqlite3.Connection, ref: str) -> SourceRow | None:
    """The source with this ref, or None."""

    row = conn.execute(f"SELECT {_COLUMNS} FROM source WHERE ref = ?", (ref,)).fetchone()
    return None if row is None else _row(row)


def refuse_reviewed(conn: sqlite3.Connection, ref: str) -> None:
    """Raise :class:`AlreadyReviewedError` (exit 3) when ``ref`` was already reviewed.

    Re-processing a reviewed source is a v1 non-goal (plan §3.5).
    """

    row = get_by_ref(conn, ref)
    if row is not None and row.reviewed:
        raise AlreadyReviewedError(
            f"{ref} was already reviewed at {row.reviewed_at}; re-processing a reviewed "
            "source is not supported, so nothing was fetched or written"
        )


def upsert_fetched(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref: str,
    title: str | None,
    url: str | None,
    revision: str | None,
    fetched_at: str,
) -> SourceRow:
    """Insert the source, or refresh an unreviewed one, with ``reviewed_at`` NULL.

    ``mode`` keeps its value (the column default on insert); ``propose`` sets it.
    A reviewed row is never touched: the caller refuses it first, and the
    ``WHERE`` guard makes the update a no-op if it was reviewed in between.
    The caller holds the transaction.
    """

    if not conn.in_transaction:
        raise RuntimeError("upsert_fetched must run inside connection.transaction()")
    conn.execute(
        "INSERT INTO source (kind, ref, title, url, revision, fetched_at, reviewed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT (ref) DO UPDATE SET kind = excluded.kind, title = excluded.title, "
        "url = excluded.url, revision = excluded.revision, fetched_at = excluded.fetched_at "
        "WHERE source.reviewed_at IS NULL",
        (kind, ref, title, url, revision, fetched_at),
    )
    row = get_by_ref(conn, ref)
    if row is None:  # pragma: no cover - the statement above inserts or keeps the row
        raise RuntimeError(f"source {ref} vanished during its upsert")
    return row


def mark_reviewed(conn: sqlite3.Connection, source_id: int, *, mode: str, reviewed_at: str) -> None:
    """Store the source as reviewed in ``mode`` ("stored as reviewed", plan §4.5).

    Only an unreviewed row changes; the caller holds the transaction and refused
    a reviewed source first, so finding none to update is a bug.
    """

    if not conn.in_transaction:
        raise RuntimeError("mark_reviewed must run inside connection.transaction()")
    cursor = conn.execute(
        "UPDATE source SET reviewed_at = ?, mode = ? WHERE id = ? AND reviewed_at IS NULL",
        (reviewed_at, mode, source_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"source {source_id} is missing or already reviewed")


def all_sources(conn: sqlite3.Connection) -> list[SourceRow]:
    """Every source, by id (the seeded manual source first)."""

    rows = conn.execute(f"SELECT {_COLUMNS} FROM source ORDER BY id")
    return [_row(row) for row in rows]
