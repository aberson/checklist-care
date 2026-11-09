"""``GET /api/health`` and ``GET /api/meta`` (plan §6).

``/api/meta`` is how the web app learns every ``limits.py`` value (form limits,
enums, the print capacity) and the bucket titles. :func:`build_meta` puts the
``limits.py`` objects themselves into the payload, never copies or restated
values, so the frontend can only see what the module defines.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter

from checklist_care import __version__, limits
from checklist_care.db import connection
from checklist_care.server import API_PREFIX
from checklist_care.server.database import DatabaseDep
from checklist_care.store import buckets as bucket_store

router = APIRouter(prefix=API_PREFIX)


def _is_public_constant(name: str, value: object) -> bool:
    # Module constants only: not the imported `annotations` feature or `Final`,
    # and not the helper functions.
    return not name.startswith("_") and isinstance(value, int | str | tuple)


def limit_values() -> dict[str, object]:
    """Every public constant of ``limits.py``, mapped to the module's own object."""

    return {name: value for name, value in vars(limits).items() if _is_public_constant(name, value)}


def build_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    """The ``/api/meta`` payload: the version, every ``limits.py`` constant, the current
    bucket titles and ``print_capacity``."""

    with connection.snapshot(conn):
        buckets = bucket_store.all_buckets(conn)
    return {
        "version": __version__,
        "limits": limit_values(),
        "buckets": [{"id": bucket.id, "title": bucket.title} for bucket in buckets],
        "print_capacity": limits.PRINT_CAPACITY,
    }


@router.get("/health")
def health(database: DatabaseDep) -> dict[str, Any]:
    """``{ok, version, data_dir}``: the readiness probe."""

    return {"ok": True, "version": __version__, "data_dir": str(database.path.parent)}


@router.get("/meta")
def meta(database: DatabaseDep) -> dict[str, Any]:
    """The limits, enums, bucket titles and print capacity (:func:`build_meta`)."""

    with database.session() as conn:
        return build_meta(conn)
