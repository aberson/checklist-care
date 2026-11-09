"""Typed queries on the ``tip`` table (plan §3.3, §3.5, §4.5, §6).

Reads take any connection. :func:`insert_tip`, :func:`update_tip`,
:func:`approve`, :func:`leave_out` and :func:`delete_tip` write, and their
caller runs them inside ``connection.transaction``. The placement rules are the
caller's (``placement.py``); the schema triggers back them up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from checklist_care.limits import (
    BUCKET_LEFT_OUT,
    TIP_STATUS_APPROVED,
    TIP_STATUS_PENDING,
    TIP_STATUSES,
)
from checklist_care.store.lists import list_ids_by_tip, list_ids_of, remove_all_items

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

    found: set[int] = set()
    for chunk in _chunks(ids):
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


def _chunks(ids: Iterable[int]) -> Iterator[list[int]]:
    """The distinct ``ids``, ascending, in slices of at most ``_IDS_PER_QUERY``."""

    wanted = sorted(set(ids))
    for start in range(0, len(wanted), _IDS_PER_QUERY):
        yield wanted[start : start + _IDS_PER_QUERY]


def _require_transaction(conn: sqlite3.Connection, what: str) -> None:
    if not conn.in_transaction:
        raise RuntimeError(f"{what} must run inside connection.transaction()")


# --- the API's view of a tip (plan §6) ---------------------------------------------------

_DETAIL_COLUMNS: Final = (
    "tip.id, tip.name, tip.action, tip.reason, tip.bucket, tip.audience, tip.status, "
    "tip.source_id, source.ref AS source_ref, source.kind AS source_kind, tip.start_s, "
    "tip.locator, tip.evidence_url, tip.duplicate_of, tip.possible_duplicate_of, tip.note, "
    "tip.created_at, tip.updated_at"
)
"""The :class:`TipDetail` columns, from ``tip`` joined with its ``source``."""

EDITABLE_COLUMNS: Final = (
    "name",
    "action",
    "reason",
    "bucket",
    "audience",
    "status",
    "duplicate_of",
    "possible_duplicate_of",
    "note",
)
"""The ``tip`` columns :func:`update_tip` may set (``PATCH /tips/{id}``, plan §6)."""


@dataclass(frozen=True)
class TipDetail:
    """Every stored field of a tip, its source's ref and kind, and its lists."""

    id: int
    name: str
    action: str
    reason: str | None
    bucket: int
    audience: str
    status: str
    source_id: int
    source_ref: str
    source_kind: str
    start_s: int | None
    locator: str | None
    evidence_url: str | None
    duplicate_of: int | None
    possible_duplicate_of: int | None
    note: str | None
    created_at: str
    updated_at: str
    list_ids: tuple[int, ...]


def _optional_int(value: int | None) -> int | None:
    return None if value is None else int(value)


