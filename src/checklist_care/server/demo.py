"""Synthetic demo data for ``checklist-care serve --demo`` (plan §5).

Everything here is self-written; no third-party text. The demo database is built
through the production path (``connection.connect`` applies the schema,
``seed.seed_database`` adds the four buckets, the seven lists and the manual
source), so the demo server shows the same rows a fresh ``init`` does, plus
:data:`DEMO_LISTS` and :data:`DEMO_SOURCES`:

* an earlier, reviewed video whose tips are approved, and an approved manual tip;
* a reviewed video and a reviewed text whose tips wait in the review inbox: one
  tip in every bucket, a duplicate (bucket 4, ``duplicate_of`` an approved tip)
  and a possible duplicate (``possible_duplicate_of`` another approved tip);
* for the print sheets, an approved video that brings Coffee Machine / adults
  (list 7) to :data:`COFFEE_LIST_TIPS` approved tips, gives the activities sheet
  at least two approved bucket-2 tips per audience, and leaves approved daily tips
  on no list ("Not on any list yet");
* the two capacity lists: Capacity Fits / adults (list 8) holds exactly
  ``PRINT_CAPACITY`` approved tips and Capacity Spills / adults (list 9)
  ``PRINT_CAPACITY + 1``. Every capacity tip is realistic text whose printed line
  is exactly ``LINE_MAX`` characters (:data:`CAPACITY_LINES`), so Playwright can
  show that a full sheet fits one A4 page and that one more row spills.

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
    PRINT_CAPACITY,
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


@dataclass(frozen=True)
class DemoList:
    """One list the demo adds after the seven seeded ones."""

    id: int
    place: str
    audience: str
    cue: str


COFFEE_LIST: Final = 7
"""Coffee Machine / adults, the list the runtime reviewers open at ``#/print/list/7``."""

COFFEE_LIST_TIPS: Final = 6
"""How many approved tips the demo puts on :data:`COFFEE_LIST`."""

CAPACITY_FITS_LIST: Final = 8
"""Capacity Fits / adults: exactly ``PRINT_CAPACITY`` approved tips, one A4 page."""

CAPACITY_SPILLS_LIST: Final = 9
"""Capacity Spills / adults: ``PRINT_CAPACITY + 1`` approved tips, one row too many."""

DEMO_LISTS: Final = (
    DemoList(
        CAPACITY_FITS_LIST,
        "Capacity Fits",
        AUDIENCE_ADULTS,
        "Print test: exactly one full A4 page of approved tips",
    ),
    DemoList(
        CAPACITY_SPILLS_LIST,
        "Capacity Spills",
        AUDIENCE_ADULTS,
        "Print test: one approved tip more than an A4 page holds",
    ),
)
"""The demo's extra lists, stored after the seeded ones with ``sort_order`` = id."""

