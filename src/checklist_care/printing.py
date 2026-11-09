"""Print payload builders (plan §4.7, §6): what the A4 sheets show.

Nothing prints until it is approved (plan §8):

* a **location sheet** (``#/print/list/:id``) shows the approved daily (bucket-1)
  tips on one list, in ``position`` order;
* the **activities sheet** (``#/print/activities``) shows the approved occasional
  (bucket-2) tips, one group per tip audience: ``kids+``, then ``adults``.

Pending tips and buckets 3 and 4 never print. Each row is the tip's ``name`` and
``action``; the web page joins them with ``LINE_SEPARATOR`` on one 12pt line and
flags any row that would be cut. A location sheet holding more than
``PRINT_CAPACITY`` rows spills onto a second page (:func:`over_capacity`, which
``GET /lists`` uses too).

``server/routes_print.py`` serves these payloads, and the acceptance scorer reads
the same builders.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from checklist_care.db import connection
from checklist_care.limits import (
    AUDIENCES_TIP,
    BUCKET_DAILY,
    BUCKET_OCCASIONAL,
    PRINT_CAPACITY,
    TIP_STATUS_APPROVED,
)
from checklist_care.store import lists as list_store
from checklist_care.store import tips as tip_store

PRINTED_STATUS: Final = TIP_STATUS_APPROVED
"""The only tip status that prints."""

LOCATION_BUCKET: Final = BUCKET_DAILY
"""The bucket a location sheet prints: the daily checklist."""

ACTIVITIES_BUCKET: Final = BUCKET_OCCASIONAL
"""The bucket the activities sheet prints: occasional activities."""

ACTIVITY_AUDIENCES: Final = AUDIENCES_TIP
"""The activities sheet's groups, in order: ``kids+``, then ``adults``."""


def over_capacity(rows: int) -> bool:
    """True when a location sheet of ``rows`` rows spills onto a second A4 page."""

    return rows > PRINT_CAPACITY


def prints(*, status: str, bucket: int, sheet_bucket: int) -> bool:
    """True when a tip with ``status`` and ``bucket`` prints on a sheet of ``sheet_bucket``."""

    return status == PRINTED_STATUS and bucket == sheet_bucket


@dataclass(frozen=True)
class PrintItem:
    """One printed row: the bold ``name``, then the ``action``."""

    name: str
    action: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "action": self.action}


@dataclass(frozen=True)
class LocationSheet:
    """The print sheet of one list (``GET /print/lists/{id}``)."""

    list_id: int
    title: str
    """The list's display title, such as ``Coffee Machine — Adults``."""
    audience: str
    """The list's audience, one of ``limits.AUDIENCES_LIST``."""
    generated_at: str
    items: tuple[PrintItem, ...]

    @property
    def over_capacity(self) -> bool:
        return over_capacity(len(self.items))

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "audience": self.audience,
            "generated_at": self.generated_at,
            "items": [item.to_json() for item in self.items],
            "over_capacity": self.over_capacity,
        }


@dataclass(frozen=True)
class ActivityGroup:
    """The approved occasional activities of one tip audience."""

    audience: str
    """One of ``limits.AUDIENCES_TIP``."""
    items: tuple[PrintItem, ...]

    def to_json(self) -> dict[str, Any]:
        return {"audience": self.audience, "items": [item.to_json() for item in self.items]}


@dataclass(frozen=True)
class ActivitiesSheet:
    """The activities sheet (``GET /print/activities``)."""

    generated_at: str
    groups: tuple[ActivityGroup, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "groups": [group.to_json() for group in self.groups],
        }


def _now() -> str:
    return connection.format_timestamp(connection.utc_now())


def location_items(items: Iterable[list_store.ListItemRow]) -> tuple[PrintItem, ...]:
    """The printed rows of a list's items, in their order: approved daily tips only."""

    return tuple(
        PrintItem(item.name, item.action)
        for item in items
        if prints(status=item.status, bucket=item.bucket, sheet_bucket=LOCATION_BUCKET)
    )


def location_sheet(
    conn: sqlite3.Connection, list_id: int, *, now: str | None = None
) -> LocationSheet | None:
    """The print sheet of list ``list_id``, or None when there is no such list."""

    with connection.snapshot(conn):
        row = list_store.get_list(conn, list_id)
        if row is None:
            return None
        items = location_items(list_store.list_items(conn, list_id))
    return LocationSheet(
        list_id=row.id,
        title=row.title,
        audience=row.audience,
        generated_at=_now() if now is None else now,
        items=items,
    )


def activities_sheet(conn: sqlite3.Connection, *, now: str | None = None) -> ActivitiesSheet:
    """The activities sheet: the approved bucket-2 tips by audience, each group by tip id."""

    with connection.snapshot(conn):
        tips = tip_store.find_tips(conn, status=PRINTED_STATUS, bucket=ACTIVITIES_BUCKET)
    groups = tuple(
        ActivityGroup(
            audience=audience,
            items=tuple(
                PrintItem(tip.name, tip.action)
                for tip in tips
                if tip.audience == audience
                and prints(status=tip.status, bucket=tip.bucket, sheet_bucket=ACTIVITIES_BUCKET)
            ),
        )
        for audience in ACTIVITY_AUDIENCES
    )
    return ActivitiesSheet(generated_at=_now() if now is None else now, groups=groups)