def _detail_row(row: sqlite3.Row, list_ids: Iterable[int]) -> TipDetail:
    return TipDetail(
        id=int(row["id"]),
        name=str(row["name"]),
        action=str(row["action"]),
        reason=row["reason"],
        bucket=int(row["bucket"]),
        audience=str(row["audience"]),
        status=str(row["status"]),
        source_id=int(row["source_id"]),
        source_ref=str(row["source_ref"]),
        source_kind=str(row["source_kind"]),
        start_s=_optional_int(row["start_s"]),
        locator=row["locator"],
        evidence_url=row["evidence_url"],
        duplicate_of=_optional_int(row["duplicate_of"]),
        possible_duplicate_of=_optional_int(row["possible_duplicate_of"]),
        note=row["note"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        list_ids=tuple(list_ids),
    )


def find_tips(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    bucket: int | None = None,
    audience: str | None = None,
    source_id: int | None = None,
) -> list[TipDetail]:
    """The tips that match every given filter (None matches all), by id, with their lists."""

    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ("tip.status", status),
        ("tip.bucket", bucket),
        ("tip.audience", audience),
        ("tip.source_id", source_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    placed = list_ids_by_tip(conn)
    rows = conn.execute(
        f"SELECT {_DETAIL_COLUMNS} FROM tip JOIN source ON source.id = tip.source_id"
        f"{where} ORDER BY tip.id",
        params,
    )
    return [_detail_row(row, placed.get(int(row["id"]), ())) for row in rows]


def get_tip(conn: sqlite3.Connection, tip_id: int) -> TipDetail | None:
    """The tip ``tip_id`` with its lists, or None."""

    row = conn.execute(
        f"SELECT {_DETAIL_COLUMNS} FROM tip JOIN source ON source.id = tip.source_id "
        "WHERE tip.id = ?",
        (tip_id,),
    ).fetchone()
    return None if row is None else _detail_row(row, list_ids_of(conn, tip_id))


@dataclass(frozen=True)
class TipBrief:
    """What a duplicate badge shows of the tip it points at."""

    id: int
    name: str
    action: str
    bucket: int
    status: str
    source_id: int


def briefs(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, TipBrief]:
    """The stored tips among ``ids``, by id."""

    found: dict[int, TipBrief] = {}
    for chunk in _chunks(ids):
        marks = ", ".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, name, action, bucket, status, source_id FROM tip WHERE id IN ({marks})",
            chunk,
        )
        for row in rows:
            found[int(row["id"])] = TipBrief(
                id=int(row["id"]),
                name=str(row["name"]),
                action=str(row["action"]),
                bucket=int(row["bucket"]),
                status=str(row["status"]),
                source_id=int(row["source_id"]),
            )
    return found


def counts_by_source(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """``{source_id: {status: count}}`` for every source that has tips; each status of
    ``TIP_STATUSES`` is present."""

    counts: dict[int, dict[str, int]] = {}
    rows = conn.execute(
        "SELECT source_id, status, count(*) AS n FROM tip GROUP BY source_id, status"
    )
    for row in rows:
        per_status = counts.setdefault(int(row["source_id"]), dict.fromkeys(TIP_STATUSES, 0))
        per_status[str(row["status"])] = int(row["n"])
    return counts


def update_tip(
    conn: sqlite3.Connection, tip_id: int, changes: Mapping[str, object], *, now: str
) -> None:
    """Set the ``changes`` columns (each one of :data:`EDITABLE_COLUMNS`) and
    ``updated_at`` of tip ``tip_id``. The caller holds the transaction and has
    removed any placement the change leaves invalid."""

    _require_transaction(conn, "update_tip")
    unknown = sorted(set(changes) - set(EDITABLE_COLUMNS))
    if unknown:
        raise ValueError(f"not editable tip columns: {', '.join(unknown)}")
    columns = [column for column in EDITABLE_COLUMNS if column in changes]
    assignments = ", ".join(f"{column} = ?" for column in [*columns, "updated_at"])
    conn.execute(
        f"UPDATE tip SET {assignments} WHERE id = ?",
        [*(changes[column] for column in columns), now, tip_id],
    )


def approve(conn: sqlite3.Connection, ids: Iterable[int], *, now: str) -> int:
    """Approve the pending tips among ``ids``; returns how many changed. The caller
    holds the transaction."""

    _require_transaction(conn, "approve")
    changed = 0
    for chunk in _chunks(ids):
        marks = ", ".join("?" * len(chunk))
        cursor = conn.execute(
            f"UPDATE tip SET status = ?, updated_at = ? WHERE status = ? AND id IN ({marks})",
            [TIP_STATUS_APPROVED, now, TIP_STATUS_PENDING, *chunk],
        )
        changed += cursor.rowcount
    return changed


def leave_out(conn: sqlite3.Connection, ids: Iterable[int], *, now: str) -> int:
    """Mark ``ids`` "Left out" (plan §3.5): take each off every list, then put it in
    bucket 4 and approve it. Nothing is deleted, so a rejected idea still catches
    its own duplicates. Returns how many tips changed. The caller holds the
    transaction."""

    _require_transaction(conn, "leave_out")
    changed = 0
    for chunk in _chunks(ids):
        remove_all_items(conn, chunk)
        marks = ", ".join("?" * len(chunk))
        cursor = conn.execute(
            "UPDATE tip SET bucket = ?, status = ?, updated_at = ? "
            f"WHERE (bucket <> ? OR status <> ?) AND id IN ({marks})",
            [
                BUCKET_LEFT_OUT,
                TIP_STATUS_APPROVED,
                now,
                BUCKET_LEFT_OUT,
                TIP_STATUS_APPROVED,
                *chunk,
            ],
        )
        changed += cursor.rowcount
    return changed


def deleted_original_note(tip_id: int, name: str) -> str:
    """What a duplicate's ``note`` gains when its original is deleted (plan §3.3)."""

    return f'(original #{tip_id} "{name}" deleted)'


def delete_tip(conn: sqlite3.Connection, tip_id: int) -> bool:
    """Delete tip ``tip_id``, the only hard delete; False when it does not exist.

    First, in the same transaction, each tip that duplicates it gets
    :func:`deleted_original_note` appended to its ``note``; ``ON DELETE SET NULL``
    then clears their ``duplicate_of``. The caller holds the transaction.
    """

    _require_transaction(conn, "delete_tip")
    row = conn.execute("SELECT name FROM tip WHERE id = ?", (tip_id,)).fetchone()
    if row is None:
        return False
    marker = deleted_original_note(tip_id, str(row["name"]))
    conn.execute(
        "UPDATE tip SET note = CASE WHEN note IS NULL OR note = '' THEN ? "
        "ELSE note || ' ' || ? END WHERE duplicate_of = ?",
        (marker, marker, tip_id),
    )
    conn.execute("DELETE FROM tip WHERE id = ?", (tip_id,))
    return True