CAPACITY_LINES: Final = (
    ("Refill The Water Jug", "Refill the water jug at the sink and put it in the fridge"),
    ("Clear The Breakfast Sink", "Wash up the breakfast dishes and leave the sink empty"),
    ("Open The Curtains Wide", "Open the curtains as soon as you come down each morning"),
    ("Wipe The Kitchen Counter", "Wipe the counter with a damp cloth after every dinner"),
    ("Read Tomorrow's Diary", "Look at tomorrow's plans and lay out the things you need"),
    ("Water Your Herb Pots", "Feel the soil of the herb pots and water any that are dry"),
    ("Empty The Lint Trap", "Pull the fluff out of the dryer lint trap after every load"),
    ("Take Your Daily Vitamins", "Take your vitamins with a glass of water at breakfast"),
    ("Sort The Morning Post", "Open your post at the door and recycle the junk mail now"),
    ("Charge The Headphones", "Plug the headphones in to charge before you go to dinner"),
    ("Plan Your Top Three Tasks", "List the three tasks that matter most today on paper"),
    ("Stretch Your Calves", "Rise up on your toes ten times while the toast is browning"),
    ("Hang Up Your Coat", "Hang your coat on its own hook, not over the back of a chair"),
    ("Rinse The Coffee Pot", "Rinse the coffee pot and leave it upside down on the rack"),
    ("Tidy The Shoe Rack", "Pair up the shoes on the rack and carry any strays upstairs"),
    ("Turn On The Dishwasher", "Start the dishwasher before bed if it is over half full"),
    ("Breathe Before Calls", "Take two slow breaths before you answer a work phone call"),
    ("Check The Weather", "Check the weather before you choose a coat for the day ahead"),
    ("Put Keys In The Bowl", "Drop your keys and wallet into the bowl by the front door"),
    ("Clean Your Glasses", "Polish your glasses with the soft lens cloth before reading"),
    ("Feed The Cat First", "Feed the cat and fill up her water bowl before you make tea"),
    ("Back Up Your Photos", "Plug the phone in at night so all the day's photos back up"),
    ("Wipe Down The Cool Hob", "Wipe the hob once it has cooled so spills never bake on"),
    ("Close The Work Laptop", "Close the laptop lid at the end of work and tuck it away"),
    ("Fold The Sofa Blanket", "Fold the sofa blanket over the arm when you go up to bed"),
    ("Check The Back Door", "Make sure the back door is locked before the lights go out"),
    ("Pack Tomorrow's Lunch", "Pack tomorrow's lunch in a box and keep it in the fridge"),
    ("Move For Two Minutes", "Walk around the house for two minutes after every meeting"),
    ("Floss After Brushing", "Floss between every tooth after you brush them each night"),
    ("Write Three Thanks", "Write down three good things about the day before you sleep"),
    ("Air Out The Bedroom", "Open the window wide for ten minutes when you make the bed"),
    ("Set Out Your Clothes", "Lay out tomorrow's clothes on your chair before you sleep"),
    ("Clear The Desk Top", "Put every pen and paper back in its place when work is over"),
    ("Check The Fridge First", "Look in the fridge and eat the food that goes off first"),
    ("Drink Before Coffee", "Drink one glass of water before you pour your first coffee"),
    ("Unload The Warm Dryer", "Take the washing out of the dryer while it is still warm"),
    ("Reply To Old Messages", "Answer every message that has waited for more than a day"),
    ("Return The Mugs Today", "Take every mug back to the kitchen when you leave a room"),
    ("Stand Up Straight", "Roll your shoulders back and stand tall as your kettle boils"),
    ("Empty The Kitchen Bin", "Take the bin bag out to the wheelie bin when it fills up"),
    ("Wash Your Hands Well", "Wash with soap for twenty seconds as soon as you get home"),
    ("Rest Your Tired Eyes", "Focus on something far away for twenty seconds every hour"),
    ("Prep The Coffee Machine", "Fill the machine with water and beans the night before"),
    ("Recycle The Boxes", "Flatten the delivery boxes and put them in the recycling bin"),
    ("Check Your Desk Posture", "Sit back with both your feet flat on the floor at work"),
    ("Note The Shopping", "Add anything that has just run out to your shopping list now"),
    ("Lock The Car Twice", "Press the lock button and wait for the beep as you walk off"),
    ("Switch Off The Plugs", "Switch off the plugs you don't need on your way up to bed"),
    ("Read Ten Pages In Bed", "Read ten pages of your book in bed instead of your phone"),
)
"""``PRINT_CAPACITY + 1`` everyday tips as ``(name, action)``, each printed line exactly
``LINE_MAX`` characters: the first ``PRINT_CAPACITY`` go on both capacity lists, the
last one on Capacity Spills only."""

CAPACITY_REASON: Final = (
    "One small daily habit from a full page of them, written to test long sheets."
)
"""The shared reason of the capacity tips."""

DEMO_REVISION: Final = f"{TRANSCRIPT_KIND_GENERATED}:en"
"""The ``source.revision`` of the demo videos: an auto-generated English transcript."""

DEMO_TEXT: Final = (
    "Demo notes from a talk on calmer school mornings: pack the night before, keep water "
    "handy, and make the library a regular outing."
)
"""The self-written text behind the demo text source's ref."""

DEMO_CAPACITY_TEXT: Final = (
    "Demo notes: a full page of small daily habits, written to check how a long checklist "
    "prints on A4."
)
"""The self-written text behind the capacity tips' source ref."""


