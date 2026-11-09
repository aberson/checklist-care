"""Typed queries on the ``list`` and ``list_item`` tables (plan §3.3, §4.1).

Reads take any connection. :func:`append_item` writes, and its caller runs it
inside ``connection.transaction`` after checking the placement with
``placement.py``; the schema triggers abort any placement that slips through.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from checklist_care.limits import LIST_TITLE_SEPARATOR

_COLUMNS: Final = "id, place, audience, cue, sort_order"


@dataclass(frozen=True)
class ListRow:
    """A stored checklist: where it hangs, who it is for and its cue."""

    id: int
    place: str
    audience: str
    """One of ``limits.AUDIENCES_LIST``."""
    cue: str
    sort_order: int

    @property
    def title(self) -> str:
        """The display title, such as ``Coffee Machine — Adults``."""

        return f"{self.place}{LIST_TITLE_SEPARATOR}{self.audience.capitalize()}"


def _row(row: sqlite3.Row) -> ListRow:
    return ListRow(
        id=int(row["id"]),
        place=str(row["place"]),
        audience=str(row["audience"]),
        cue=str(row["cue"]),
        sort_order=int(row["sort_order"]),
    )


def all_lists(conn: sqlite3.Connection) -> list[ListRow]:
    """Every list, in display order (``sort_order``, then id)."""

    rows = conn.execute(f"SELECT {_COLUMNS} FROM list ORDER BY sort_order, id")
    return [_row(row) for row in rows]


def lists_by_id(conn: sqlite3.Connection) -> dict[int, ListRow]:
    """Every list, keyed by id."""

    return {row.id: row for row in all_lists(conn)}


def list_ids_by_tip(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """The ids of the lists each placed tip is on, ascending. Unplaced tips are absent."""

    placed: dict[int, list[int]] = {}
    for row in conn.execute("SELECT tip_id, list_id FROM list_item ORDER BY tip_id, list_id"):
        placed.setdefault(int(row["tip_id"]), []).append(int(row["list_id"]))
    return placed


def append_item(conn: sqlite3.Connection, list_id: int, tip_id: int) -> int:
    """Put ``tip_id`` at the end of list ``list_id``; returns its position (1 = first).

    The caller holds the transaction and has checked the placement.
    """

    if not conn.in_transaction:
        raise RuntimeError("append_item must run inside connection.transaction()")
    row = conn.execute(
        "SELECT coalesce(max(position), 0) + 1 FROM list_item WHERE list_id = ?", (list_id,)
    ).fetchone()
    position = int(row[0])
    conn.execute(
        "INSERT INTO list_item (list_id, tip_id, position) VALUES (?, ?, ?)",
        (list_id, tip_id, position),
    )
    return position
