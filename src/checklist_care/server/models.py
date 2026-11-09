"""Request models of the API (plan §6), built from ``limits.py``.

Every field type takes its bounds and enums from ``limits.py`` and its text rules
from ``proposal.text_problem``, so a request is held to exactly the rules a
proposal is. A request that fails them is a 400 ``CC_E_VALIDATION`` naming the
fields (``server/app.py`` turns FastAPI's validation error into that shape).
Types are strict: ``true`` is not a bucket and ``"3"`` is not an id.
"""

from __future__ import annotations

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
    ID_MAX,
    NAME_MAX,
    NOTE_MAX,
    REASON_MAX,
    TIP_STATUSES,
)


def _text_rule(limit: tuple[str, int]) -> Callable[[str], str]:
    def check(value: str) -> str:
        problem = proposal.text_problem(value, limit)
        if problem is not None:
            raise ValueError(problem)
        return value

    return check


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


class TipIds(BaseModel):
    """``POST /review/approve`` and ``POST /review/left-out``: ``{tip_ids: [...]}``."""

    model_config = ConfigDict(extra="forbid")

    tip_ids: list[RowId]
