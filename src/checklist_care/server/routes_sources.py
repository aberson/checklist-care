"""``GET /api/sources`` (plan §6): every source, with its tip counts.

``tip_counts`` maps each tip status (``limits.TIP_STATUSES``) to the number of the
source's tips in it, so the review inbox can name and order its groups and the
library can offer a source filter.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from checklist_care.db import connection
from checklist_care.limits import TIP_STATUSES
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.store import sources as source_store
from checklist_care.store import tips as tip_store

router = APIRouter(prefix=f"{API_PREFIX}/sources")


def source_json(row: source_store.SourceRow, tip_counts: dict[str, int]) -> dict[str, Any]:
    """A source as ``GET /sources`` returns it."""

    return {
        "id": row.id,
        "kind": row.kind,
        "ref": row.ref,
        "title": row.title,
        "url": row.url,
        "mode": row.mode,
        "reviewed_at": row.reviewed_at,
        "tip_counts": tip_counts,
    }


@router.get("")
def all_sources(database: DatabaseDep) -> list[dict[str, Any]]:
    """Every source, by id."""

    with database.session() as conn, connection.snapshot(conn):
        rows = source_store.all_sources(conn)
        counts = tip_store.counts_by_source(conn)
    return [source_json(row, counts.get(row.id, dict.fromkeys(TIP_STATUSES, 0))) for row in rows]
