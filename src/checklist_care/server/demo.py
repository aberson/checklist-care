"""Synthetic demo data for ``checklist-care serve --demo`` (plan §5).

Everything here is self-written; no third-party text. The demo database is built
through the production path (``connection.connect`` applies the schema,
``seed.seed_database`` adds the four buckets, the seven lists and the manual
source), so the demo server shows the same rows a fresh ``init`` does, plus
:data:`DEMO_SOURCES`:

* an earlier, reviewed video whose tips are approved, and an approved manual tip;
* a reviewed video and a reviewed text whose tips wait in the review inbox: one
  tip in every bucket, a duplicate (bucket 4, ``duplicate_of`` an approved tip)
  and a possible duplicate (``possible_duplicate_of`` another approved tip).

Every tip is checked against the proposal's text rules (``proposal.text_problem``,
``proposal.line_problem``) and the placement rules (``placement.py``) before it
is stored, through the same store functions ``propose`` uses. The demo video ids
are made up, so their links open YouTube's "video unavailable" page.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from checklist_care import paths, placement, proposal
from checklist_care.db import connection, seed
from checklist_care.limits import (
    ACTION_MAX,
    AUDIENCE_ADULTS,
    AUDIENCE_KIDS_PLUS,
    BUCKET_DAILY,
    BUCKET_LEFT_OUT,
    BUCKET_OCCASIONAL,
    BUCKET_OPTIONAL,
    NAME_MAX,
    NOTE_MAX,
    REASON_MAX,
    SOURCE_KIND_MANUAL,
    SOURCE_KIND_TEXT,
    SOURCE_KIND_YOUTUBE,
    SOURCE_MODE_EXTRACT,
    TIP_STATUS_APPROVED,
    TIP_STATUS_PENDING,
    TRANSCRIPT_KIND_GENERATED,
)
from checklist_care.sources import youtube
from checklist_care.store import lists as list_store
from checklist_care.store import sources as source_store
from checklist_care.store import tips as tip_store


@dataclass(frozen=True)
class DemoTip:
    """One demo tip. ``key`` names it for the references of later tips."""

    key: str
    name: str
    action: str
    reason: str | None
    bucket: int
    audience: str
    list_ids: tuple[int, ...] = ()
    start_s: int | None = None
    duplicate_of: str | None = None
    """The ``key`` of the earlier tip this one repeats."""
    possible_duplicate_of: str | None = None
    """The ``key`` of the earlier tip this one nearly repeats."""
    note: str | None = None


@dataclass(frozen=True)
class DemoSource:
    """One demo source and its tips, all stored with ``status``."""

    kind: str
    ref: str
    title: str | None
    status: str
    tips: tuple[DemoTip, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str | None:
        if self.kind != SOURCE_KIND_YOUTUBE:
            return None
        return youtube.watch_url(self.ref.partition(":")[2])


DEMO_REVISION: Final = f"{TRANSCRIPT_KIND_GENERATED}:en"
"""The ``source.revision`` of the demo videos: an auto-generated English transcript."""

DEMO_TEXT: Final = (
    "Demo notes from a talk on calmer school mornings: pack the night before, keep water "
    "handy, and make the library a regular outing."
)
"""The self-written text behind the demo text source's ref."""

