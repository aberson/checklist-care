"""Placement rules (plan §3.3, §4.1): which tips may go on which lists.

* Only a bucket-1 (daily) tip goes on a list.
* A kids list accepts only kids+ tips; an adults list accepts both.

:func:`placement_problems` checks a requested placement and returns one plain
problem per violation, which ``propose`` reports field by field (and the API
will report the same way). :func:`incompatible_list_ids` names the placements a
bucket or audience change leaves invalid, so they can be removed in the same
transaction. The schema triggers back both rules up by aborting any violating
write.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from checklist_care.limits import AUDIENCE_KIDS, AUDIENCE_KIDS_PLUS, BUCKET_DAILY
from checklist_care.store.lists import ListRow


def compatible(*, bucket: int, audience: str, list_audience: str) -> bool:
    """True when a tip with ``bucket`` and ``audience`` may go on a list of ``list_audience``."""

    if bucket != BUCKET_DAILY:
        return False
    return list_audience != AUDIENCE_KIDS or audience == AUDIENCE_KIDS_PLUS


def placement_problems(
    *,
    bucket: int,
    audience: str | None,
    list_ids: Sequence[int],
    lists: Mapping[int, ListRow],
) -> list[str]:
    """Every reason the tip may not go on exactly ``list_ids``; empty when it may.

    ``lists`` holds every stored list by id. An empty ``list_ids`` (unplaced) is
    always valid. An ``audience`` of None (itself invalid, and reported by the
    caller) skips the audience rule.
    """

    problems: list[str] = []
    if list_ids and bucket != BUCKET_DAILY:
        problems.append(
            f"only bucket-{BUCKET_DAILY} (daily) tips go on lists; this tip is in bucket "
            f"{bucket}, so its list_ids must be []"
        )
    seen: set[int] = set()
    for list_id in list_ids:
        if list_id in seen:
            problems.append(f"list {list_id} is named twice")
            continue
        seen.add(list_id)
        row = lists.get(list_id)
        if row is None:
            known = ", ".join(str(key) for key in sorted(lists)) or "none"
            problems.append(f"list {list_id} does not exist (the lists are {known})")
        elif (
            bucket == BUCKET_DAILY
            and audience is not None
            and not compatible(bucket=bucket, audience=audience, list_audience=row.audience)
        ):
            problems.append(
                f"list {list_id} ({row.title}) is a {AUDIENCE_KIDS} list and accepts only "
                f"{AUDIENCE_KIDS_PLUS} tips; this tip is {audience}"
            )
    return problems


def incompatible_list_ids(*, bucket: int, audience: str, placed: Iterable[ListRow]) -> list[int]:
    """The ids of the ``placed`` lists a tip with ``bucket`` and ``audience`` may not stay on.

    A tip leaving bucket 1 loses every placement; a tip becoming adults loses its
    kids lists.
    """

    return [
        row.id
        for row in placed
        if not compatible(bucket=bucket, audience=audience, list_audience=row.audience)
    ]
