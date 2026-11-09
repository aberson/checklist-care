"""Every text limit and enum of checklist-care (plan §4.2).

This module is the only definition. The proposal validator, the API models,
``checklist-care context`` and ``GET /api/meta`` import these objects; they
never restate the values. ``schema.sql`` necessarily spells the enums out in
its CHECK constraints, and ``tests/unit/test_limits.py`` fails if the two drift.
"""

from __future__ import annotations

from typing import Final

NAME_MAX: Final = 40
"""Catchy name, printed first on a checklist line."""

ACTION_MAX: Final = 60
"""Imperative "what to do", printed after the name."""

LINE_SEPARATOR: Final = " — "
"""Joins name and action on a printed line."""

LINE_MAX: Final = 80
"""``len(name) + len(LINE_SEPARATOR) + len(action)``: one 12pt Arial line."""

REASON_MAX: Final = 110
"""One plain-language sentence."""

PRINT_CAPACITY: Final = 48
"""Rows that fit on one A4 page; more rows spill onto page 2."""

TIPS_MAX_EXTRACT: Final = 60
TIPS_MAX_INVESTIGATE: Final = 300
"""Proposal size caps per mode. Zero tips is allowed."""

# Each enum is a tuple built from its named members, so code that needs one
# value names the member instead of restating its spelling.

AUDIENCE_KIDS_PLUS: Final = "kids+"
AUDIENCE_KIDS: Final = "kids"
AUDIENCE_ADULTS: Final = "adults"
AUDIENCES_TIP: Final = (AUDIENCE_KIDS_PLUS, AUDIENCE_ADULTS)
AUDIENCES_LIST: Final = (AUDIENCE_KIDS, AUDIENCE_ADULTS)

BUCKET_DAILY: Final = 1
BUCKET_OCCASIONAL: Final = 2
BUCKET_OPTIONAL: Final = 3
BUCKET_LEFT_OUT: Final = 4
BUCKET_IDS: Final = (BUCKET_DAILY, BUCKET_OCCASIONAL, BUCKET_OPTIONAL, BUCKET_LEFT_OUT)
"""1 daily checklist, 2 occasional activity, 3 optional, 4 left out."""

TIP_STATUS_PENDING: Final = "pending"
TIP_STATUS_APPROVED: Final = "approved"
TIP_STATUSES: Final = (TIP_STATUS_PENDING, TIP_STATUS_APPROVED)

SOURCE_KIND_YOUTUBE: Final = "youtube"
SOURCE_KIND_WIKI: Final = "wiki"
SOURCE_KIND_TEXT: Final = "text"
SOURCE_KIND_MANUAL: Final = "manual"
SOURCE_KINDS: Final = (SOURCE_KIND_YOUTUBE, SOURCE_KIND_WIKI, SOURCE_KIND_TEXT, SOURCE_KIND_MANUAL)

SOURCE_MODE_EXTRACT: Final = "extract"
SOURCE_MODE_INVESTIGATE: Final = "investigate"
SOURCE_MODE_MANUAL: Final = "manual"
SOURCE_MODES: Final = (SOURCE_MODE_EXTRACT, SOURCE_MODE_INVESTIGATE, SOURCE_MODE_MANUAL)


def line_length(name: str, action: str) -> int:
    """Length of the printed ``name — action`` line, compared with ``LINE_MAX``."""

    return len(name) + len(LINE_SEPARATOR) + len(action)
