"""Request models of the API (plan §6), built from ``limits.py``.

Every field type takes its bounds and enums from ``limits.py`` and its text rules
from ``proposal.text_problem``, so a request is held to exactly the rules a
proposal is. A request that fails them is a 400 ``CC_E_VALIDATION`` naming the
fields (``server/app.py`` turns FastAPI's validation error into that shape).
Types are strict: ``true`` is not a bucket and ``"3"`` is not an id.

A bucket rubric is the one text that may run to several lines (:func:`rubric_problem`);
a bucket title follows the one-line tip-text rule.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from typing import Annotated, Final

from fastapi import Path
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from checklist_care import proposal
from checklist_care.errors import ApiNotFoundError
from checklist_care.limits import (
    ACTION_MAX,
    AUDIENCES_TIP,
    BUCKET_IDS,
    BUCKET_TITLE_MAX,
    ID_MAX,
    NAME_MAX,
    NOTE_MAX,
    REASON_MAX,
    RUBRIC_MAX,
    TIP_STATUSES,
)


def _text_rule(limit: tuple[str, int]) -> Callable[[str], str]:
    def check(value: str) -> str:
        problem = proposal.text_problem(value, limit)
        if problem is not None:
            raise ValueError(problem)
        return value

    return check


RUBRIC_WHITESPACE: Final = frozenset({"\n", "\t"})
"""The control characters a rubric may hold: line feeds and tabs."""

_RUBRIC_REFUSED: Final = frozenset({"Cc", "Zl", "Zp"})
"""Unicode categories a rubric may not hold, beyond :data:`RUBRIC_WHITESPACE`: control
codes and the line and paragraph separators."""


def rubric_problem(value: str) -> str | None:
    """Why ``value`` is not a valid bucket rubric, or None when it is.

    A rubric is non-blank and at most ``RUBRIC_MAX`` characters. Unlike a tip text it
    may run to several lines: it may hold line feeds and tabs, but no other control
    code (a carriage return included; the Buckets page sends LF line breaks), no
    Unicode line or paragraph separator and no unpaired surrogate. The stored text
    is exactly the text given, which is what ``checklist-care context`` prints.
    """

    if not value.strip():
        return "must not be empty"
    if len(value) > RUBRIC_MAX:
        return f"is {len(value)} characters; RUBRIC_MAX is {RUBRIC_MAX}; shorten it"
    for char in value:
        if char in RUBRIC_WHITESPACE:
            continue
        category = unicodedata.category(char)
        if category == "Cs":
            return proposal.UNPAIRED_SURROGATE
        if category in _RUBRIC_REFUSED:
            return (
                "may hold line breaks (LF) and tabs, but no carriage return, "
                f"other control code or Unicode line separator (U+{ord(char):04X})"
            )
    return None


def _rubric_rule(value: str) -> str:
    problem = rubric_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


def _one_of[Member: (str, int)](members: Sequence[Member]) -> Callable[[Member], Member]:
    shown = ", ".join(str(member) for member in members)

    def check(value: Member) -> Member:
        if value not in members:
            raise ValueError(f"must be one of {shown}")
        return value

    return check


RowId = Annotated[StrictInt, Field(ge=1, le=ID_MAX)]
"""A stored row id: a whole number from 1 to ``ID_MAX``."""

ListId = Annotated[int, Path(ge=1, le=ID_MAX)]
"""A list id in a path (``/lists/{id}/items``, ``/print/lists/{id}``): 1 to ``ID_MAX``."""


def no_such_list(list_id: int) -> ApiNotFoundError:
    """The 404 for a :data:`ListId` that names no list."""

    return ApiNotFoundError(f"there is no list #{list_id}")


BucketId = Annotated[int, Path(ge=1, le=ID_MAX)]
"""A bucket id in a path (``/buckets/{id}``): 1 to ``ID_MAX``; only 1-4 exist."""


TipName = Annotated[StrictStr, AfterValidator(_text_rule(("NAME_MAX", NAME_MAX)))]
TipAction = Annotated[StrictStr, AfterValidator(_text_rule(("ACTION_MAX", ACTION_MAX)))]
TipReason = Annotated[StrictStr, AfterValidator(_text_rule(("REASON_MAX", REASON_MAX)))]
TipNote = Annotated[StrictStr, AfterValidator(_text_rule(("NOTE_MAX", NOTE_MAX)))]
Bucket = Annotated[StrictInt, AfterValidator(_one_of(BUCKET_IDS))]
TipAudience = Annotated[StrictStr, AfterValidator(_one_of(AUDIENCES_TIP))]
TipStatus = Annotated[StrictStr, AfterValidator(_one_of(TIP_STATUSES))]

NOT_NULL_FIELDS: Final = ("name", "action", "bucket", "audience", "list_ids", "status")
"""``PATCH /tips/{id}`` fields that may be left out but never set to null."""


class TipPatch(BaseModel):
    """``PATCH /tips/{id}``: any of these fields; a field left out keeps its value.

    ``reason``, ``duplicate_of``, ``possible_duplicate_of`` and ``note`` may be set
    to null (a reason only on a manual tip, which the route checks).
    """

    model_config = ConfigDict(extra="forbid")

    name: TipName | None = None
    action: TipAction | None = None
    reason: TipReason | None = None
    bucket: Bucket | None = None
    audience: TipAudience | None = None
    list_ids: list[RowId] | None = None
    duplicate_of: RowId | None = None
    possible_duplicate_of: RowId | None = None
    note: TipNote | None = None
    status: TipStatus | None = None

    def given(self) -> frozenset[str]:
        """The fields the request named, null or not."""

        return frozenset(self.model_fields_set)

    def null_problems(self) -> list[str]:
        """The :data:`NOT_NULL_FIELDS` the request set to null."""

        given = self.given()
        return [name for name in NOT_NULL_FIELDS if name in given and getattr(self, name) is None]


class TipCreate(BaseModel):
    """``POST /tips``: a tip of your own (plan §6, "Add my own tip").

    ``name``, ``action``, ``bucket`` and ``audience`` are required; ``reason`` is
    optional (a manual tip may have none); ``list_ids`` names the lists to put it
    on, in order, and defaults to none. The route stores it approved under the
    manual source.
    """

    model_config = ConfigDict(extra="forbid")

    name: TipName
    action: TipAction
    reason: TipReason | None = None
    bucket: Bucket
    audience: TipAudience
    list_ids: list[RowId] = Field(default_factory=list)


class TipIds(BaseModel):
    """``POST /review/approve`` and ``POST /review/left-out``: ``{tip_ids: [...]}``."""

    model_config = ConfigDict(extra="forbid")

    tip_ids: list[RowId]


BucketTitle = Annotated[
    StrictStr, AfterValidator(_text_rule(("BUCKET_TITLE_MAX", BUCKET_TITLE_MAX)))
]
BucketRubric = Annotated[StrictStr, AfterValidator(_rubric_rule)]

BUCKET_FIELDS: Final = ("title", "rubric")
"""The fields ``PUT /buckets/{id}`` takes."""


class BucketPut(BaseModel):
    """``PUT /buckets/{id}``: ``{title?, rubric?}``, at least one, each non-empty.

    A field left out keeps its value; neither may be null (the route checks that and
    the empty body, so the 400 names the fields).
    """

    model_config = ConfigDict(extra="forbid")

    title: BucketTitle | None = None
    rubric: BucketRubric | None = None

    def given(self) -> frozenset[str]:
        """The fields the request named, null or not."""

        return frozenset(self.model_fields_set)

    def null_problems(self) -> list[str]:
        """The fields the request set to null, in :data:`BUCKET_FIELDS` order."""

        given = self.given()
        return [name for name in BUCKET_FIELDS if name in given and getattr(self, name) is None]
