"""``GET /api/lists`` (plan §6): every list, in display order.

Each list carries its display ``title`` (``Coffee Machine — Adults``, built by
``store/lists.py``), ``item_count`` (the tips placed on it, pending or approved)
and ``over_capacity``: whether its print sheet, which shows only the approved
tips, would hold more than ``PRINT_CAPACITY`` rows and spill onto a second page.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from checklist_care import limits
from checklist_care.db import connection
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.store import lists as list_store

router = APIRouter(prefix=f"{API_PREFIX}/lists")

_EMPTY: list_store.ListCounts = list_store.ListCounts(items=0, approved=0)


def list_json(row: list_store.ListRow, counts: list_store.ListCounts) -> dict[str, Any]:
    """A list as ``GET /lists`` returns it."""

    return {
        "id": row.id,
        "place": row.place,
        "audience": row.audience,
        "title": row.title,
        "cue": row.cue,
        "sort_order": row.sort_order,
        "item_count": counts.items,
        "over_capacity": counts.approved > limits.PRINT_CAPACITY,
    }


@router.get("")
def all_lists(database: DatabaseDep) -> list[dict[str, Any]]:
    """Every list, by ``sort_order`` then id."""

    with database.session() as conn, connection.snapshot(conn):
        rows = list_store.all_lists(conn)
        counts = list_store.counts_by_list(conn)
    return [list_json(row, counts.get(row.id, _EMPTY)) for row in rows]
