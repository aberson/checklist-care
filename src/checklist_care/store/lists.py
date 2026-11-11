"""Typed queries on the ``list`` and ``list_item`` tables (plan §3.3, §4.1).

Reads take any connection. The writes (:func:`insert_list`, :func:`update_list`,
:func:`delete_list`, :func:`set_items`, :func:`append_item`, :func:`remove_items`)
run inside the caller's ``connection.transaction``; the caller checks a placement
with ``placement.py`` first, and the schema triggers abort any placement that slips
through.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from checklist_care.limits import LIST_TITLE_SEPARATOR, TIP_STATUS_APPROVED

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


def get_list(conn: sqlite3.Connection, list_id: int) -> ListRow | None:
    """The list ``list_id``, or None."""

    row = conn.execute(f"SELECT {_COLUMNS} FROM list WHERE id = ?", (list_id,)).fetchone()
    return None if row is None else _row(row)


def insert_list(
    conn: sqlite3.Connection,
    *,
    place: str,
    audience: str,
    cue: str,
    sort_order: int,
    list_id: int | None = None,
) -> int:
    """Insert a list and return its id (``list_id`` when given, else the next free one).
    The caller holds the transaction and has checked ``audience`` against
    ``limits.AUDIENCES_LIST``; the schema refuses a repeated place and audience."""

    if not conn.in_transaction:
        raise RuntimeError("insert_list must run inside connection.transaction()")
    cursor = conn.execute(
        "INSERT INTO list (id, place, audience, cue, sort_order) VALUES (?, ?, ?, ?, ?)",
        (list_id, place, audience, cue, sort_order),
    )
    new_id = cursor.lastrowid
    if new_id is None:  # pragma: no cover - an INSERT always sets lastrowid
        raise RuntimeError("the list insert returned no row id")
    return int(new_id)


def find_list(conn: sqlite3.Connection, place: str, audience: str) -> ListRow | None:
    """The list of ``place`` and ``audience`` (they are unique together), or None."""

    row = conn.execute(
        f"SELECT {_COLUMNS} FROM list WHERE place = ? AND audience = ?", (place, audience)
    ).fetchone()
    return None if row is None else _row(row)


def next_sort_order(conn: sqlite3.Connection) -> int:
    """The ``sort_order`` that puts a new list after every stored one."""

    row = conn.execute("SELECT coalesce(max(sort_order), 0) + 1 FROM list").fetchone()
    return int(row[0])


def update_list(
    conn: sqlite3.Connection,
    list_id: int,
    *,
    place: str,
    audience: str,
    cue: str,
    sort_order: int,
) -> ListRow | None:
    """Store every column of list ``list_id``; returns the stored list, or None when
    there is no such list. The caller holds the transaction, has checked ``audience``
    and that no other list has this place and audience, and has taken any tip the new
    audience refuses off the list first (the schema trigger aborts otherwise)."""

    if not conn.in_transaction:
        raise RuntimeError("update_list must run inside connection.transaction()")
    conn.execute(
        "UPDATE list SET place = ?, audience = ?, cue = ?, sort_order = ? WHERE id = ?",
        (place, audience, cue, sort_order, list_id),
    )
    return get_list(conn, list_id)


def delete_list(conn: sqlite3.Connection, list_id: int) -> bool:
    """Delete list ``list_id`` and, by the schema's cascade, its items; the tips stay.
    False when there is no such list. The caller holds the transaction."""

    if not conn.in_transaction:
        raise RuntimeError("delete_list must run inside connection.transaction()")
    return conn.execute("DELETE FROM list WHERE id = ?", (list_id,)).rowcount > 0


@dataclass(frozen=True)
class ListItemRow:
    """One tip on a list, at its ``position`` (1 = first), pending or approved."""

    tip_id: int
    position: int
    name: str
    action: str
    status: str
    bucket: int
    audience: str


def list_items(conn: sqlite3.Connection, list_id: int) -> list[ListItemRow]:
    """The tips on list ``list_id`` in ``position`` order, whatever their status."""

    rows = conn.execute(
        "SELECT list_item.tip_id AS tip_id, list_item.position AS position, tip.name AS name, "
        "tip.action AS action, tip.status AS status, tip.bucket AS bucket, "
        "tip.audience AS audience FROM list_item JOIN tip ON tip.id = list_item.tip_id "
        "WHERE list_item.list_id = ? ORDER BY list_item.position, list_item.tip_id",
        (list_id,),
    )
    return [
        ListItemRow(
            tip_id=int(row["tip_id"]),
            position=int(row["position"]),
            name=str(row["name"]),
            action=str(row["action"]),
            status=str(row["status"]),
            bucket=int(row["bucket"]),
            audience=str(row["audience"]),
        )
        for row in rows
    ]


def list_ids_by_tip(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """The ids of the lists each placed tip is on, ascending. Unplaced tips are absent."""

    placed: dict[int, list[int]] = {}
    for row in conn.execute("SELECT tip_id, list_id FROM list_item ORDER BY tip_id, list_id"):
        placed.setdefault(int(row["tip_id"]), []).append(int(row["list_id"]))
    return placed


def list_ids_of(conn: sqlite3.Connection, tip_id: int) -> list[int]:
    """The ids of the lists ``tip_id`` is on, ascending."""

    rows = conn.execute(
        "SELECT list_id FROM list_item WHERE tip_id = ? ORDER BY list_id", (tip_id,)
    )
    return [int(row["list_id"]) for row in rows]


@dataclass(frozen=True)
class ListCounts:
    """How many tips a list holds (``items``) and how many of them print (``approved``)."""

    items: int
    approved: int


def counts_by_list(conn: sqlite3.Connection) -> dict[int, ListCounts]:
    """The item counts of every list that holds at least one tip, by list id.

    Only bucket-1 tips are ever placed (the schema triggers), so ``approved`` is the
    number of rows the list's print sheet shows.
    """

    rows = conn.execute(
        "SELECT list_item.list_id AS list_id, count(*) AS items, "
        "sum(tip.status = ?) AS approved FROM list_item "
        "JOIN tip ON tip.id = list_item.tip_id GROUP BY list_item.list_id",
        (TIP_STATUS_APPROVED,),
    )
    return {
        int(row["list_id"]): ListCounts(items=int(row["items"]), approved=int(row["approved"]))
        for row in rows
    }


def remove_items(conn: sqlite3.Connection, tip_id: int, list_ids: Iterable[int]) -> None:
    """Take ``tip_id`` off each of ``list_ids``. The caller holds the transaction."""

    if not conn.in_transaction:
        raise RuntimeError("remove_items must run inside connection.transaction()")
    conn.executemany(
        "DELETE FROM list_item WHERE list_id = ? AND tip_id = ?",
        [(list_id, tip_id) for list_id in list_ids],
    )


def remove_all_items(conn: sqlite3.Connection, tip_ids: Iterable[int]) -> None:
    """Take each of ``tip_ids`` off every list. The caller holds the transaction."""

    if not conn.in_transaction:
        raise RuntimeError("remove_all_items must run inside connection.transaction()")
    conn.executemany("DELETE FROM list_item WHERE tip_id = ?", [(tip_id,) for tip_id in tip_ids])


def set_items(conn: sqlite3.Connection, list_id: int, tip_ids: Sequence[int]) -> None:
    """Make ``tip_ids``, in this order, exactly the items of list ``list_id``, at
    positions 1, 2, … (one reorder, add or remove). The caller holds the transaction
    and has checked every placement."""

    if not conn.in_transaction:
        raise RuntimeError("set_items must run inside connection.transaction()")
    conn.execute("DELETE FROM list_item WHERE list_id = ?", (list_id,))
    conn.executemany(
        "INSERT INTO list_item (list_id, tip_id, position) VALUES (?, ?, ?)",
        [(list_id, tip_id, position) for position, tip_id in enumerate(tip_ids, start=1)],
    )


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
