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

LIST_TITLE_SEPARATOR: Final = LINE_SEPARATOR
"""Joins a list's place and audience in its display title, ``Coffee Machine — Adults``
(plan §3.2); the same dash as a printed line."""

REASON_MAX: Final = 110
"""One plain-language sentence."""

PRINT_CAPACITY: Final = 48
"""Rows that fit on one A4 page; more rows spill onto page 2."""

TIPS_MAX_EXTRACT: Final = 60
TIPS_MAX_INVESTIGATE: Final = 300
"""Proposal size caps per mode. Zero tips is allowed."""

# Caps on the optional free-text tip fields, the editable bucket texts and the
# proposal file. Plan §4.2 states none; these bound what an untrusted proposal or
# request can store, well above what a real tip or rubric needs.

LOCATOR_MAX: Final = 200
"""A tip's ``locator``: the original chapter title or wiki section heading."""

NOTE_MAX: Final = 300
"""A tip's ``note``: the skill's short remark on the bucket or a duplicate."""

EVIDENCE_URL_MAX: Final = 2048
"""A tip's ``evidence_url``: the http(s) page the tip came from."""

BUCKET_TITLE_MAX: Final = 40
"""A bucket's title (plan §3.4), edited on the Buckets page: one line, like a tip name."""

RUBRIC_MAX: Final = 2000
"""A bucket's rubric (plan §3.4), edited on the Buckets page and read by the skill
through ``context``. It may run to several lines; the seeded rubrics are under 250
characters."""

PLACE_MAX: Final = 40
"""A list's place (plan §3.3), such as ``Coffee Machine``, edited on the Lists page: one
line, like a tip name. The display title ``<place> — <Audience>`` heads its print sheet
in 16pt bold, so a place this long still fits on that one line."""

CUE_MAX: Final = 120
"""A list's cue (plan §3.3, §4.1), the editable "when you are here" rule the skill reads
through ``context`` to place daily tips: one line; the seeded cues are under 60 characters.
An empty cue is allowed (the schema's default)."""

PROPOSAL_FILE_MAX_BYTES: Final = 8 * 1024 * 1024
"""The largest proposal file ``propose`` reads. A proposal of ``TIPS_MAX_INVESTIGATE``
tips with every text field at its limit, every character written as a ``\\uXXXX``
escape, stays below it."""

# Bounds on the whole numbers of a proposal. JSON numbers have no size limit, and a
# Python int above SQLITE_INTEGER_MAX makes SQLite raise OverflowError, so every
# integer field is bounded here and reported as a field-level problem instead.
# (``bucket`` is bounded by BUCKET_IDS and the tip count by tips_max().)

SQLITE_INTEGER_MAX: Final = 2**63 - 1
"""The largest value a SQLite INTEGER column stores."""

ID_MAX: Final = SQLITE_INTEGER_MAX
"""The largest row id a proposal may name: ``list_ids``, ``duplicate_of`` and
``possible_duplicate_of``. A larger number cannot name a row."""

START_S_MAX: Final = 365 * 24 * 60 * 60
"""The largest ``start_s``, one year in seconds, whether or not the packet has a
``duration_s``. No video runs that long; with a duration the bound is ``duration_s``."""

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

PROPOSAL_MODES: Final = (SOURCE_MODE_EXTRACT, SOURCE_MODE_INVESTIGATE)
"""A proposal's ``mode`` (plan §4.5); ``propose`` stores it as ``source.mode``."""

HOST_CLAUDE_CODE: Final = "claude-code"
HOST_CODEX: Final = "codex"
PROPOSAL_HOSTS: Final = (HOST_CLAUDE_CODE, HOST_CODEX)
"""A proposal's ``host``: the agent host that ran the skill (plan §4.5)."""

TRANSCRIPT_KIND_GENERATED: Final = "generated"
TRANSCRIPT_KIND_MANUAL: Final = "manual"
TRANSCRIPT_KINDS: Final = (TRANSCRIPT_KIND_GENERATED, TRANSCRIPT_KIND_MANUAL)
"""A YouTube packet's ``transcript.kind`` (plan §4.4), also the first half of the
``source.revision`` ``<kind>:<language_code>``. ``manual`` here is a hand-made
caption track, unrelated to the manual source kind."""


def line_length(name: str, action: str) -> int:
    """Length of the printed ``name — action`` line, compared with ``LINE_MAX``."""

    return len(name) + len(LINE_SEPARATOR) + len(action)


def tips_max(mode: str) -> int:
    """The proposal size cap of a mode: ``TIPS_MAX_EXTRACT`` or ``TIPS_MAX_INVESTIGATE``."""

    if mode == SOURCE_MODE_INVESTIGATE:
        return TIPS_MAX_INVESTIGATE
    if mode == SOURCE_MODE_EXTRACT:
        return TIPS_MAX_EXTRACT
    raise ValueError(f"{mode!r} is not a proposal mode")
