"""Seed data (plan §3.4): the 4 buckets, the 7 lists and the manual source.

Idempotent: every row is ``INSERT OR IGNORE``, so a second run changes nothing
and never overwrites an edited title, rubric or cue.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from checklist_care.db.connection import format_timestamp, transaction, utc_now
from checklist_care.limits import (
    AUDIENCE_ADULTS,
    AUDIENCE_KIDS,
    BUCKET_DAILY,
    BUCKET_LEFT_OUT,
    BUCKET_OCCASIONAL,
    BUCKET_OPTIONAL,
    SOURCE_KIND_MANUAL,
    SOURCE_MODE_MANUAL,
)


@dataclass(frozen=True)
class BucketSeed:
    id: int
    title: str
    rubric: str


@dataclass(frozen=True)
class ListSeed:
    id: int
    place: str
    audience: str
    cue: str


# The ref of the one manual source (plan §3.1). It is a source ref, not an enum
# value; its kind and mode are the limits members.
MANUAL_SOURCE_REF: Final = "manual"
MANUAL_SOURCE_TITLE: Final = "My own tips"

BUCKETS: Final = (
    BucketSeed(
        BUCKET_DAILY,
        "Daily checklist",
        "Worth doing every day, or every time a cue happens. Takes about two minutes or less, "
        "needs no special equipment, and is manageable if a printed reminder hangs where you "
        "are when it is due. Has one clear physical action.",
    ),
    BucketSeed(
        BUCKET_OCCASIONAL,
        "Occasional activity",
        "Worth doing now and then (weekly, monthly, when the mood strikes) but takes real "
        "effort or time: a practice, project, outing or periodic review.",
    ),
    BucketSeed(
        BUCKET_OPTIONAL,
        "Optional / when time allows",
        "Reasonable but not a priority: one-time setups, general principles without a single "
        "action, situational hacks, nice-to-haves. Browsable; never printed.",
    ),
    BucketSeed(
        BUCKET_LEFT_OUT,
        "Left out",
        "Not worth doing, questionable or unsafe, or a duplicate of a tip already stored. "
        "A duplicate is the same action at the same cue or moment; a different reason does "
        "not make it new. Duplicates record which tip they repeat. Never printed.",
    ),
)

LISTS: Final = (
    ListSeed(
        1,
        "Adult Bathroom",
        AUDIENCE_ADULTS,
        "Morning and bedtime routines: teeth, face, getting ready",
    ),
    ListSeed(2, "My Office", AUDIENCE_ADULTS, "Working at the desk or computer"),
    ListSeed(3, "Kids Bathroom", AUDIENCE_KIDS, "Kids' morning and bedtime routines"),
    ListSeed(
        4,
        "Kids Bathroom",
        AUDIENCE_ADULTS,
        "Helping kids get ready; adults using the kids' bathroom",
    ),
    ListSeed(5, "Front Door", AUDIENCE_KIDS, "Leaving for school or coming home"),
    ListSeed(6, "Front Door", AUDIENCE_ADULTS, "Leaving the house or coming home"),
    ListSeed(7, "Coffee Machine", AUDIENCE_ADULTS, "First coffee or hot drink of the day"),
)


def seed_database(conn: sqlite3.Connection, *, now: str | None = None) -> None:
    """Insert the seed rows that are missing, in one transaction."""

    stamp = format_timestamp(utc_now()) if now is None else now
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO bucket (id, title, rubric, updated_at) VALUES (?, ?, ?, ?)",
            [(bucket.id, bucket.title, bucket.rubric, stamp) for bucket in BUCKETS],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO list (id, place, audience, cue, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            [(item.id, item.place, item.audience, item.cue, item.id) for item in LISTS],
        )
        conn.execute(
            "INSERT OR IGNORE INTO source (kind, ref, title, mode) VALUES (?, ?, ?, ?)",
            (SOURCE_KIND_MANUAL, MANUAL_SOURCE_REF, MANUAL_SOURCE_TITLE, SOURCE_MODE_MANUAL),
        )
