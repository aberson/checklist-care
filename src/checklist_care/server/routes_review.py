"""``POST /api/review/approve`` and ``POST /api/review/left-out`` (plan §3.5, §6).

Both take ``{tip_ids: [...]}`` and change every named tip in one transaction. A
named tip that does not exist is a 404 and changes nothing.

* **approve** approves each tip: ``{approved: n}``, the number of tips that were
  pending.
* **left-out** takes each tip off every list, then puts it in bucket 4 and
  approves it; nothing is deleted, so a rejected idea still catches its own
  duplicates. ``{left_out: n}``, the number of tips that changed.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter

from checklist_care.db import connection
from checklist_care.errors import ApiNotFoundError
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.server.models import TipIds
from checklist_care.store import tips as tip_store

router = APIRouter(prefix=f"{API_PREFIX}/review")


def _require_tips(conn: sqlite3.Connection, tip_ids: list[int]) -> None:
    missing = sorted(set(tip_ids) - tip_store.existing_ids(conn, tip_ids))
    if missing:
        shown = ", ".join(f"#{tip_id}" for tip_id in missing)
        raise ApiNotFoundError(f"there is no tip {shown}; nothing was changed", ["tip_ids"])


@router.post("/approve")
def approve(body: TipIds, database: DatabaseDep) -> dict[str, int]:
    """Approve the named tips."""

    now = connection.format_timestamp(connection.utc_now())
    with database.session() as conn, connection.transaction(conn):
        _require_tips(conn, body.tip_ids)
        return {"approved": tip_store.approve(conn, body.tip_ids, now=now)}


@router.post("/left-out")
def left_out(body: TipIds, database: DatabaseDep) -> dict[str, int]:
    """Mark the named tips "Left out": bucket 4, approved, off every list."""

    now = connection.format_timestamp(connection.utc_now())
    with database.session() as conn, connection.transaction(conn):
        _require_tips(conn, body.tip_ids)
        return {"left_out": tip_store.leave_out(conn, body.tip_ids, now=now)}