DEMO_SOURCES: Final = (
    DemoSource(
        SOURCE_KIND_MANUAL,
        seed.MANUAL_SOURCE_REF,
        seed.MANUAL_SOURCE_TITLE,
        TIP_STATUS_APPROVED,
        (
            DemoTip(
                "brush",
                "Brush Your Teeth",
                "Brush for two full minutes, morning and night",
                None,
                BUCKET_DAILY,
                AUDIENCE_KIDS_PLUS,
                list_ids=(3, 1),
            ),
        ),
    ),
    DemoSource(
        SOURCE_KIND_YOUTUBE,
        youtube.source_ref("DEMOearlier"),
        "Demo video: Habits for a calmer week",
        TIP_STATUS_APPROVED,
        (
            DemoTip(
                "keys",
                "Keys On The Hook",
                "Hang your keys on the hook as you walk in",
                "A fixed spot for the keys ends the morning hunt for them.",
                BUCKET_DAILY,
                AUDIENCE_KIDS_PLUS,
                list_ids=(5, 6),
                start_s=40,
            ),
            DemoTip(
                "kettle",
                "Stretch While The Kettle Boils",
                "Stretch your arms and back as the kettle heats",
                "Waiting for the kettle is a ready-made pause for a short stretch.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(7,),
                start_s=125,
            ),
            DemoTip(
                "walk",
                "Plan A Family Walk",
                "Pick one evening this week for a family walk",
                "A short walk together is easy to plan and gets everyone outside.",
                BUCKET_OCCASIONAL,
                AUDIENCE_KIDS_PLUS,
                start_s=210,
            ),
        ),
    ),
    DemoSource(
        SOURCE_KIND_YOUTUBE,
        youtube.source_ref("DEMOcalmday"),
        "Demo video: Ten small habits for easier mornings",
        TIP_STATUS_PENDING,
        (
            DemoTip(
                "shoes",
                "Shoes By The Door",
                "Before bed, line up tomorrow's shoes by the door",
                "Hunting for shoes is a classic late-morning scramble; setting them out avoids it.",
                BUCKET_DAILY,
                AUDIENCE_KIDS_PLUS,
                list_ids=(5, 6),
                start_s=30,
                note="daily, under two minutes, due when leaving the house",
            ),
            DemoTip(
                "desk-stretch",
                "Hourly Desk Stretch",
                "Stand and stretch at the top of every hour",
                "Breaking up long sitting eases a stiff back and neck.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(2,),
                start_s=95,
                possible_duplicate_of="kettle",
                note="near match: the same goal as the kettle stretch, at a different cue",
            ),
            DemoTip(
                "keys-again",
                "Hang Up Your Keys",
                "Put your keys on the hook the moment you get in",
                "One home for the keys saves the search when you head out again.",
                BUCKET_LEFT_OUT,
                AUDIENCE_KIDS_PLUS,
                start_s=160,
                duplicate_of="keys",
                note="duplicate: the same action at the same cue as Keys On The Hook",
            ),
            DemoTip(
                "phone",
                "Phone Sleeps Outside",
                "Charge your phone outside the bedroom at night",
                "With the phone out of reach it is easier to put it down and fall asleep.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(1,),
                start_s=232,
                note="daily, due at the bedtime routine",
            ),
            DemoTip(
                "dinners",
                "Sunday Dinner Plan",
                "Plan the week's dinners on Sunday afternoon",
                "Deciding meals once a week saves a nightly decision and extra shop trips.",
                BUCKET_OCCASIONAL,
                AUDIENCE_ADULTS,
                start_s=301,
                note="a weekly planning session",
            ),
            DemoTip(
                "cables",
                "Tidy The Desk Cables",
                "Bundle the cables under your desk once",
                "A tidy desk is nice to have, but it is a one-time setup, not a habit.",
                BUCKET_OPTIONAL,
                AUDIENCE_ADULTS,
                start_s=377,
                note="one-time setup",
            ),
            DemoTip(
                "router",
                "Read The Router Manual",
                "Read your internet router's manual cover to cover",
                "Rarely useful; the quick-start card covers what most homes need.",
                BUCKET_LEFT_OUT,
                AUDIENCE_ADULTS,
                start_s=446,
                note="left out: not worth the time",
            ),
        ),
    ),
    DemoSource(
        SOURCE_KIND_TEXT,
        f"{SOURCE_KIND_TEXT}:sha256:{hashlib.sha256(DEMO_TEXT.encode('utf-8')).hexdigest()}",
        "Demo notes: a talk on school mornings",
        TIP_STATUS_PENDING,
        (
            DemoTip(
                "bottle",
                "Water Bottle In The Bag",
                "Pack a filled water bottle in the school bag",
                "A bottle packed the night before means no thirsty afternoons at school.",
                BUCKET_DAILY,
                AUDIENCE_KIDS_PLUS,
                list_ids=(5,),
                note="daily, due when leaving for school",
            ),
            DemoTip(
                "library",
                "Library Trip Together",
                "Visit the library together once a month",
                "A regular library visit keeps new books coming without buying them.",
                BUCKET_OCCASIONAL,
                AUDIENCE_KIDS_PLUS,
                note="a monthly outing",
            ),
        ),
    ),
)
"""The demo sources in insertion order; tip ids follow this order."""


