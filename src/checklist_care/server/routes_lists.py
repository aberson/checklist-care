"""The ``/api/lists`` routes (plan §6): the checklists, their cues and their tips.

* ``GET /lists``: every list, in display order (``sort_order``, then id). Each carries
  its display ``title`` (``Coffee Machine — Adults``, built by ``store/lists.py``),
  ``item_count`` (the tips placed on it, pending or approved) and ``over_capacity``:
  whether its print sheet, which shows only the approved tips, would hold more than
  ``PRINT_CAPACITY`` rows and spill onto a second page (``printing.over_capacity``).
* ``GET /lists/{id}/items``: the tips on one list in position order, pending or
  approved, as ``[{tip_id, position, name, action, status, bucket, audience}]``.
* ``POST /lists``: ``{place, audience, cue?}`` adds a list after every other one; the
  reply is 201 with the list. The cue is what the skill reads (through
  ``checklist-care context``) to place daily tips on its next ingest.
* ``PATCH /lists/{id}``: ``{place?, audience?, cue?, sort_order?}``, at least one, none
  null; ``{list, removed_tip_ids}``. A new audience takes every tip it refuses
  (``placement.compatible``: a kids list holds only kids+ tips) off the list in the
  same transaction, before the change, and reports them in position order;
  ``sort_order`` moves the list on the Lists page.
* ``DELETE /lists/{id}``: 204. Its tips stay stored; one that was on no other list
  shows under "Not on any list yet" again (``GET /tips?unplaced=true``).
* ``PUT /lists/{id}/items``: ``{tip_ids: [ordered]}`` makes exactly those tips, in that
  order, the list's items (positions 1, 2, …): one reorder, add or remove. A tip the
  list may not hold (placement rules) is a 409, a missing tip a 404 and a repeated one
  a 400, and each changes nothing. The reply is the stored items.

A place has one list per audience: a ``POST`` or ``PATCH`` that would repeat one is a
400 naming the fields. An unknown list is a 404. Every write is one transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Response

from checklist_care import placement, printing
from checklist_care.db import connection
from checklist_care.errors import ApiConflictError, ApiNotFoundError, ApiValidationError
from checklist_care.limits import AUDIENCE_KIDS, AUDIENCE_KIDS_PLUS, BUCKET_DAILY
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.server.models import (
    LIST_FIELDS,
    ListCreate,
    ListId,
    ListPatch,
    TipIds,
    no_such_list,
)
from checklist_care.store import lists as list_store
from checklist_care.store import tips as tip_store

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


def _stored_list_json(conn: sqlite3.Connection, row: list_store.ListRow) -> dict[str, Any]:
    return list_json(row, list_store.counts_by_list(conn).get(row.id, _EMPTY))


def _existing_list(conn: sqlite3.Connection, list_id: int) -> list_store.ListRow:
    row = list_store.get_list(conn, list_id)
    if row is None:
        raise no_such_list(list_id)
    return row


def _require_free(
    conn: sqlite3.Connection,
    *,
    place: str,
    audience: str,
    fields: Sequence[str],
    list_id: int | None = None,
) -> None:
    """400 when a list other than ``list_id`` already has this place and audience."""

    other = list_store.find_list(conn, place, audience)
    if other is not None and other.id != list_id:
        raise ApiValidationError(
            f"there is already a list {other.title} (#{other.id}); a place has one list per "
            "audience",
            fields,
        )


def _item_problem(tip: tip_store.TipDetail, row: list_store.ListRow) -> str:
    """Why ``tip`` may not go on list ``row`` (which ``placement.compatible`` refused)."""

    if tip.bucket != BUCKET_DAILY:
        return (
            f'tip #{tip.id} "{tip.name}" is in bucket {tip.bucket}; only bucket-{BUCKET_DAILY} '
            "(daily) tips go on lists"
        )
    return (
        f'tip #{tip.id} "{tip.name}" is {tip.audience}; {row.title} is a {AUDIENCE_KIDS} list '
        f"and accepts only {AUDIENCE_KIDS_PLUS} tips"
    )


@router.get("")
def all_lists(database: DatabaseDep) -> list[dict[str, Any]]:
    """Every list, by ``sort_order`` then id."""

    with database.session() as conn, connection.snapshot(conn):
        rows = list_store.all_lists(conn)
        counts = list_store.counts_by_list(conn)
    return [list_json(row, counts.get(row.id, _EMPTY)) for row in rows]


@router.post("", status_code=201)
def create_list(body: ListCreate, database: DatabaseDep) -> dict[str, Any]:
    """Add a list after every other one (module docstring)."""

    with database.session() as conn, connection.transaction(conn):
        _require_free(conn, place=body.place, audience=body.audience, fields=["place", "audience"])
        list_id = list_store.insert_list(
            conn,
            place=body.place,
            audience=body.audience,
            cue=body.cue,
            sort_order=list_store.next_sort_order(conn),
        )
        return _stored_list_json(conn, _existing_list(conn, list_id))


@router.patch("/{list_id}")
def patch_list(list_id: ListId, body: ListPatch, database: DatabaseDep) -> dict[str, Any]:
    """Edit a list: ``{list, removed_tip_ids}`` (module docstring)."""

    given = body.given()
    if not given:
        raise ApiValidationError(
            "give at least one of place, audience, cue and sort_order", list(LIST_FIELDS)
        )
    nulls = body.null_problems()
    if nulls:
        raise ApiValidationError(f"{', '.join(nulls)} may not be null", nulls)
    with database.session() as conn, connection.transaction(conn):
        current = _existing_list(conn, list_id)
        place = current.place if body.place is None else body.place
        audience = current.audience if body.audience is None else body.audience
        if (place, audience) != (current.place, current.audience):
            fields = [name for name in ("place", "audience") if name in given]
            _require_free(conn, place=place, audience=audience, fields=fields, list_id=list_id)
        removed: list[int] = []
        if audience != current.audience:
            items = list_store.list_items(conn, list_id)
            kept = [
                item.tip_id
                for item in items
                if placement.compatible(
                    bucket=item.bucket, audience=item.audience, list_audience=audience
                )
            ]
            removed = [item.tip_id for item in items if item.tip_id not in kept]
            if removed:
                # Items first: the schema trigger refuses an audience change that would
                # leave a refused tip behind.
                list_store.set_items(conn, list_id, kept)
        updated = list_store.update_list(
            conn,
            list_id,
            place=place,
            audience=audience,
            cue=current.cue if body.cue is None else body.cue,
            sort_order=current.sort_order if body.sort_order is None else body.sort_order,
        )
        if updated is None:  # pragma: no cover - the list was read in this transaction
            raise no_such_list(list_id)
        return {"list": _stored_list_json(conn, updated), "removed_tip_ids": removed}


@router.delete("/{list_id}", status_code=204)
def delete_list(list_id: ListId, database: DatabaseDep) -> Response:
    """Delete a list and its placements; its tips stay stored."""

    with database.session() as conn, connection.transaction(conn):
        if not list_store.delete_list(conn, list_id):
            raise no_such_list(list_id)
    return Response(status_code=204)


@router.get("/{list_id}/items")
def items_of_list(list_id: ListId, database: DatabaseDep) -> list[dict[str, Any]]:
    """The tips on list ``list_id``, in position order."""

    with database.session() as conn, connection.snapshot(conn):
        _existing_list(conn, list_id)
        items = list_store.list_items(conn, list_id)
    return [item_json(item) for item in items]


@router.put("/{list_id}/items")
def put_items(list_id: ListId, body: TipIds, database: DatabaseDep) -> list[dict[str, Any]]:
    """Store ``tip_ids``, in order, as the list's items (module docstring)."""

    requested = body.tip_ids
    if len(set(requested)) != len(requested):
        raise ApiValidationError("tip_ids names a tip twice; nothing was changed", ["tip_ids"])
    with database.session() as conn, connection.transaction(conn):
        row = _existing_list(conn, list_id)
        missing = sorted(set(requested) - tip_store.existing_ids(conn, requested))
        if missing:
            shown = ", ".join(f"#{tip_id}" for tip_id in missing)
            raise ApiNotFoundError(f"there is no tip {shown}; nothing was changed", ["tip_ids"])
        problems: list[str] = []
        for tip_id in requested:
            tip = tip_store.get_tip(conn, tip_id)
            if tip is None:  # pragma: no cover - existence was checked in this transaction
                raise ApiNotFoundError(f"there is no tip #{tip_id}", ["tip_ids"])
            if not placement.compatible(
                bucket=tip.bucket, audience=tip.audience, list_audience=row.audience
            ):
                problems.append(_item_problem(tip, row))
        if problems:
            raise ApiConflictError(f"{'; '.join(problems)}; nothing was changed", ["tip_ids"])
        list_store.set_items(conn, list_id, requested)
        items = list_store.list_items(conn, list_id)
    return [item_json(item) for item in items]
