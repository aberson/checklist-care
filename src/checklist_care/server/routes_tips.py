"""``GET /api/tips``, ``PATCH /api/tips/{id}`` and ``DELETE /api/tips/{id}`` (plan §6).

``GET /tips`` takes the filters ``status``, ``bucket``, ``audience``, ``source_id``
and ``unplaced``, all applied together. ``unplaced=true`` keeps only the approved
daily (bucket-1) tips that are on no list, the Lists page's "Not on any list yet"
panel; ``unplaced=false`` or no ``unplaced`` adds no filter.

A tip in a response carries every stored field, its ``list_ids``, its source's ref,
a ``start_url`` (the YouTube link that starts at ``start_s``, else null) and, for
the duplicate badges, a short view of the tips it names in ``duplicate_of`` and
``possible_duplicate_of`` (``duplicate_of_tip``, ``possible_duplicate_of_tip``).

**PATCH semantics** (plan §3.3, §6), all in one transaction:

* A field left out keeps its value. Text fields follow ``proposal.text_problem``,
  and the printed line follows ``LINE_MAX`` (400). ``reason`` may be null only on a
  manual tip.
* An explicit ``list_ids`` replaces the tip's placements. A list that does not
  exist is a 400; a list the tip may not go on (after its new bucket and audience)
  is a 409 and changes nothing.
* Without ``list_ids``, a bucket or audience change removes the placements it
  leaves invalid (``placement.incompatible_list_ids``) before the update, and
  reports them in ``removed_placements``.
* An explicit non-null ``duplicate_of`` needs bucket 4 (400 otherwise). A bucket
  change away from 4 that does not name ``duplicate_of`` clears it and reports
  the old id in ``cleared_duplicate_of`` (otherwise null).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, Response

from checklist_care import placement, proposal
from checklist_care.db import connection
from checklist_care.errors import ApiConflictError, ApiNotFoundError, ApiValidationError
from checklist_care.limits import (
    AUDIENCES_TIP,
    BUCKET_IDS,
    BUCKET_LEFT_OUT,
    ID_MAX,
    SOURCE_KIND_MANUAL,
    TIP_STATUSES,
)
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.server.models import TipPatch
from checklist_care.sources import youtube
from checklist_care.store import lists as list_store
from checklist_care.store import tips as tip_store

router = APIRouter(prefix=f"{API_PREFIX}/tips")

TipId = Annotated[int, Path(ge=1, le=ID_MAX)]
"""A tip id in the path: a whole number from 1 to ``ID_MAX``."""

REFERENCE_FIELDS: Final = ("duplicate_of", "possible_duplicate_of")
"""The fields that name another tip."""


def brief_json(brief: tip_store.TipBrief | None) -> dict[str, Any] | None:
    """The short view of a referenced tip that a duplicate badge shows."""

    if brief is None:
        return None
    return {
        "id": brief.id,
        "name": brief.name,
        "action": brief.action,
        "bucket": brief.bucket,
        "status": brief.status,
        "source_id": brief.source_id,
    }


def tip_json(tip: tip_store.TipDetail, briefs: dict[int, tip_store.TipBrief]) -> dict[str, Any]:
    """A tip as the API returns it; ``briefs`` holds the tips it references."""

    start = None if tip.start_s is None else youtube.start_url(tip.source_ref, tip.start_s)
    return {
        "id": tip.id,
        "name": tip.name,
        "action": tip.action,
        "reason": tip.reason,
        "bucket": tip.bucket,
        "audience": tip.audience,
        "status": tip.status,
        "source_id": tip.source_id,
        "source_ref": tip.source_ref,
        "start_s": tip.start_s,
        "start_url": start,
        "locator": tip.locator,
        "evidence_url": tip.evidence_url,
        "duplicate_of": tip.duplicate_of,
        "duplicate_of_tip": brief_json(
            None if tip.duplicate_of is None else briefs.get(tip.duplicate_of)
        ),
        "possible_duplicate_of": tip.possible_duplicate_of,
        "possible_duplicate_of_tip": brief_json(
            None if tip.possible_duplicate_of is None else briefs.get(tip.possible_duplicate_of)
        ),
        "note": tip.note,
        "list_ids": list(tip.list_ids),
        "created_at": tip.created_at,
        "updated_at": tip.updated_at,
    }


def tips_json(
    conn: sqlite3.Connection, tips: Iterable[tip_store.TipDetail]
) -> list[dict[str, Any]]:
    """Each of ``tips`` as :func:`tip_json`, with the tips they reference read in one go."""

    items = list(tips)
    referenced = {
        tip_id
        for tip in items
        for tip_id in (tip.duplicate_of, tip.possible_duplicate_of)
        if tip_id is not None
    }
    briefs = tip_store.briefs(conn, referenced)
    return [tip_json(tip, briefs) for tip in items]


def _member(name: str, value: Any, members: Iterable[Any]) -> None:
    allowed = tuple(members)
    if value is not None and value not in allowed:
        shown = ", ".join(str(member) for member in allowed)
        raise ApiValidationError(f"{name} must be one of {shown}", [name])


@router.get("")
def list_tips(
    database: DatabaseDep,
    status: str | None = None,
    bucket: int | None = None,
    audience: str | None = None,
    source_id: Annotated[int | None, Query(ge=1, le=ID_MAX)] = None,
    unplaced: bool | None = None,
) -> list[dict[str, Any]]:
    """The tips matching every given filter, by id, each with its ``list_ids``."""

    _member("status", status, TIP_STATUSES)
    _member("bucket", bucket, BUCKET_IDS)
    _member("audience", audience, AUDIENCES_TIP)
    with database.session() as conn, connection.snapshot(conn):
        found = tip_store.find_tips(
            conn,
            status=status,
            bucket=bucket,
            audience=audience,
            source_id=source_id,
            unplaced=unplaced is True,
        )
        return tips_json(conn, found)


def _existing_tip(conn: sqlite3.Connection, tip_id: int) -> tip_store.TipDetail:
    tip = tip_store.get_tip(conn, tip_id)
    if tip is None:
        raise ApiNotFoundError(f"there is no tip #{tip_id}")
    return tip


def _check_references(conn: sqlite3.Connection, tip_id: int, body: TipPatch) -> None:
    named = {
        field: getattr(body, field)
        for field in REFERENCE_FIELDS
        if field in body.given() and getattr(body, field) is not None
    }
    for field, other in named.items():
        if other == tip_id:
            raise ApiValidationError(f"{field}: a tip cannot point at itself (#{tip_id})", [field])
    existing = tip_store.existing_ids(conn, named.values())
    for field, other in named.items():
        if other not in existing:
            raise ApiValidationError(f"{field}: there is no tip #{other}", [field])


def _check_list_ids(
    requested: list[int],
    *,
    bucket: int,
    audience: str,
    lists: dict[int, list_store.ListRow],
) -> None:
    """400 for a repeated or unknown list; 409 for a list the tip may not go on."""

    if len(set(requested)) != len(requested):
        raise ApiValidationError("list_ids names a list twice", ["list_ids"])
    unknown = [list_id for list_id in requested if list_id not in lists]
    if unknown:
        shown = ", ".join(str(list_id) for list_id in unknown)
        raise ApiValidationError(f"list_ids: there is no list {shown}", ["list_ids"])
    problems = placement.placement_problems(
        bucket=bucket, audience=audience, list_ids=requested, lists=lists
    )
    if problems:
        raise ApiConflictError("; ".join(problems), ["list_ids"])


@router.patch("/{tip_id}")
def patch_tip(tip_id: TipId, body: TipPatch, database: DatabaseDep) -> dict[str, Any]:
    """Edit a tip: ``{tip, removed_placements, cleared_duplicate_of}`` (module docstring)."""

    nulls = body.null_problems()
    if nulls:
        raise ApiValidationError(f"{', '.join(nulls)} may not be null", nulls)
    given = body.given()
    now = connection.format_timestamp(connection.utc_now())
    with database.session() as conn, connection.transaction(conn):
        current = _existing_tip(conn, tip_id)
        changes: dict[str, object] = {
            field: getattr(body, field) for field in tip_store.EDITABLE_COLUMNS if field in given
        }
        bucket = body.bucket if body.bucket is not None else current.bucket
        audience = body.audience if body.audience is not None else current.audience
        name = body.name if body.name is not None else current.name
        action = body.action if body.action is not None else current.action

        if "reason" in given and body.reason is None and current.source_kind != SOURCE_KIND_MANUAL:
            raise ApiValidationError(
                "reason may be null only on a manual tip; give one plain sentence", ["reason"]
            )
        too_long = proposal.line_problem(name, action)
        if too_long is not None:
            fields = [field for field in ("name", "action") if field in given] or ["action"]
            raise ApiValidationError(too_long, fields)
        _check_references(conn, tip_id, body)
        if body.duplicate_of is not None and bucket != BUCKET_LEFT_OUT:
            raise ApiValidationError(
                f"duplicate_of requires bucket {BUCKET_LEFT_OUT} (left out); this tip would be "
                f"in bucket {bucket}. Use possible_duplicate_of for a near match",
                ["duplicate_of"],
            )
        cleared: int | None = None
        if "duplicate_of" not in given and bucket != BUCKET_LEFT_OUT:
            cleared = current.duplicate_of
        if cleared is not None:
            changes["duplicate_of"] = None

        lists = list_store.lists_by_id(conn)
        placed = list(current.list_ids)
        removed: list[int] = []
        if body.list_ids is not None:
            _check_list_ids(body.list_ids, bucket=bucket, audience=audience, lists=lists)
            dropped = [list_id for list_id in placed if list_id not in body.list_ids]
            added = [list_id for list_id in body.list_ids if list_id not in placed]
        else:
            dropped = placement.incompatible_list_ids(
                bucket=bucket, audience=audience, placed=[lists[list_id] for list_id in placed]
            )
            added = []
            removed = dropped

        if given:
            # Placements first: the triggers refuse a bucket or audience change that
            # would leave an invalid one behind.
            list_store.remove_items(conn, tip_id, dropped)
            tip_store.update_tip(conn, tip_id, changes, now=now)
            for list_id in added:
                list_store.append_item(conn, list_id, tip_id)
        updated = _existing_tip(conn, tip_id)
        return {
            "tip": tips_json(conn, [updated])[0],
            "removed_placements": removed,
            "cleared_duplicate_of": cleared,
        }


@router.delete("/{tip_id}", status_code=204)
def delete_tip(tip_id: TipId, database: DatabaseDep) -> Response:
    """Delete a tip; the tips that duplicate it get a note naming it (plan §3.3)."""

    with database.session() as conn, connection.transaction(conn):
        if not tip_store.delete_tip(conn, tip_id):
            raise ApiNotFoundError(f"there is no tip #{tip_id}")
    return Response(status_code=204)
