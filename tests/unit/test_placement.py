"""Placement rules (plan §3.3, §4.1) in ``placement.py``."""

from __future__ import annotations

import pytest

from checklist_care import placement
from checklist_care.db import seed
from checklist_care.limits import (
    AUDIENCE_ADULTS,
    AUDIENCE_KIDS,
    AUDIENCE_KIDS_PLUS,
    BUCKET_DAILY,
    BUCKET_IDS,
    BUCKET_LEFT_OUT,
    BUCKET_OCCASIONAL,
    BUCKET_OPTIONAL,
)
from checklist_care.store.lists import ListRow

LISTS = {
    item.id: ListRow(item.id, item.place, item.audience, item.cue, item.id) for item in seed.LISTS
}
KIDS_LIST = 3  # Kids Bathroom / kids
ADULTS_LIST = 7  # Coffee Machine / adults


@pytest.mark.parametrize(
    ("bucket", "audience", "list_audience", "allowed"),
    [
        (BUCKET_DAILY, AUDIENCE_KIDS_PLUS, AUDIENCE_KIDS, True),
        (BUCKET_DAILY, AUDIENCE_KIDS_PLUS, AUDIENCE_ADULTS, True),
        (BUCKET_DAILY, AUDIENCE_ADULTS, AUDIENCE_ADULTS, True),
        (BUCKET_DAILY, AUDIENCE_ADULTS, AUDIENCE_KIDS, False),
        (BUCKET_OCCASIONAL, AUDIENCE_KIDS_PLUS, AUDIENCE_ADULTS, False),
        (BUCKET_OPTIONAL, AUDIENCE_ADULTS, AUDIENCE_ADULTS, False),
        (BUCKET_LEFT_OUT, AUDIENCE_KIDS_PLUS, AUDIENCE_KIDS, False),
    ],
)
def test_compatible(bucket: int, audience: str, list_audience: str, allowed: bool) -> None:
    assert (
        placement.compatible(bucket=bucket, audience=audience, list_audience=list_audience)
        is allowed
    )


def test_no_lists_is_always_valid() -> None:
    for bucket in BUCKET_IDS:
        assert (
            placement.placement_problems(
                bucket=bucket, audience=AUDIENCE_ADULTS, list_ids=[], lists=LISTS
            )
            == []
        )


def test_a_daily_kids_tip_may_go_on_kids_and_adults_lists() -> None:
    assert (
        placement.placement_problems(
            bucket=BUCKET_DAILY,
            audience=AUDIENCE_KIDS_PLUS,
            list_ids=[KIDS_LIST, ADULTS_LIST],
            lists=LISTS,
        )
        == []
    )


def test_unknown_and_repeated_lists_are_one_problem_each() -> None:
    problems = placement.placement_problems(
        bucket=BUCKET_DAILY,
        audience=AUDIENCE_KIDS_PLUS,
        list_ids=[99, ADULTS_LIST, ADULTS_LIST],
        lists=LISTS,
    )
    assert len(problems) == 2


def test_an_unknown_audience_skips_only_the_audience_rule() -> None:
    assert (
        placement.placement_problems(
            bucket=BUCKET_DAILY, audience=None, list_ids=[KIDS_LIST], lists=LISTS
        )
        == []
    )
    assert (
        len(
            placement.placement_problems(
                bucket=BUCKET_DAILY, audience=None, list_ids=[99], lists=LISTS
            )
        )
        == 1
    )


def test_incompatible_list_ids_after_a_change() -> None:
    placed = [LISTS[KIDS_LIST], LISTS[ADULTS_LIST]]
    assert (
        placement.incompatible_list_ids(
            bucket=BUCKET_DAILY, audience=AUDIENCE_KIDS_PLUS, placed=placed
        )
        == []
    )
    assert placement.incompatible_list_ids(
        bucket=BUCKET_DAILY, audience=AUDIENCE_ADULTS, placed=placed
    ) == [KIDS_LIST]
    assert placement.incompatible_list_ids(
        bucket=BUCKET_OCCASIONAL, audience=AUDIENCE_KIDS_PLUS, placed=placed
    ) == [
        KIDS_LIST,
        ADULTS_LIST,
    ]
