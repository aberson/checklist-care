"""``GET /api/buckets`` and ``PUT /api/buckets/{id}`` (plan §6): the bucket rubrics.

The four buckets are fixed (plan §3.4); their titles and rubrics are the operator's
to edit on the Buckets page. The rubric stored here is the one the skill reads on
its next ingest: ``checklist-care context`` prints the stored rubrics as they are
(``context.build_context``), so the text a ``PUT`` stores is exactly the text the
skill sees.

* ``GET /buckets``: every bucket by id, as ``[{id, title, rubric, updated_at}]``.
* ``PUT /buckets/{id}``: ``{title?, rubric?}``. At least one field; a field left out
  keeps its value; neither may be empty or null (400 ``CC_E_VALIDATION`` naming the
  field, nothing stored). The title follows the one-line tip-text rule up to
  ``BUCKET_TITLE_MAX``; the rubric may run to several lines up to ``RUBRIC_MAX``
  (``models.rubric_problem``). An unknown bucket is a 404. The reply is the stored
  bucket, with ``updated_at`` set to now.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from checklist_care.db import connection
from checklist_care.errors import ApiNotFoundError, ApiValidationError
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.server.models import BUCKET_FIELDS, BucketId, BucketPut
from checklist_care.store import buckets as bucket_store

router = APIRouter(prefix=f"{API_PREFIX}/buckets")


def bucket_json(row: bucket_store.BucketRow) -> dict[str, Any]:
    """A bucket as ``GET /buckets`` and ``PUT /buckets/{id}`` return it."""

    return {"id": row.id, "title": row.title, "rubric": row.rubric, "updated_at": row.updated_at}


@router.get("")
def all_buckets(database: DatabaseDep) -> list[dict[str, Any]]:
    """Every bucket, by id."""

    with database.session() as conn, connection.snapshot(conn):
        rows = bucket_store.all_buckets(conn)
    return [bucket_json(row) for row in rows]


@router.put("/{bucket_id}")
def put_bucket(bucket_id: BucketId, body: BucketPut, database: DatabaseDep) -> dict[str, Any]:
    """Store a new title and/or rubric for bucket ``bucket_id`` (module docstring)."""

    if not body.given():
        raise ApiValidationError("give a title, a rubric or both", list(BUCKET_FIELDS))
    nulls = body.null_problems()
    if nulls:
        raise ApiValidationError(f"{', '.join(nulls)} may not be null", nulls)
    now = connection.format_timestamp(connection.utc_now())
    with database.session() as conn, connection.transaction(conn):
        updated = bucket_store.update_bucket(
            conn, bucket_id, title=body.title, rubric=body.rubric, now=now
        )
        if updated is None:
            raise ApiNotFoundError(f"there is no bucket #{bucket_id}")
    return bucket_json(updated)
