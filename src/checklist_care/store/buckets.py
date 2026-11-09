"""Typed queries on the ``bucket`` table (plan §3.3, §3.4).

The four buckets are a fixed set; their titles and rubrics are editable, and the
skill reads the current ones through ``checklist-care context``.
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