def tip_problems(tip: DemoTip, lists: dict[int, list_store.ListRow]) -> list[str]:
    """Every rule ``tip`` breaks: the proposal's text and line rules and the
    placement rules. The demo stores only tips with none."""

    problems: list[str] = []
    texts = (
        ("name", tip.name, ("NAME_MAX", NAME_MAX)),
        ("action", tip.action, ("ACTION_MAX", ACTION_MAX)),
        ("reason", tip.reason, ("REASON_MAX", REASON_MAX)),
        ("note", tip.note, ("NOTE_MAX", NOTE_MAX)),
    )
    for name, value, limit in texts:
        problem = None if value is None else proposal.text_problem(value, limit)
        if problem is not None:
            problems.append(f"{name} {problem}")
    line = proposal.line_problem(tip.name, tip.action)
    if line is not None:
        problems.append(line)
    if tip.duplicate_of is not None and tip.bucket != BUCKET_LEFT_OUT:
        problems.append("duplicate_of needs bucket 4")
    problems.extend(
        placement.placement_problems(
            bucket=tip.bucket, audience=tip.audience, list_ids=tip.list_ids, lists=lists
        )
    )
    return problems


def _store_source(
    conn: sqlite3.Connection, source: DemoSource, ids: dict[str, int], now: str
) -> None:
    if source.kind == SOURCE_KIND_MANUAL:
        row = source_store.get_by_ref(conn, source.ref)
        if row is None:  # pragma: no cover - seed_database always adds it
            raise RuntimeError("the seeded manual source is missing")
    else:
        row = source_store.upsert_fetched(
            conn,
            kind=source.kind,
            ref=source.ref,
            title=source.title,
            url=source.url,
            revision=DEMO_REVISION if source.kind == SOURCE_KIND_YOUTUBE else None,
            fetched_at=now,
        )
    lists = list_store.lists_by_id(conn)
    for tip in source.tips:
        problems = tip_problems(tip, lists)
        if problems:
            raise RuntimeError(f"demo tip {tip.key!r} breaks the rules: {'; '.join(problems)}")
        tip_id = tip_store.insert_tip(
            conn,
            tip_store.NewTip(
                name=tip.name,
                action=tip.action,
                reason=tip.reason,
                bucket=tip.bucket,
                audience=tip.audience,
                start_s=tip.start_s,
                duplicate_of=None if tip.duplicate_of is None else ids[tip.duplicate_of],
                possible_duplicate_of=(
                    None if tip.possible_duplicate_of is None else ids[tip.possible_duplicate_of]
                ),
                note=tip.note,
            ),
            source_id=row.id,
            status=source.status,
            now=now,
        )
        ids[tip.key] = tip_id
        for list_id in tip.list_ids:
            list_store.append_item(conn, list_id, tip_id)
    if source.kind != SOURCE_KIND_MANUAL:
        source_store.mark_reviewed(conn, row.id, mode=SOURCE_MODE_EXTRACT, reviewed_at=now)


def add_demo_tips(conn: sqlite3.Connection) -> dict[str, int]:
    """Store :data:`DEMO_SOURCES` in one transaction; returns each demo tip's id by key."""

    now = connection.format_timestamp(connection.utc_now())
    ids: dict[str, int] = {}
    with connection.transaction(conn):
        for source in DEMO_SOURCES:
            _store_source(conn, source, ids, now)
    return ids


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
        add_demo_tips(conn)
    return db_path
