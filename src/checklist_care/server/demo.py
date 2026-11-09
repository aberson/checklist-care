"""Synthetic demo data for ``checklist-care serve --demo`` (plan §5).

Everything here is self-written; no third-party text. The demo database is built
through the production path (``connection.connect`` applies the schema,
``seed.seed_database`` adds the four buckets, the seven lists and the manual
source), so the demo server shows the same rows a fresh ``init`` does.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from checklist_care import paths
from checklist_care.db import connection, seed


def populate(data_dir: Path) -> Path:
    """Create the demo database in ``data_dir`` and return its path.

    ``data_dir`` gets the standard layout; like any data directory it must not be
    inside a git checkout.
    """

    paths.reject_git_checkout(data_dir)
    paths.ensure_layout(data_dir)
    db_path = data_dir / paths.DB_FILENAME
    with closing(connection.connect(db_path)) as conn:
        seed.seed_database(conn)
    return db_path
