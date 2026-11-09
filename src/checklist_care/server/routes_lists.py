"""``GET /api/lists`` and ``GET /api/lists/{id}/items`` (plan §6): the lists, read only.

* ``/lists``: every list, in display order. Each carries its display ``title``
  (``Coffee Machine — Adults``, built by ``store/lists.py``), ``item_count`` (the
  tips placed on it, pending or approved) and ``over_capacity``: whether its print
  sheet, which shows only the approved tips, would hold more than
  ``PRINT_CAPACITY`` rows and spill onto a second page (``printing.over_capacity``).
* ``/lists/{id}/items``: the tips on one list in position order, pending or
  approved, as ``[{tip_id, position, name, action, status, bucket, audience}]``;
  an unknown list is a 404.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from checklist_care import printing
from checklist_care.db import connection
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.server.models import ListId, no_such_list
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
        "over_capacity": printing.over_capacity(counts.approved),
    }


def item_json(item: list_store.ListItemRow) -> dict[str, Any]:
    """A list item as ``GET /lists/{id}/items`` returns it."""

    return {
        "tip_id": item.tip_id,
        "position": item.position,
        "name": item.name,
        "action": item.action,
        "status": item.status,
        "bucket": item.bucket,
        "audience": item.audience,
    }


@router.get("")
def all_lists(database: DatabaseDep) -> list[dict[str, Any]]:
    """Every list, by ``sort_order`` then id."""

    with database.session() as conn, connection.snapshot(conn):
        rows = list_store.all_lists(conn)
        counts = list_store.counts_by_list(conn)
    return [list_json(row, counts.get(row.id, _EMPTY)) for row in rows]


@router.get("/{list_id}/items")
def items_of_list(list_id: ListId, database: DatabaseDep) -> list[dict[str, Any]]:
    """The tips on list ``list_id``, in position order."""

    with database.session() as conn, connection.snapshot(conn):
        if list_store.get_list(conn, list_id) is None:
            raise no_such_list(list_id)
        items = list_store.list_items(conn, list_id)
    return [item_json(item) for item in items]
