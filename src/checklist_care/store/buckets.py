"""Typed queries on the ``bucket`` table (plan §3.3, §3.4).

The four buckets are a fixed set; their titles and rubrics are editable (the
Buckets page, ``PUT /api/buckets/{id}``), and the skill reads the current ones
through ``checklist-care context``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

_COLUMNS: Final = "id, title, rubric, updated_at"


@dataclass(frozen=True)
class BucketRow:
    """A stored bucket: its title and the rubric that defines it."""

    id: int
    title: str
    rubric: str
    updated_at: str


def _row(row: sqlite3.Row) -> BucketRow:
    return BucketRow(
        id=int(row["id"]),
        title=str(row["title"]),
        rubric=str(row["rubric"]),
        updated_at=str(row["updated_at"]),
    )


def all_buckets(conn: sqlite3.Connection) -> list[BucketRow]:
    """Every bucket, by id."""

    rows = conn.execute(f"SELECT {_COLUMNS} FROM bucket ORDER BY id")
    return [_row(row) for row in rows]


def get_bucket(conn: sqlite3.Connection, bucket_id: int) -> BucketRow | None:
    """The bucket ``bucket_id``, or None when there is none."""

    row = conn.execute(f"SELECT {_COLUMNS} FROM bucket WHERE id = ?", (bucket_id,)).fetchone()
    return None if row is None else _row(row)


def update_bucket(
    conn: sqlite3.Connection,
    bucket_id: int,
    *,
    title: str | None = None,
    rubric: str | None = None,
    now: str,
) -> BucketRow | None:
    """Set the title and/or rubric given (None keeps the stored one) and ``updated_at``
    to ``now``; returns the stored bucket, or None when there is no bucket ``bucket_id``.
    The caller holds the transaction."""

    conn.execute(
        "UPDATE bucket SET title = coalesce(?, title), rubric = coalesce(?, rubric), "
        "updated_at = ? WHERE id = ?",
        (title, rubric, now, bucket_id),
    )
    return get_bucket(conn, bucket_id)
