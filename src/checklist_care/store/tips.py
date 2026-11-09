"""Typed queries on the ``tip`` table (plan §3.3, §3.5, §4.5).

Reads take any connection. :func:`insert_tip` writes, and its caller runs it
inside ``connection.transaction``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from checklist_care.limits import BUCKET_LEFT_OUT
from checklist_care.store.lists import list_ids_by_tip

_IDS_PER_QUERY: Final = 500
"""Bound parameters per ``IN (...)`` query, well under SQLite's variable limit."""

_COLUMNS: Final = (
    "tip.id, tip.name, tip.action, tip.bucket, tip.audience, tip.status, source.ref AS source_ref"
)
"""The :class:`TipRow` columns, from ``tip`` joined with its ``source``."""


@dataclass(frozen=True)
class NewTip:
    """A tip to insert; ``status``, ``source_id`` and the timestamps come from the caller."""

    name: str
    action: str
    reason: str | None
    bucket: int
    audience: str
    start_s: int | None = None
    locator: str | None = None
    evidence_url: str | None = None
    duplicate_of: int | None = None
    possible_duplicate_of: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class TipRow:
    """A stored tip as ``checklist-care context`` shows it to the skill (plan §4.5)."""

    id: int
    name: str
    action: str
    bucket: int
    audience: str
    status: str
    source_ref: str
    list_ids: tuple[int, ...]


def _row(row: sqlite3.Row, list_ids: Iterable[int]) -> TipRow:
    return TipRow(
        id=int(row["id"]),
        name=str(row["name"]),
        action=str(row["action"]),
        bucket=int(row["bucket"]),
        audience=str(row["audience"]),
        status=str(row["status"]),
        source_ref=str(row["source_ref"]),
        list_ids=tuple(list_ids),
    )


def insert_tip(
    conn: sqlite3.Connection, tip: NewTip, *, source_id: int, status: str, now: str
) -> int:
    """Insert ``tip`` and return its id. The caller holds the transaction."""

    if not conn.in_transaction:
        raise RuntimeError("insert_tip must run inside connection.transaction()")
    cursor = conn.execute(
        "INSERT INTO tip (name, action, reason, bucket, audience, status, source_id, start_s, "
        "locator, evidence_url, duplicate_of, possible_duplicate_of, note, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tip.name,
            tip.action,
            tip.reason,
            tip.bucket,
            tip.audience,
            status,
            source_id,
            tip.start_s,
            tip.locator,
            tip.evidence_url,
            tip.duplicate_of,
            tip.possible_duplicate_of,
            tip.note,
            now,
            now,
        ),
    )
    tip_id = cursor.lastrowid
    if tip_id is None:  # pragma: no cover - an INSERT always sets lastrowid
        raise RuntimeError("the tip insert returned no row id")
    return int(tip_id)


def existing_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> frozenset[int]:
    """The members of ``ids`` that are stored tip ids."""

    wanted = sorted(set(ids))
    found: set[int] = set()
    for start in range(0, len(wanted), _IDS_PER_QUERY):
        chunk = wanted[start : start + _IDS_PER_QUERY]
        marks = ", ".join("?" * len(chunk))
        rows = conn.execute(f"SELECT id FROM tip WHERE id IN ({marks})", chunk)
        found.update(int(row[0]) for row in rows)
    return frozenset(found)


def known_tips(conn: sqlite3.Connection) -> list[TipRow]:
    """The tips a new candidate is compared against (plan §3.5), by id.

    Every tip in buckets 1-3, pending or approved, and every bucket-4 tip whose
    ``duplicate_of`` is NULL (a rejected idea, which still catches its own
    duplicates). Each carries the ids of the lists it is on.
    """

    placed = list_ids_by_tip(conn)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM tip JOIN source ON source.id = tip.source_id "
        "WHERE tip.bucket <> ? OR tip.duplicate_of IS NULL ORDER BY tip.id",
        (BUCKET_LEFT_OUT,),
    )
    return [_row(row, placed.get(int(row["id"]), ())) for row in rows]
