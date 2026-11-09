"""``GET /api/print/lists/{id}`` and ``GET /api/print/activities`` (plan §4.7, §6).

The payloads come from ``printing.py``: a location sheet holds the approved daily
tips on one list, in position order, and the activities sheet the approved
occasional tips by audience. Pending tips and buckets 3 and 4 never print.

* ``/print/lists/{id}``: ``{title, audience, generated_at, items:[{name, action}],
  over_capacity}``; an unknown list is a 404.
* ``/print/activities``: ``{generated_at, groups:[{audience:"kids+", items},
  {audience:"adults", items}]}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from checklist_care import printing
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.server.models import ListId, no_such_list

router = APIRouter(prefix=f"{API_PREFIX}/print")


@router.get("/lists/{list_id}")
def print_list(list_id: ListId, database: DatabaseDep) -> dict[str, Any]:
    """The location sheet of one list."""

    with database.session() as conn:
        sheet = printing.location_sheet(conn, list_id)
    if sheet is None:
        raise no_such_list(list_id)
    return sheet.to_json()


@router.get("/activities")
def print_activities(database: DatabaseDep) -> dict[str, Any]:
    """The activities sheet."""

    with database.session() as conn:
        return printing.activities_sheet(conn).to_json()