def text_ref(text: str) -> str:
    """The ref of a pasted-text source: ``text:sha256:<hex of the UTF-8 text>``."""

    return f"{SOURCE_KIND_TEXT}:sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def capacity_tips(lines: tuple[tuple[str, str], ...] = CAPACITY_LINES) -> tuple[DemoTip, ...]:
    """The capacity tips: the first ``PRINT_CAPACITY`` of ``lines`` on both capacity
    lists, the one after them on Capacity Spills only."""

    if len(lines) != PRINT_CAPACITY + 1:
        raise RuntimeError(
            f"the capacity lists need PRINT_CAPACITY + 1 = {PRINT_CAPACITY + 1} lines, "
            f"not {len(lines)}"
        )
    return tuple(
        DemoTip(
            f"capacity-{number:02d}",
            name,
            action,
            CAPACITY_REASON,
            BUCKET_DAILY,
            AUDIENCE_ADULTS,
            list_ids=(
                (CAPACITY_FITS_LIST, CAPACITY_SPILLS_LIST)
                if number <= PRINT_CAPACITY
                else (CAPACITY_SPILLS_LIST,)
            ),
        )
        for number, (name, action) in enumerate(lines, start=1)
    )


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
        text_ref(DEMO_TEXT),
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
    DemoSource(
        SOURCE_KIND_YOUTUBE,
        youtube.source_ref("DEMOcoffee1"),
        "Demo video: Small habits around the morning coffee",
        TIP_STATUS_APPROVED,
        (
            DemoTip(
                "glass-first",
                "Glass Of Water First",
                "Drink a glass of water while the coffee brews",
                "A glass of water while you wait starts the day hydrated.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(COFFEE_LIST,),
                start_s=20,
            ),
            DemoTip(
                "top-three",
                "Pick Today's Top Three",
                "Write the day's three most important tasks on a note",
                "Naming the three tasks that matter keeps small jobs from filling the day.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(COFFEE_LIST,),
                start_s=64,
            ),
            DemoTip(
                "calf-raises",
                "Calf Raises While It Pours",
                "Rise onto your toes ten times as the coffee pours",
                "The wait for the cup is just long enough for a few calf raises.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(COFFEE_LIST,),
                start_s=118,
            ),
            DemoTip(
                "drip-tray",
                "Empty The Drip Tray",
                "Empty and rinse the drip tray after your first cup",
                "A tray rinsed every morning never smells or overflows.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(COFFEE_LIST,),
                start_s=171,
            ),
            DemoTip(
                "blinds",
                "Open The Kitchen Blinds",
                "Open the blinds while the machine warms up",
                "Daylight while the machine warms up helps you wake up.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                list_ids=(COFFEE_LIST,),
                start_s=205,
            ),
            DemoTip(
                "lights-off",
                "Lights Off As You Leave",
                "Switch off the light every time you leave a room",
                "Lights left on in empty rooms waste power all day.",
                BUCKET_DAILY,
                AUDIENCE_KIDS_PLUS,
                start_s=248,
            ),
            DemoTip(
                "fridge-list",
                "Shopping List On The Fridge",
                "Add what runs out to the list on the fridge door",
                "Writing it down at once saves a second trip to the shop.",
                BUCKET_DAILY,
                AUDIENCE_ADULTS,
                start_s=290,
            ),
            DemoTip(
                "board-games",
                "Family Board Game Night",
                "Pick a board game to play together this weekend",
                "A game night is an easy, screen-free evening everyone can join.",
                BUCKET_OCCASIONAL,
                AUDIENCE_KIDS_PLUS,
                start_s=333,
            ),
            DemoTip(
                "call-friend",
                "Call An Old Friend",
                "Phone a friend you have not spoken to in a while",
                "A short call keeps a friendship going between visits.",
                BUCKET_OCCASIONAL,
                AUDIENCE_ADULTS,
                start_s=372,
            ),
            DemoTip(
                "money-check",
                "Weekly Money Check",
                "Look over the week's spending every Sunday evening",
                "A quick weekly look catches surprises before the month ends.",
                BUCKET_OCCASIONAL,
                AUDIENCE_ADULTS,
                start_s=415,
            ),
            DemoTip(
                "fuse-box",
                "Label The Fuse Box",
                "Label each switch in the fuse box once",
                "Handy in a power cut, but a one-time job rather than a habit.",
                BUCKET_OPTIONAL,
                AUDIENCE_ADULTS,
                start_s=458,
                note="one-time setup",
            ),
            DemoTip(
                "late-espresso",
                "An Espresso Before Bed",
                "Have a strong coffee just before you go to sleep",
                "Caffeine late at night makes it harder to fall asleep.",
                BUCKET_LEFT_OUT,
                AUDIENCE_ADULTS,
                start_s=502,
                note="left out: it keeps you awake",
            ),
        ),
    ),
    DemoSource(
        SOURCE_KIND_TEXT,
        text_ref(DEMO_CAPACITY_TEXT),
        "Demo notes: a full page of small daily habits",
        TIP_STATUS_APPROVED,
        capacity_tips(),
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


def _store_lists(conn: sqlite3.Connection) -> None:
    for item in DEMO_LISTS:
        stored = list_store.insert_list(
            conn,
            place=item.place,
            audience=item.audience,
            cue=item.cue,
            sort_order=item.id,
            list_id=item.id,
        )
        if stored != item.id:  # pragma: no cover - an explicit id is kept
            raise RuntimeError(f"demo list {item.place!r} was stored as #{stored}")


def add_demo_tips(conn: sqlite3.Connection) -> dict[str, int]:
    """Store :data:`DEMO_LISTS`, then :data:`DEMO_SOURCES`, in one transaction; returns
    each demo tip's id by key."""

    now = connection.format_timestamp(connection.utc_now())
    ids: dict[str, int] = {}
    with connection.transaction(conn):
        _store_lists(conn)
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
