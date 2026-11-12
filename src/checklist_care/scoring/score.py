"""``checklist-care eval score --labels <file> [--json]``: the acceptance scorer (plan §4.8).

The scorer grades what the production skill stored, never a chat transcript. It reads
the database rows ``propose`` wrote (through ``store/``), each labelled source's cached
packet (decoded by :func:`context.decode_source_packet`, the decoder of the reader
``propose`` uses) and its archived proposal ``proposals/<source-key>.1.json`` (decoded by
:func:`proposal.decode`). Every input file (labels, packet, archived proposal) is read
by :func:`read_input`: only a regular file, and no further than its cap. A labels file
(``checklist-care.labels/1``, plan Appendix E) supplies only data: source refs, chapter
titles and expectations. The criterion logic lives here, keyed by id
(:data:`CRITERIA`); a labels file names the ids to score.

**Verdicts.** ``PASS`` (exit 0) when every scored hard criterion holds; ``FAIL`` (exit 1)
when any fails; ``ABORT`` (exit 6, ``CC_EVAL_ABORT``) when the input cannot give a
verdict; ``REPORTED (degraded)`` (exit 0, never a pass) when no abort holds and a scored
proposal says ``"degraded": true``: the criteria are still evaluated and reported, and
the verdict is neither PASS nor FAIL. Soft criteria are reported and never change the
verdict, and so are operator criteria (INV-6), whose verdict the operator records. An
abort is never a warning: the scorer does not fall back on a default, skip a label it
cannot read or score a source it cannot verify.

**Labelled sources** come in three kinds. A source with a ``ref`` is graded by the
per-source criteria; ``"harm_titled": true`` marks a video whose every chapter title
names a harm (OOS-3). A ``"reference": true`` source is read and verified like any
other, but only so that a chapter pair (OOS-4) can name its chapter: no per-source
criterion grades it. An anchor names a self-written file (``anchor_file``, relative to
the checkout, :data:`context.REPO_ROOT`) and the tips it must yield (``expect_tips``,
OOS-5); its ref is recomputed from the file by ``sources/text.py``
(:func:`text.read_text_file`), exactly as ``fetch --text-file`` computes it.

**Investigate sources** (mode ``investigate``) also get a spot check
(:func:`spot_checks`): the :data:`SPOT_CHECK_URLS` distinct evidence URLs whose sha256
is lowest, printed for the operator's INV-6 verdict. Every source's tip count and
spread across buckets are reported, never gated.

**Matching** is deterministic, and each tip lands in at most one chapter window
(:func:`chapter_index`): the chapter whose ``[start_s, end_s)`` holds the tip's
``start_s``, except that a ``start_s`` within the :data:`CHAPTER_LEAD_S` seconds before a
chapter's start counts toward that chapter. "The tip in chapter X" is the lowest tip id
in the window (with a note when there are several) and fails the criterion when the
window is empty. "On list X" means X is in the tip's ``list_ids``.

**ABORT rules** (each an :class:`Abort` with a stable ``rule``):

* the data dir came from neither ``--data-dir`` nor ``CHECKLIST_CARE_DATA_DIR``, or
  cannot be used (an empty value, a directory inside a git checkout:
  :func:`data_dir_abort`, which ``cli.py`` applies);
* the labels file is missing, not a regular file, larger than
  :data:`LABELS_FILE_MAX_BYTES`, malformed, names an unknown criterion id or lacks the
  data a criterion needs; or the database is missing or unreadable;
* a labelled anchor file cannot be read as text by ``sources/text.py`` (missing, not a
  regular file, binary, empty or too long: :data:`ABORT_ANCHOR`);
* a labelled source is missing or unreviewed;
* its packet is missing, not a regular file, larger than :data:`PACKET_FILE_MAX_BYTES`,
  unreadable, not its own or of an unexpected shape; a YouTube packet has zero segments
  or zero chapters; a wiki or text packet has empty text;
* a label's chapter title is not byte-equal to a chapter title of the packet;
* the archived proposal is missing, not a regular file, larger than
  ``PROPOSAL_FILE_MAX_BYTES`` (the cap ``propose`` reads it under) or unreadable (its
  ``degraded`` flag included), or its ``packet_sha256`` differs from the cached packet;
* the scored proposals mix ``host`` or ``skill_sha256`` values (mixed ``model`` values
  are reported, not aborted), or their ``skill_sha256`` is not the current fingerprint,
  computed by :func:`context.skill_sha256` like ``context`` and ``propose`` do;
* a stored value the scorer reads differs from the archived proposal, or a tip was
  added or deleted after the import (operator edits, or any other change to the rows):
  each scored tip's :data:`EDIT_FIELDS` (name, action, reason, bucket, audience,
  ``start_s``, ``list_ids``, ``duplicate_of``, ``possible_duplicate_of``, ``locator``,
  ``note``, ``evidence_url``) and the source's ``mode``. This goes beyond the plan's
  list (name, action, bucket, audience, ``list_ids``) on purpose: chapter matching reads
  ``start_s``, IS-1 the mode, IS-5 and IS-6 the duplicate fields, and IS-7 every field
  ``limits.py`` bounds, so a changed value would be scored as the skill's work. The
  criteria see a tip only as a :class:`ScoredTip`, which holds exactly the compared
  fields and the id.

**Results.** Every result, an abort included, is written to
``<data-dir>/eval/score-<YYYYMMDD-HHMMSS>.json``, except an abort raised before the
database is opened (data dir, labels or database), which writes nothing: the data dir
may not be the eval's own. The database is opened read-only; the score file is the
only thing the scorer writes, atomically and never over another file
(:func:`write_report`). The score file keeps every string exact; each printed report
line goes through :func:`printable`, so an untrusted string in it (a chapter title
from yt-dlp's metadata, a ref, a host or model name, an evidence URL) prints as inert
text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from checklist_care import context, paths, printing, proposal
from checklist_care.context import decode_source_packet
from checklist_care.db import connection
from checklist_care.db.connection import BUSY_TIMEOUT_PRAGMA, open_connection
from checklist_care.errors import (
    EXIT_EVAL_ABORT,
    EXIT_EVAL_FAIL,
    EXIT_OK,
    ConfigError,
    EvalAbortError,
    FetchError,
    ProposalInvalidError,
)
from checklist_care.limits import (
    BUCKET_DAILY,
    BUCKET_IDS,
    BUCKET_LEFT_OUT,
    ID_MAX,
    PROPOSAL_FILE_MAX_BYTES,
    SOURCE_KIND_TEXT,
    SOURCE_KIND_WIKI,
    SOURCE_KIND_YOUTUBE,
    SOURCE_MODE_EXTRACT,
    SOURCE_MODE_INVESTIGATE,
)
from checklist_care.paths import lacks_hard_links, numbered_paths, sibling_path
from checklist_care.placement import placement_problems
from checklist_care.proposal import (
    HASH_FIELDS,
    NOT_A_BUCKET,
    NOT_A_TIP_AUDIENCE,
    TIP_OPTIONAL,
    TIP_TEXT_LIMITS,
    holds_surrogate,
    is_bucket,
    is_http_url,
    is_id,
    is_sha256,
    is_tip_audience,
    line_problem,
    start_problem,
    text_problem,
)
from checklist_care.sources import packet as packet_module
from checklist_care.sources.text import TEXT_FILE_MAX_BYTES, read_text_file
from checklist_care.sources.wiki import WIKIPEDIA_DOMAIN, is_wikipedia_url
from checklist_care.store import lists as list_store
from checklist_care.store import sources as source_store
from checklist_care.store import tips as tip_store
from checklist_care.strict_json import (
    ENCODING_KINDS,
    JSON_LIMIT_ERRORS,
    NOT_REGULAR_FILE,
    JsonFileError,
    is_regular_file,
    is_whole,
    read_capped,
    read_error_reason,
)
from checklist_care.strict_json import decode as decode_json

LABELS_SCHEMA: Final = "checklist-care.labels/1"
SCORE_SCHEMA: Final = "checklist-care.score/1"
SCORE_FILE_PREFIX: Final = "score-"

CHAPTER_LEAD_S: Final = 5
"""A tip whose ``start_s`` falls within this many seconds before a chapter's start
(``start - 5 <= start_s < start``) counts toward that chapter (plan §4.8)."""

VIDEO_TIPS_MIN: Final = 8
VIDEO_TIPS_MAX: Final = 12
"""IS-2 and OOS-1: each scored video holds 8 to 12 tips."""

OOS_COVERAGE_MIN: Final = Fraction(9, 10)
"""OOS-1: at least 9/10 of each out-of-sample video's chapter windows hold a tip."""

FILLER_PATTERN: Final = re.compile("subscribe", re.IGNORECASE)
"""IS-8: no tip name or action matches ``/subscribe/i``."""

NAME_EXACT: Final = "exact"
"""The one value of a label's ``expect.name``: the tip is named with the chapter title."""

CHAPTER_HEADING: Final = re.compile(r"Ch\.\s?\d+\b")
"""INV-2: a book chapter heading of the article, such as ``Ch.1: The Divided Self``
(matched at the heading's start)."""

CHAPTER_HEADINGS: Final = 11
CHAPTER_HEADINGS_MIN: Final = 9
"""INV-2: at least 9 of the article's 11 "Ch.N" headings have a tip whose ``locator``
equals the heading. A packet with another number of such headings fails INV-2: the
criterion is "9 of the 11", so no other count can pass it."""

ELSEWHERE_MIN: Final = Fraction(1, 2)
"""INV-4: at least half the tips cite a host that does not end in ``wikipedia.org``."""

SPOT_CHECK_URLS: Final = 5
SPOT_CHECK_SUPPORTED_MIN: Final = 4
"""INV-6: the spot check prints the 5 evidence URLs whose sha256 is lowest, and the
operator's verdict needs at least 4 of them to open and support their tip."""

PASS: Final = "PASS"
FAIL: Final = "FAIL"
ABORT: Final = "ABORT"
REPORTED_DEGRADED: Final = "REPORTED (degraded)"
"""The verdict when a scored proposal says ``"degraded": true`` (plan §4.8): exit 0, but
never a pass."""
HARD: Final = "hard"
SOFT: Final = "soft"
OPERATOR: Final = "operator"
"""The gate of a criterion the operator judges (INV-6): listed and reported, never scored."""
OPERATOR_STATUS: Final = "OPERATOR"
"""An operator criterion's status: neither PASS nor FAIL, the operator's to record."""

LABELS_FILE_MAX_BYTES: Final = 1024 * 1024
"""A labels file holds a few chapter titles; a larger file is refused unread."""

PACKET_FILE_MAX_BYTES: Final = TEXT_FILE_MAX_BYTES
"""The largest cached packet the scorer reads; a larger one is refused unread. No packet
cap existed, so this one reuses the ingest cap: a packet keeps what ``fetch`` took from
one source, which is at most ``TEXT_CHARS_MAX`` characters of a text file (about 3.2 MB),
one Wikipedia article's plain text (from an answer of at most ``wiki.RESPONSE_MAX_BYTES``,
the same 16 MiB as ``TEXT_FILE_MAX_BYTES``) or one video's transcript. It stops a runaway
or foreign file, not a packet ``fetch`` wrote."""

ABORT_DATA_DIR: Final = "data-dir-not-explicit"
ABORT_DATA_DIR_UNUSABLE: Final = "data-dir-unusable"
ABORT_LABELS: Final = "labels-unusable"
ABORT_ANCHOR: Final = "anchor-unusable"
ABORT_DATABASE: Final = "database-unusable"
ABORT_LABEL_LIST: Final = "label-list-missing"
ABORT_SOURCE_MISSING: Final = "source-missing"
ABORT_SOURCE_UNREVIEWED: Final = "source-unreviewed"
ABORT_PACKET: Final = "packet-unusable"
ABORT_PACKET_EMPTY: Final = "packet-empty"
ABORT_CHAPTER_TITLE: Final = "chapter-title-mismatch"
ABORT_ARCHIVE: Final = "archive-unusable"
ABORT_PACKET_STALE: Final = "packet-stale"
ABORT_MIXED_HOST: Final = "mixed-host"
ABORT_MIXED_SKILL: Final = "mixed-skill"
ABORT_SKILL_STALE: Final = "skill-stale"
ABORT_OPERATOR_EDIT: Final = "operator-edit"


# --- labels (plan Appendix E) --------------------------------------------------------


class LabelsError(ValueError):
    """The labels file cannot be scored against; the message says why."""


class AnchorError(LabelsError):
    """A labelled anchor file cannot be read as text (:data:`ABORT_ANCHOR`)."""


@dataclass(frozen=True)
class Expect:
    """What a labelled chapter's tip must be. ``None`` (or False) checks nothing.

    ``on_lists`` lists ids the tip must be on; an empty ``on_lists`` means "on no
    list". ``on_any_of`` needs at least one of its ids. ``bucket_any_of`` needs the tip
    in one of its buckets. ``duplicate_of_name`` needs the tip's ``duplicate_of`` to
    name a stored tip of that exact name (such as the operator's own "Brush your
    teeth", OOS-S). ``exact_name`` (``"name": "exact"``) needs the tip to be named with
    the chapter title, byte for byte.
    """

    bucket: int | None = None
    not_bucket: int | None = None
    audience: str | None = None
    on_lists: tuple[int, ...] | None = None
    on_any_of: tuple[int, ...] | None = None
    exact_name: bool = False
    bucket_any_of: tuple[int, ...] | None = None
    duplicate_of_name: str | None = None

    @property
    def placement(self) -> bool:
        """True when the expectation checks the bucket, audience, lists or duplicate
        target (IS-4, OOS-2)."""

        values = (
            self.bucket,
            self.not_bucket,
            self.bucket_any_of,
            self.audience,
            self.on_lists,
            self.on_any_of,
            self.duplicate_of_name,
        )
        return any(value is not None for value in values)

    def list_ids(self) -> tuple[int, ...]:
        return (*(self.on_lists or ()), *(self.on_any_of or ()))


@dataclass(frozen=True)
class ChapterLabel:
    title: str
    expect: Expect


@dataclass(frozen=True)
class Anchor:
    """A self-written garbage anchor (OOS-5): its file and the tips it must yield."""

    path: Path
    """Absolute; a relative ``anchor_file`` is a path in the checkout (:func:`anchor_path`)."""
    expect_tips: int


@dataclass(frozen=True)
class SourceLabel:
    ref: str
    chapters: tuple[ChapterLabel, ...]
    harm_titled: bool = False
    """OOS-3: every chapter title of this video names a harm."""
    reference: bool = False
    """Read and verified only so that a chapter pair can name its chapters; no
    per-source criterion grades it."""
    anchor: Anchor | None = None
    """Set for an anchor, whose ``ref`` was recomputed from its file (:func:`anchor_ref`)."""

    @property
    def graded(self) -> bool:
        """True for a source the per-source criteria grade (OOS-1, INV-1 to INV-4): a
        labelled source that is neither a reference nor an anchor."""

        return not self.reference and self.anchor is None


@dataclass(frozen=True)
class ChapterRef:
    """A chapter of a labelled source, named by its exact title."""

    ref: str
    title: str


@dataclass(frozen=True)
class ChapterPair:
    """OOS-4: the tips in chapters ``a`` and ``b`` are not duplicates of each other:
    neither one's ``duplicate_of`` is the other."""

    a: ChapterRef
    b: ChapterRef


@dataclass(frozen=True)
class DuplicatePair:
    """IS-5: whichever of ``a`` and ``b`` was ingested first is bucket 1 on
    ``list_if_first``; the other is bucket 4 with ``duplicate_of`` the first."""

    a: ChapterRef
    b: ChapterRef
    list_if_first: int


@dataclass(frozen=True)
class SoftGroup:
    """IS-S: at least ``at_least`` of the ``titles`` chapters' tips meet ``expect``."""

    ref: str
    titles: tuple[str, ...]
    at_least: int
    expect: Expect


@dataclass(frozen=True)
class Labels:
    path: Path
    sha256: str
    claim: str
    sources: tuple[SourceLabel, ...]
    duplicate_pairs: tuple[DuplicatePair, ...]
    soft: tuple[SoftGroup, ...]
    criteria: tuple[str, ...]
    not_duplicate_pairs: tuple[ChapterPair, ...] = ()

    def chapter_refs(self) -> Iterator[ChapterRef]:
        """Every chapter the labels name, in file order (repeats included)."""

        for source in self.sources:
            for chapter in source.chapters:
                yield ChapterRef(source.ref, chapter.title)
        for pair in self.duplicate_pairs:
            yield pair.a
            yield pair.b
        for chapter_pair in self.not_duplicate_pairs:
            yield chapter_pair.a
            yield chapter_pair.b
        for group in self.soft:
            for title in group.titles:
                yield ChapterRef(group.ref, title)

    def list_ids(self) -> Iterator[int]:
        """Every list id the labels name."""

        for source in self.sources:
            for chapter in source.chapters:
                yield from chapter.expect.list_ids()
        for pair in self.duplicate_pairs:
            yield pair.list_if_first
        for group in self.soft:
            yield from group.expect.list_ids()


_LABELS_REQUIRED: Final = ("schema", "claim", "sources", "criteria")
_LABELS_OPTIONAL: Final = ("duplicate_pairs", "not_duplicate_pairs", "soft")
_SOURCE_KEYS: Final = ("ref", "chapters")
_SOURCE_OPTIONAL: Final = ("harm_titled", "reference")
ANCHOR_FILE_KEY: Final = "anchor_file"
"""The key that makes a ``sources`` entry an anchor (plan Appendix E)."""
_ANCHOR_KEYS: Final = (ANCHOR_FILE_KEY, "expect_tips")
_CHAPTER_KEYS: Final = ("title", "expect")
_EXPECT_KEYS: Final = (
    "bucket",
    "not_bucket",
    "bucket_any_of",
    "audience",
    "on_lists",
    "on_any_of",
    "duplicate_of_name",
    "name",
)
_PAIR_KEYS: Final = ("a", "b", "list_if_first")
_CHAPTER_PAIR_KEYS: Final = ("a", "b")
_SOFT_KEYS: Final = ("ref", "titles", "at_least", "expect")


def _keys(
    value: Any, where: str, required: Collection[str], optional: Collection[str] = ()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LabelsError(f"{where} must be a JSON object")
    unknown = sorted(key for key in value if key not in required and key not in optional)
    if unknown:
        raise LabelsError(
            f"{where} has the unknown key(s) {', '.join(unknown)}; the scorer never skips a "
            "label it cannot read"
        )
    missing = [key for key in required if key not in value]
    if missing:
        raise LabelsError(f"{where} lacks {', '.join(missing)}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LabelsError(f"{where} must be a non-empty string")
    return value


def _bucket(value: Any, where: str) -> int:
    if not is_bucket(value):
        raise LabelsError(f"{where} {NOT_A_BUCKET}")
    return int(value)


def _list_ids(value: Any, where: str, *, allow_empty: bool) -> tuple[int, ...]:
    if not isinstance(value, list) or not all(is_id(item) for item in value):
        raise LabelsError(f"{where} must be a list of list ids (whole numbers from 1)")
    if not value and not allow_empty:
        raise LabelsError(f"{where} must name at least one list id")
    if len(set(value)) != len(value):
        raise LabelsError(f"{where} repeats a list id")
    return tuple(int(item) for item in value)


def _buckets(value: Any, where: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise LabelsError(f"{where} must be a non-empty list of buckets")
    buckets = tuple(_bucket(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(set(buckets)) != len(buckets):
        raise LabelsError(f"{where} repeats a bucket")
    return buckets


def _expect(value: Any, where: str) -> Expect:
    obj = _keys(value, where, (), _EXPECT_KEYS)
    if not obj:
        raise LabelsError(f"{where} is empty; give at least one expectation")
    name = obj.get("name")
    if "name" in obj and name != NAME_EXACT:
        raise LabelsError(f'{where}.name must be "{NAME_EXACT}"')
    audience = obj.get("audience")
    if "audience" in obj and not is_tip_audience(audience):
        raise LabelsError(f"{where}.audience {NOT_A_TIP_AUDIENCE}")
    return Expect(
        bucket_any_of=(
            _buckets(obj["bucket_any_of"], f"{where}.bucket_any_of")
            if "bucket_any_of" in obj
            else None
        ),
        duplicate_of_name=(
            _text(obj["duplicate_of_name"], f"{where}.duplicate_of_name")
            if "duplicate_of_name" in obj
            else None
        ),
        bucket=_bucket(obj["bucket"], f"{where}.bucket") if "bucket" in obj else None,
        not_bucket=(
            _bucket(obj["not_bucket"], f"{where}.not_bucket") if "not_bucket" in obj else None
        ),
        audience=audience if isinstance(audience, str) else None,
        on_lists=(
            _list_ids(obj["on_lists"], f"{where}.on_lists", allow_empty=True)
            if "on_lists" in obj
            else None
        ),
        on_any_of=(
            _list_ids(obj["on_any_of"], f"{where}.on_any_of", allow_empty=False)
            if "on_any_of" in obj
            else None
        ),
        exact_name="name" in obj,
    )


def _chapter_ref(value: Any, where: str, refs: Collection[str]) -> ChapterRef:
    if not isinstance(value, list) or len(value) != 2:
        raise LabelsError(f"{where} must be [source ref, chapter title]")
    ref = _text(value[0], f"{where}[0]")
    if ref not in refs:
        raise LabelsError(f"{where} names {ref}, which is not one of the labelled sources")
    return ChapterRef(ref, _text(value[1], f"{where}[1]"))


def _flag(obj: dict[str, Any], key: str, where: str) -> bool:
    value = obj.get(key, False)
    if not isinstance(value, bool):
        raise LabelsError(f"{where}.{key} must be true or false")
    return value


def anchor_path(raw: str) -> Path:
    """The anchor file a labels ``anchor_file`` names: an absolute path as given, a
    relative one inside the checkout, :data:`context.REPO_ROOT` (looked up at call time,
    the root the skill fingerprint reads), so the result never depends on the working
    directory."""

    path = Path(raw)
    return path if path.is_absolute() else context.REPO_ROOT / path


def anchor_ref(path: Path) -> str:
    """The source ref of the anchor file at ``path``, recomputed by ``sources/text.py``
    exactly as ``fetch --text-file`` computes it (:func:`text.read_text_file`: decoded,
    normalized, hashed); :class:`AnchorError` when the file cannot be read as text."""

    try:
        return read_text_file(path).ref
    except FetchError as error:
        raise AnchorError(error.message) from error
    except ValueError as error:  # a path the OS cannot name, such as one holding a NUL
        raise AnchorError(f"cannot read the text file '{path}': {error}") from error


def _anchor(obj: dict[str, Any], where: str) -> SourceLabel:
    _keys(obj, where, _ANCHOR_KEYS)
    raw = _text(obj[ANCHOR_FILE_KEY], f"{where}.{ANCHOR_FILE_KEY}")
    expect_tips = obj["expect_tips"]
    if not is_whole(expect_tips) or expect_tips < 0:
        raise LabelsError(f"{where}.expect_tips must be a whole number from 0")
    path = anchor_path(raw)
    try:
        ref = anchor_ref(path)
    except AnchorError as error:
        raise AnchorError(f"{where}.{ANCHOR_FILE_KEY}: {error}") from error
    return SourceLabel(ref, (), anchor=Anchor(path, int(expect_tips)))


def _source(value: Any, where: str) -> SourceLabel:
    if isinstance(value, dict) and ANCHOR_FILE_KEY in value:
        return _anchor(value, where)
    obj = _keys(value, where, _SOURCE_KEYS, _SOURCE_OPTIONAL)
    ref = _text(obj["ref"], f"{where}.ref")
    raw_chapters = obj["chapters"]
    if not isinstance(raw_chapters, list):
        raise LabelsError(f"{where}.chapters must be a list")
    chapters: list[ChapterLabel] = []
    for index, raw in enumerate(raw_chapters):
        here = f"{where}.chapters[{index}]"
        chapter = _keys(raw, here, _CHAPTER_KEYS)
        title = _text(chapter["title"], f"{here}.title")
        if any(seen.title == title for seen in chapters):
            raise LabelsError(f"{here}.title repeats {title!r}")
        chapters.append(ChapterLabel(title, _expect(chapter["expect"], f"{here}.expect")))
    harm_titled = _flag(obj, "harm_titled", where)
    reference = _flag(obj, "reference", where)
    if reference and (chapters or harm_titled):
        raise LabelsError(
            f"{where} is a reference: it has no chapter expectations and is not harm_titled, "
            "since no per-source criterion grades it"
        )
    return SourceLabel(ref, tuple(chapters), harm_titled=harm_titled, reference=reference)


def _pair(value: Any, where: str, refs: Collection[str]) -> DuplicatePair:
    obj = _keys(value, where, _PAIR_KEYS)
    a = _chapter_ref(obj["a"], f"{where}.a", refs)
    b = _chapter_ref(obj["b"], f"{where}.b", refs)
    if a == b:
        raise LabelsError(f"{where} pairs a chapter with itself")
    first = obj["list_if_first"]
    if not is_id(first):
        raise LabelsError(f"{where}.list_if_first must be a list id (a whole number from 1)")
    return DuplicatePair(a, b, int(first))


def _chapter_pair(value: Any, where: str, refs: Collection[str]) -> ChapterPair:
    obj = _keys(value, where, _CHAPTER_PAIR_KEYS)
    a = _chapter_ref(obj["a"], f"{where}.a", refs)
    b = _chapter_ref(obj["b"], f"{where}.b", refs)
    if a == b:
        raise LabelsError(f"{where} pairs a chapter with itself")
    return ChapterPair(a, b)


def _soft(value: Any, where: str, refs: Collection[str]) -> SoftGroup:
    obj = _keys(value, where, _SOFT_KEYS)
    ref = _text(obj["ref"], f"{where}.ref")
    if ref not in refs:
        raise LabelsError(f"{where}.ref names {ref}, which is not one of the labelled sources")
    raw_titles = obj["titles"]
    if not isinstance(raw_titles, list) or not raw_titles:
        raise LabelsError(f"{where}.titles must be a non-empty list of chapter titles")
    titles = tuple(_text(title, f"{where}.titles[{i}]") for i, title in enumerate(raw_titles))
    if len(set(titles)) != len(titles):
        raise LabelsError(f"{where}.titles repeats a title")
    at_least = obj["at_least"]
    if not is_whole(at_least) or not 1 <= at_least <= len(titles):
        raise LabelsError(f"{where}.at_least must be a whole number from 1 to {len(titles)}")
    return SoftGroup(ref, titles, int(at_least), _expect(obj["expect"], f"{where}.expect"))


def _items(document: dict[str, Any], key: str) -> list[Any]:
    value = document.get(key, [])
    if not isinstance(value, list):
        raise LabelsError(f"{key} must be a list")
    return value


def _strict_json(data: bytes) -> Any:
    """The JSON value in the labels file bytes, read by :func:`strict_json.decode`, the
    decoder ``proposal.decode`` shares: a UTF-16 or NUL-holding file, bytes that are not
    UTF-8, repeated keys, ``NaN`` and ``Infinity`` are refused, so a label the scorer
    reads is the label that was written."""

    try:
        return decode_json(data)
    except JsonFileError as error:
        if error.kind in ENCODING_KINDS:
            raise LabelsError(f"{error.message}; save the labels as UTF-8") from error
        raise LabelsError(error.message) from error


def _refuse_surrogates(document: dict[str, Any]) -> None:
    """Refuse a labels value with a string (an object key included, at any depth) that
    holds an unpaired surrogate (:func:`proposal.holds_surrogate`), naming its top-level
    key, or the list item, such as ``sources[1]``. A ``\\ud800``-style JSON escape
    decodes to one, and it is not a character, so the score file (UTF-8) could not hold
    it. Each top-level key is a known name by now (:func:`_keys`)."""

    for key, value in document.items():
        places = (
            [(f"{key}[{index}]", item) for index, item in enumerate(value)]
            if isinstance(value, list)
            else [(key, value)]
        )
        for where, item in places:
            if holds_surrogate(item):
                raise LabelsError(
                    f"{where} holds an unpaired surrogate (a lone \\ud800 to \\udfff escape), "
                    "which is not a character and cannot be written to the UTF-8 score file; "
                    "write the character itself"
                )


def parse_labels(data: bytes, path: Path) -> Labels:
    """The labels in ``data`` (the bytes of ``path``); :class:`LabelsError` on any defect.

    Every key is known, no string holds an unpaired surrogate, every value has its
    type, every criterion id is one the scorer knows, and every criterion has the
    label data it reads. An anchor's file is read here, to recompute its ref
    (:func:`anchor_ref`); one that cannot be read is an :class:`AnchorError`.
    """

    document = _keys(_strict_json(data), "the labels file", _LABELS_REQUIRED, _LABELS_OPTIONAL)
    _refuse_surrogates(document)
    if document["schema"] != LABELS_SCHEMA:
        raise LabelsError(f"schema must be {LABELS_SCHEMA!r}")
    claim = _text(document["claim"], "claim")
    raw_sources = document["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise LabelsError("sources must be a non-empty list")
    sources = tuple(_source(raw, f"sources[{i}]") for i, raw in enumerate(raw_sources))
    refs = [source.ref for source in sources]
    if len(set(refs)) != len(refs):
        raise LabelsError("sources repeats a ref")
    pairs = tuple(
        _pair(raw, f"duplicate_pairs[{i}]", refs)
        for i, raw in enumerate(_items(document, "duplicate_pairs"))
    )
    not_pairs = tuple(
        _chapter_pair(raw, f"not_duplicate_pairs[{i}]", refs)
        for i, raw in enumerate(_items(document, "not_duplicate_pairs"))
    )
    soft = tuple(_soft(raw, f"soft[{i}]", refs) for i, raw in enumerate(_items(document, "soft")))
    raw_criteria = document["criteria"]
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise LabelsError("criteria must be a non-empty list of criterion ids")
    criteria = tuple(_text(item, f"criteria[{i}]") for i, item in enumerate(raw_criteria))
    if len(set(criteria)) != len(criteria):
        raise LabelsError("criteria repeats an id")
    labels = Labels(
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        claim=claim,
        sources=sources,
        duplicate_pairs=pairs,
        soft=soft,
        criteria=criteria,
        not_duplicate_pairs=not_pairs,
    )
    for criterion_id in criteria:
        criterion = CRITERIA.get(criterion_id)
        if criterion is None:
            raise LabelsError(
                f"criterion {criterion_id!r} is not one this scorer knows "
                f"({', '.join(CRITERIA)}); an unknown id is never skipped"
            )
        lacking = criterion.lacks(labels)
        if lacking is not None:
            raise LabelsError(f"{criterion_id} {lacking}")
    return labels


class InputFileError(ValueError):
    """An eval input file that is not a regular file or is larger than its cap; the
    message says which (``it is ...``)."""


def read_input(path: Path, max_bytes: int, cap_name: str) -> bytes:
    """The bytes of the eval input file at ``path``: the labels, a cached packet or an
    archived proposal.

    Only a regular file is opened (:func:`strict_json.is_regular_file`): a folder, a
    device or a pipe is an :class:`InputFileError` before anything reads it, since
    reading one can block or never end. So is a file larger than ``max_bytes`` (the cap
    ``cap_name``), which is read no further (:func:`strict_json.read_capped`). An
    ``OSError`` from inspecting or reading the file (``FileNotFoundError`` included)
    propagates. Each reader words either refusal in its own message.
    """

    if not is_regular_file(path):
        raise InputFileError(NOT_REGULAR_FILE)
    data = read_capped(path, max_bytes)
    if data is None:
        raise InputFileError(f"it is larger than {cap_name} ({max_bytes} bytes)")
    return data


def load_labels(path: Path) -> Labels:
    """Read and parse the labels file; :class:`EvalAbortError` when it is unusable,
    including a file that is not a regular file or is larger than
    :data:`LABELS_FILE_MAX_BYTES` (:func:`read_input`)."""

    try:
        data = read_input(path, LABELS_FILE_MAX_BYTES, "LABELS_FILE_MAX_BYTES")
    except OSError as error:
        reason = read_error_reason(error)
        raise EvalAbortError(
            f"{ABORT_LABELS}: cannot read the labels file {path}: {reason}"
        ) from error
    except InputFileError as error:
        raise EvalAbortError(f"{ABORT_LABELS}: {path}: {error}") from error
    try:
        return parse_labels(data, path)
    except AnchorError as error:
        raise EvalAbortError(f"{ABORT_ANCHOR}: {path}: {error}") from error
    except LabelsError as error:
        raise EvalAbortError(f"{ABORT_LABELS}: {path}: {error}") from error


# --- chapter windows ------------------------------------------------------------------


def chapter_index(start_s: int | None, chapters: Sequence[packet_module.Chapter]) -> int | None:
    """The index of the chapter window a tip starting at ``start_s`` belongs to, or None.

    A chapter whose start lies within :data:`CHAPTER_LEAD_S` seconds after ``start_s``
    claims the tip first (the nearest such start wins); otherwise the chapter whose
    ``[start_s, end_s)`` holds it. ``chapters`` are in ascending, non-overlapping order
    (:func:`packet_aborts` refuses any other shape). A tip without ``start_s``, or in
    a gap between chapters, belongs to none.
    """

    if start_s is None:
        return None
    for index, chapter in enumerate(chapters):
        if chapter.start_s - CHAPTER_LEAD_S <= start_s < chapter.start_s:
            return index
    for index, chapter in enumerate(chapters):
        if chapter.start_s <= start_s < chapter.end_s:
            return index
    return None


def group_by_chapter(
    tips: Sequence[ScoredTip], chapters: Sequence[packet_module.Chapter]
) -> dict[str, tuple[ScoredTip, ...]]:
    """Each chapter title (in packet order) mapped to its window's tips, lowest id first."""

    groups: dict[str, list[ScoredTip]] = {chapter.title: [] for chapter in chapters}
    for tip in sorted(tips, key=lambda item: item.id):
        index = chapter_index(tip.start_s, chapters)
        if index is not None:
            groups[chapters[index].title].append(tip)
    return {title: tuple(found) for title, found in groups.items()}


# --- evidence: what the production path stored ----------------------------------------


@dataclass(frozen=True)
class Abort:
    """One reason the input cannot give a verdict."""

    rule: str
    detail: str

    def line(self) -> str:
        return f"{self.rule}: {self.detail}"


@dataclass(frozen=True)
class TipFields:
    """Every tip field the scorer reads, besides the id: chapter matching reads
    ``start_s``, the criteria read the rest (IS-7 every field ``limits.py`` bounds).

    The archived proposal holds them too, and the operator-edit abort compares each one
    (:data:`EDIT_FIELDS`) with the stored tip, so a criterion never scores a value the
    skill did not write. The criteria see a stored tip only as a :class:`ScoredTip`,
    which holds nothing else: a field a future criterion needs goes here, and is then
    compared with the archive like the others.
    """

    name: str
    action: str
    reason: str | None
    bucket: int
    audience: str
    start_s: int | None
    list_ids: tuple[int, ...]
    """Sorted: the order a tip was placed on its lists is no part of the comparison."""
    duplicate_of: int | None
    possible_duplicate_of: int | None
    locator: str | None
    note: str | None
    evidence_url: str | None


EDIT_FIELDS: Final = tuple(item.name for item in fields(TipFields))
"""The tip fields compared with the archived proposal (the operator-edit abort)."""


@dataclass(frozen=True)
class ScoredTip(TipFields):
    """A stored tip as the criteria see it: its id and its :class:`TipFields`."""

    id: int

    @classmethod
    def of(cls, tip: tip_store.TipDetail) -> ScoredTip:
        """The stored tip's id and its :data:`EDIT_FIELDS`, its lists sorted."""

        values = {name: getattr(tip, name) for name in EDIT_FIELDS}
        return cls(id=tip.id, **{**values, "list_ids": tuple(sorted(tip.list_ids))})


@dataclass(frozen=True)
class Archived:
    """The archived proposal ``proposals/<source-key>.1.json`` of a source."""

    path: Path
    packet_sha256: str
    skill_sha256: str
    host: str
    model: str
    mode: str
    tips: tuple[TipFields, ...]
    degraded: bool = False
    """The proposal's ``degraded`` flag: true makes the verdict ``REPORTED (degraded)``."""


class ArchiveError(ValueError):
    """The archived proposal is missing or unreadable; the message says why."""


def _optional_whole(value: Any, where: str, what: str) -> int | None:
    if value is None:
        return None
    if not is_whole(value):
        raise ArchiveError(f"{where} is not {what}")
    return int(value)


def _optional_text(value: Any, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ArchiveError(f"{where} is not a string or null")
    return value


def _archived_tip(value: Any, index: int) -> TipFields:
    where = f"tips[{index}]"
    if not isinstance(value, dict):
        raise ArchiveError(f"{where} is not an object")
    texts = {name: value.get(name) for name in ("name", "action", "reason", "audience")}
    bucket, list_ids = value.get("bucket"), value.get("list_ids")
    if not all(isinstance(text, str) for text in texts.values()):
        raise ArchiveError(f"{where} lacks its name, action, reason or audience")
    if not is_whole(bucket):
        raise ArchiveError(f"{where}.bucket is not a whole number")
    if not isinstance(list_ids, list) or not all(is_whole(item) for item in list_ids):
        raise ArchiveError(f"{where}.list_ids is not a list of ids")
    return TipFields(
        name=str(texts["name"]),
        action=str(texts["action"]),
        reason=str(texts["reason"]),
        bucket=int(bucket),
        audience=str(texts["audience"]),
        start_s=_optional_whole(value.get("start_s"), f"{where}.start_s", "a whole second"),
        list_ids=tuple(sorted(int(item) for item in list_ids)),
        duplicate_of=_optional_whole(
            value.get("duplicate_of"), f"{where}.duplicate_of", "a tip id"
        ),
        possible_duplicate_of=_optional_whole(
            value.get("possible_duplicate_of"), f"{where}.possible_duplicate_of", "a tip id"
        ),
        locator=_optional_text(value.get("locator"), f"{where}.locator"),
        note=_optional_text(value.get("note"), f"{where}.note"),
        evidence_url=_optional_text(value.get("evidence_url"), f"{where}.evidence_url"),
    )


def read_archive(data_dir: Path, ref: str) -> Archived:
    """The archived proposal of ``ref``, decoded by ``proposal.decode``; :class:`ArchiveError`
    when it is missing, not a regular file, larger than ``PROPOSAL_FILE_MAX_BYTES`` (the
    cap ``propose`` reads a proposal under; :func:`read_input`), unreadable, not
    ``ref``'s, or lacks a field the scorer reads."""

    path = proposal.archive_path(data_dir, ref)
    try:
        data = read_input(path, PROPOSAL_FILE_MAX_BYTES, "PROPOSAL_FILE_MAX_BYTES")
    except FileNotFoundError as error:
        raise ArchiveError(f"no archived proposal at {path}") from error
    except OSError as error:
        raise ArchiveError(f"cannot read {path}: {read_error_reason(error)}") from error
    except InputFileError as error:
        raise ArchiveError(f"{path}: {error}") from error
    try:
        document = proposal.decode(data)
    except ProposalInvalidError as error:
        detail = "; ".join(problem.problem for problem in error.problems)
        raise ArchiveError(f"{path} is not a readable proposal: {detail}") from error
    if document.get("source_ref") != ref:
        raise ArchiveError(f"{path} is the proposal of {document.get('source_ref')!r}, not {ref}")
    hashes: dict[str, str] = {}
    for name in HASH_FIELDS:
        value = document.get(name)
        if not is_sha256(value):
            raise ArchiveError(f"{path} has no {name} (64 hex digits)")
        hashes[name] = value.lower()
    host, model, mode = document.get("host"), document.get("model"), document.get("mode")
    if not isinstance(host, str) or not isinstance(model, str) or not isinstance(mode, str):
        raise ArchiveError(f"{path} lacks its host, model or mode")
    degraded = document.get("degraded")
    if not isinstance(degraded, bool):
        raise ArchiveError(f"{path} has no degraded flag (true or false)")
    tips = document.get("tips")
    if not isinstance(tips, list):
        raise ArchiveError(f"{path} has no tips list")
    try:
        archived_tips = tuple(_archived_tip(tip, index) for index, tip in enumerate(tips))
    except ArchiveError as error:
        raise ArchiveError(f"{path}: {error}") from error
    return Archived(
        path=path,
        packet_sha256=hashes["packet_sha256"],
        skill_sha256=hashes["skill_sha256"],
        host=host,
        model=model,
        mode=mode,
        tips=archived_tips,
        degraded=degraded,
    )


@dataclass(frozen=True)
class ScoredSource:
    """A labelled source and everything stored for it."""

    label: SourceLabel
    row: source_store.SourceRow
    ref_count: int
    """How many ``source`` rows carry this ref (IS-1: exactly one)."""
    packet: packet_module.Packet
    packet_sha256: str
    archived: Archived
    tips: tuple[ScoredTip, ...]
    """The source's tips, by id."""
    chapter_tips: Mapping[str, tuple[ScoredTip, ...]] = field(default_factory=dict)
    """Each chapter title mapped to its window's tips (:func:`group_by_chapter`)."""

    @property
    def ref(self) -> str:
        return self.label.ref


def scored_source(
    *,
    label: SourceLabel,
    row: source_store.SourceRow,
    ref_count: int,
    packet: packet_module.Packet,
    packet_sha256: str,
    archived: Archived,
    tips: Sequence[ScoredTip],
) -> ScoredSource:
    """A :class:`ScoredSource` with its tips sorted by id and grouped by chapter window."""

    ordered = tuple(sorted(tips, key=lambda tip: tip.id))
    return ScoredSource(
        label=label,
        row=row,
        ref_count=ref_count,
        packet=packet,
        packet_sha256=packet_sha256,
        archived=archived,
        tips=ordered,
        chapter_tips=group_by_chapter(ordered, packet.chapters),
    )


@dataclass(frozen=True)
class Evidence:
    """Everything the criteria read: the labels, the scored sources, the lists, the
    activities print payload and the names of the tips scored tips are duplicates of."""

    labels: Labels
    sources: Mapping[str, ScoredSource]
    """By ref, in labels order."""
    lists: Mapping[int, list_store.ListRow]
    activities: printing.ActivitiesSheet | None = None
    """The activities sheet's payload, built by the print service
    (:func:`printing.activities_sheet`) on the scored database (OOS-2)."""
    duplicate_names: Mapping[int, str] = field(default_factory=dict)
    """The name of each stored tip a scored tip's ``duplicate_of`` names, by id (an
    expectation's ``duplicate_of_name``)."""


def packet_aborts(ref: str, kind: str, packet: packet_module.Packet) -> list[Abort]:
    """Why the packet of the labelled source ``ref`` (of source ``kind``) cannot be scored."""

    if packet.source_ref != ref:
        return [Abort(ABORT_PACKET, f"the packet of {ref} belongs to {packet.source_ref}")]
    if packet.kind != kind:
        return [Abort(ABORT_PACKET, f"the packet of {ref} is {packet.kind}, its source {kind}")]
    if kind in (SOURCE_KIND_WIKI, SOURCE_KIND_TEXT):
        if not (packet.text or "").strip():
            return [Abort(ABORT_PACKET_EMPTY, f"the {kind} packet of {ref} has empty text")]
        return []
    if kind != SOURCE_KIND_YOUTUBE:
        return [Abort(ABORT_PACKET, f"{ref} is a {kind} source, which has no packet to score")]
    aborts: list[Abort] = []
    if packet.transcript is None or not packet.transcript.segments:
        aborts.append(Abort(ABORT_PACKET_EMPTY, f"the YouTube packet of {ref} has zero segments"))
    if not packet.chapters:
        aborts.append(Abort(ABORT_PACKET_EMPTY, f"the YouTube packet of {ref} has zero chapters"))
    previous: packet_module.Chapter | None = None
    titles: set[str] = set()
    for chapter in packet.chapters:
        shape: str | None = None
        if chapter.start_s < 0 or chapter.end_s <= chapter.start_s:
            shape = f"runs from {chapter.start_s} to {chapter.end_s}"
        elif previous is not None and chapter.start_s < previous.end_s:
            shape = f"starts at {chapter.start_s}, before the previous chapter ends"
        elif chapter.title in titles:
            shape = "repeats an earlier chapter title"
        if shape is not None:
            aborts.append(
                Abort(ABORT_PACKET, f"{ref} chapter {chapter.title!r} {shape}; windows are unclear")
            )
        titles.add(chapter.title)
        previous = chapter
    return aborts


def _loose(text: str) -> str:
    """``text`` casefolded (after NFKC) with every character that is not a letter or a
    digit stripped: punctuation, as OOS-3 compares a tip name with its harm title, and
    also white space and symbols, so a spacing variant of the title matches it too. The
    chapter-title abort uses it to name a near match."""

    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in folded if char.isalnum())


def title_aborts(ref: str, titles: Sequence[str], packet: packet_module.Packet) -> list[Abort]:
    """One abort per label title of ``ref`` that is not byte-equal to a packet chapter title."""

    known = [chapter.title for chapter in packet.chapters]
    aborts: list[Abort] = []
    for title in dict.fromkeys(titles):
        if title in known:
            continue
        near = [candidate for candidate in known if _loose(candidate) == _loose(title)]
        hint = f" (the packet has {near[0]!r})" if near else ""
        aborts.append(
            Abort(
                ABORT_CHAPTER_TITLE,
                f"the label title {title!r} of {ref} is not byte-equal to any chapter title of "
                f"its packet{hint}",
            )
        )
    return aborts


def edit_aborts(ref: str, tips: Sequence[ScoredTip], archived: Sequence[TipFields]) -> list[Abort]:
    """Operator edits: a stored tip (by id) differs in one of :data:`EDIT_FIELDS`, every
    field the scorer reads, from the archived proposal's tip (in file order, the order
    ``propose`` inserted them), or a tip was added or deleted."""

    if len(tips) != len(archived):
        return [
            Abort(
                ABORT_OPERATOR_EDIT,
                f"{ref} holds {len(tips)} tips but its archived proposal {len(archived)}: a tip "
                "was added or deleted after the import",
            )
        ]
    aborts: list[Abort] = []
    for tip, original in zip(sorted(tips, key=lambda item: item.id), archived, strict=True):
        changed = [name for name in EDIT_FIELDS if getattr(tip, name) != getattr(original, name)]
        if changed:
            aborts.append(
                Abort(
                    ABORT_OPERATOR_EDIT,
                    f"{ref} tip #{tip.id} differs from its archived proposal in "
                    f"{', '.join(changed)}",
                )
            )
    return aborts


def consistency_aborts(sources: Sequence[ScoredSource], current_skill: str) -> list[Abort]:
    """Mixed ``host`` or ``skill_sha256`` values, and a stale ``skill_sha256``."""

    aborts: list[Abort] = []
    hosts = sorted({source.archived.host for source in sources})
    if len(hosts) > 1:
        aborts.append(Abort(ABORT_MIXED_HOST, f"the scored proposals mix hosts {', '.join(hosts)}"))
    skills = sorted({source.archived.skill_sha256 for source in sources})
    if len(skills) > 1:
        aborts.append(
            Abort(
                ABORT_MIXED_SKILL,
                f"the scored proposals mix skill fingerprints {', '.join(skills)}",
            )
        )
    stale = [source.ref for source in sources if source.archived.skill_sha256 != current_skill]
    if stale:
        aborts.append(
            Abort(
                ABORT_SKILL_STALE,
                f"the proposals of {', '.join(stale)} were made by a skill that has changed since "
                f"(current skill_sha256 {current_skill}); re-run in a fresh data dir",
            )
        )
    return aborts


@dataclass(frozen=True)
class Collected:
    """What :func:`collect` read: the sources it could load, the lists, every abort."""

    sources: tuple[ScoredSource, ...]
    lists: Mapping[int, list_store.ListRow]
    aborts: tuple[Abort, ...]
    activities: printing.ActivitiesSheet | None = None
    duplicate_names: Mapping[int, str] = field(default_factory=dict)

    def evidence(self, labels: Labels) -> Evidence | None:
        """The criteria's input, or None when any abort holds."""

        if self.aborts:
            return None
        return Evidence(
            labels,
            {source.ref: source for source in self.sources},
            self.lists,
            activities=self.activities,
            duplicate_names=self.duplicate_names,
        )


def duplicate_targets(tips: Iterable[ScoredTip]) -> frozenset[int]:
    """The tip ids the ``duplicate_of`` of ``tips`` name; a value that is no tip id (a
    tampered one past ``ID_MAX``) is left out, since it names no stored tip (IS-7 reports it)."""

    return frozenset(
        tip.duplicate_of for tip in tips if tip.duplicate_of is not None and is_id(tip.duplicate_of)
    )


def collect(
    conn: sqlite3.Connection, data_dir: Path, labels: Labels, current_skill: str
) -> Collected:
    """Read every labelled source's rows, packet and archived proposal, and apply the
    abort rules against ``current_skill``, the current skill fingerprint.

    The rows (and the names of the tips their ``duplicate_of`` names) are read in one
    snapshot. The activities print payload is then built by the print service itself
    (:func:`printing.activities_sheet`, which takes its own snapshot), as ``GET
    /print/activities`` builds it; a printed row shows only a tip's name and action."""

    with connection.snapshot(conn):
        lists = list_store.lists_by_id(conn)
        rows = source_store.all_sources(conn)
        wanted = {source.ref for source in labels.sources}
        stored_tips = {
            row.id: tuple(ScoredTip.of(tip) for tip in tip_store.find_tips(conn, source_id=row.id))
            for row in rows
            if row.ref in wanted
        }
        targets = duplicate_targets(tip for tips in stored_tips.values() for tip in tips)
        duplicate_names = {
            tip_id: brief.name for tip_id, brief in tip_store.briefs(conn, sorted(targets)).items()
        }
    activities = printing.activities_sheet(conn)
    aborts = [
        Abort(ABORT_LABEL_LIST, f"the labels name list {list_id}, which does not exist")
        for list_id in sorted(set(labels.list_ids()))
        if list_id not in lists
    ]
    titles: dict[str, list[str]] = {}
    for chapter_ref in labels.chapter_refs():
        titles.setdefault(chapter_ref.ref, []).append(chapter_ref.title)
    sources: list[ScoredSource] = []
    for label in labels.sources:
        matching = [row for row in rows if row.ref == label.ref]
        if not matching:
            aborts.append(Abort(ABORT_SOURCE_MISSING, f"{label.ref} is not in the database"))
            continue
        row = matching[0]
        if row.reviewed_at is None:
            aborts.append(Abort(ABORT_SOURCE_UNREVIEWED, f"{label.ref} was never proposed"))
            continue
        packet_path = packet_module.packet_path(data_dir, label.ref)
        try:
            data = read_input(packet_path, PACKET_FILE_MAX_BYTES, "PACKET_FILE_MAX_BYTES")
            try:
                packet_sha256, packet = decode_source_packet(data)
            except packet_module.PacketFormatError:
                raise
            except JSON_LIMIT_ERRORS as error:  # an over-long integer, deep nesting
                raise packet_module.PacketFormatError(
                    f"packet is not UTF-8 JSON: {error}"
                ) from error
        except (OSError, InputFileError, packet_module.PacketFormatError) as error:
            detail = f"cannot read the packet of {label.ref} at {packet_path}: {error}"
            aborts.append(Abort(ABORT_PACKET, detail))
            continue
        shape = packet_aborts(label.ref, row.kind, packet)
        aborts.extend(shape)
        if not shape:
            aborts.extend(title_aborts(label.ref, titles.get(label.ref, []), packet))
        try:
            archived = read_archive(data_dir, label.ref)
        except ArchiveError as error:
            aborts.append(Abort(ABORT_ARCHIVE, str(error)))
            continue
        if archived.packet_sha256 != packet_sha256:
            aborts.append(
                Abort(
                    ABORT_PACKET_STALE,
                    f"the archived proposal of {label.ref} names packet_sha256 "
                    f"{archived.packet_sha256}, but the cached packet is {packet_sha256}",
                )
            )
        if row.mode != archived.mode:
            aborts.append(
                Abort(
                    ABORT_OPERATOR_EDIT,
                    f"{label.ref} is stored in mode {row.mode}, but its archived proposal is in "
                    f"mode {archived.mode}",
                )
            )
        tips = stored_tips[row.id]  # the tips of the row checked above, never another's
        aborts.extend(edit_aborts(label.ref, tips, archived.tips))
        sources.append(
            scored_source(
                label=label,
                row=row,
                ref_count=len(matching),
                packet=packet,
                packet_sha256=packet_sha256,
                archived=archived,
                tips=tips,
            )
        )
    aborts.extend(consistency_aborts(sources, current_skill))
    return Collected(
        sources=tuple(sources),
        lists=lists,
        aborts=tuple(aborts),
        activities=activities,
        duplicate_names=duplicate_names,
    )


# --- criteria ------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionResult:
    id: str
    gate: str
    passed: bool
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.gate == OPERATOR:
            return OPERATOR_STATUS
        return PASS if self.passed else FAIL

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "gate": self.gate,
            "status": self.status,
            "failures": list(self.failures),
            "notes": list(self.notes),
        }


class _Tally:
    """A criterion's failures and notes as it runs."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, text: str) -> None:
        self.failures.append(text)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def tip(self, source: ScoredSource, title: str) -> ScoredTip | None:
        """The tip in chapter ``title``: the lowest id (noted when there are several);
        an empty window is a failure."""

        found = source.chapter_tips.get(title, ())
        if not found:
            self.fail(f"{source.ref}: no tip in the chapter window {title!r}")
            return None
        if len(found) > 1:
            ids = ", ".join(f"#{tip.id}" for tip in found)
            self.note(
                f"{source.ref}: the window {title!r} holds {len(found)} tips ({ids}); "
                f"scored #{found[0].id}, the lowest id"
            )
        return found[0]


def _shown(tip: ScoredTip) -> str:
    return f"#{tip.id} {tip.name!r}"


def _list_text(ids: Sequence[int]) -> str:
    return "[" + ", ".join(map(str, ids)) + "]"


def expectation_misses(
    tip: ScoredTip, expect: Expect, duplicate_names: Mapping[int, str] | None = None
) -> list[str]:
    """How ``tip`` misses the bucket, audience, list and duplicate parts of ``expect``;
    ``duplicate_names`` names the tips ``duplicate_of`` may point at
    (:attr:`Evidence.duplicate_names`)."""

    misses: list[str] = []
    if expect.bucket is not None and tip.bucket != expect.bucket:
        misses.append(f"is in bucket {tip.bucket}, expected {expect.bucket}")
    if expect.not_bucket is not None and tip.bucket == expect.not_bucket:
        misses.append(f"is in bucket {tip.bucket}, expected any bucket but {expect.not_bucket}")
    if expect.bucket_any_of is not None and tip.bucket not in expect.bucket_any_of:
        misses.append(
            f"is in bucket {tip.bucket}, expected one of {_list_text(expect.bucket_any_of)}"
        )
    if expect.duplicate_of_name is not None:
        wanted = repr(expect.duplicate_of_name)
        if tip.duplicate_of is None:
            misses.append(f"has no duplicate_of, expected a duplicate of {wanted}")
        else:
            target = (duplicate_names or {}).get(tip.duplicate_of)
            if target != expect.duplicate_of_name:
                found = "no stored tip" if target is None else repr(target)
                misses.append(
                    f"has duplicate_of {tip.duplicate_of} ({found}), expected a duplicate of "
                    f"{wanted}"
                )
    if expect.audience is not None and tip.audience != expect.audience:
        misses.append(f"is {tip.audience}, expected {expect.audience}")
    placed = set(tip.list_ids)
    if expect.on_lists is not None:
        if not expect.on_lists and placed:
            misses.append(f"is on lists {_list_text(tip.list_ids)}, expected no list")
        absent = [list_id for list_id in expect.on_lists if list_id not in placed]
        if absent:
            misses.append(f"is on lists {_list_text(tip.list_ids)}, not on {_list_text(absent)}")
    if expect.on_any_of is not None and not placed & set(expect.on_any_of):
        misses.append(
            f"is on lists {_list_text(tip.list_ids)}, none of {_list_text(expect.on_any_of)}"
        )
    return misses


def _is1(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        if source.ref_count != 1:
            tally.fail(f"{source.ref} is stored {source.ref_count} times, expected once")
        if source.row.reviewed_at is None:
            tally.fail(f"{source.ref} is not reviewed")
        if source.row.mode != SOURCE_MODE_EXTRACT:
            tally.fail(f"{source.ref} has mode {source.row.mode!r}, expected {SOURCE_MODE_EXTRACT}")


def tip_count_failure(source: ScoredSource) -> str | None:
    """IS-2 and OOS-1: why the video holds too few or too many tips, or None."""

    count = len(source.tips)
    if VIDEO_TIPS_MIN <= count <= VIDEO_TIPS_MAX:
        return None
    return f"{source.ref} holds {count} tips, expected {VIDEO_TIPS_MIN} to {VIDEO_TIPS_MAX}"


def empty_windows(source: ScoredSource) -> list[str]:
    """The chapter titles whose window holds no tip, in packet order."""

    return [title for title, tips in source.chapter_tips.items() if not tips]


def _is2(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        count = tip_count_failure(source)
        if count is not None:
            tally.fail(count)
        empty = empty_windows(source)
        if empty:
            covered = len(source.chapter_tips) - len(empty)
            tally.fail(
                f"{source.ref}: {covered}/{len(source.chapter_tips)} chapter windows hold a tip; "
                f"empty: {'; '.join(empty)}"
            )


def _is3(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        for chapter in source.label.chapters:
            if not chapter.expect.exact_name:
                continue
            tip = tally.tip(source, chapter.title)
            if tip is not None and tip.name != chapter.title:
                tally.fail(
                    f"{source.ref}: the tip in {chapter.title!r} is {_shown(tip)}, "
                    "not named with the exact chapter title"
                )


def _is4(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        for chapter in source.label.chapters:
            if not chapter.expect.placement:
                continue
            tip = tally.tip(source, chapter.title)
            if tip is None:
                continue
            misses = expectation_misses(tip, chapter.expect, evidence.duplicate_names)
            if misses:
                tally.fail(
                    f"{source.ref}: the tip in {chapter.title!r} ({_shown(tip)}) "
                    + "; ".join(misses)
                )


def _pair_tips(
    evidence: Evidence, pair: DuplicatePair, tally: _Tally | None = None
) -> tuple[ScoredTip, ScoredTip] | None:
    """The pair's two tips as (earlier, later) by id, or None when a window is empty."""

    found: list[ScoredTip] = []
    for chapter in (pair.a, pair.b):
        source = evidence.sources[chapter.ref]
        if tally is not None:
            tip = tally.tip(source, chapter.title)
        else:
            window = source.chapter_tips.get(chapter.title, ())
            tip = window[0] if window else None
        if tip is not None:
            found.append(tip)
    if len(found) != 2:
        return None
    earlier, later = sorted(found, key=lambda tip: tip.id)
    return earlier, later


def _is5(evidence: Evidence, tally: _Tally) -> None:
    for pair in evidence.labels.duplicate_pairs:
        tips = _pair_tips(evidence, pair, tally)
        if tips is None:
            continue
        earlier, later = tips
        tally.note(f"earlier: {_shown(earlier)}; later: {_shown(later)}")
        if earlier.bucket != BUCKET_DAILY:
            tally.fail(
                f"the earlier {_shown(earlier)} is in bucket {earlier.bucket}, "
                f"expected {BUCKET_DAILY}"
            )
        if pair.list_if_first not in earlier.list_ids:
            tally.fail(
                f"the earlier {_shown(earlier)} is on lists {_list_text(earlier.list_ids)}, "
                f"not on {pair.list_if_first}"
            )
        if later.bucket != BUCKET_LEFT_OUT:
            tally.fail(
                f"the later {_shown(later)} is in bucket {later.bucket}, expected {BUCKET_LEFT_OUT}"
            )
        if later.duplicate_of != earlier.id:
            tally.fail(
                f"the later {_shown(later)} has duplicate_of {later.duplicate_of}, "
                f"expected {earlier.id}"
            )


def _is6(evidence: Evidence, tally: _Tally) -> None:
    allowed: set[int] = set()
    for pair in evidence.labels.duplicate_pairs:
        tips = _pair_tips(evidence, pair)
        if tips is not None:
            allowed.add(tips[1].id)
    for source in evidence.sources.values():
        for tip in source.tips:
            if tip.duplicate_of is not None and tip.id not in allowed:
                tally.fail(
                    f"{source.ref}: {_shown(tip)} has duplicate_of {tip.duplicate_of}; only the "
                    "later tip of a labelled duplicate pair may"
                )
            elif tip.possible_duplicate_of is not None:
                tally.note(f"{_shown(tip)} has possible_duplicate_of {tip.possible_duplicate_of}")


def limit_problems(tip: ScoredTip, duration_s: int | None) -> Iterator[str]:
    """Each ``limits.py`` limit the stored ``tip`` breaks, checked by the rules
    ``propose`` applies (``proposal.py``): every text field against
    :data:`proposal.TIP_TEXT_LIMITS` (a required one must be there), the printed line
    against ``LINE_MAX``, ``start_s`` against ``START_S_MAX`` and the packet's
    ``duration_s``, the bucket against ``BUCKET_IDS``, the audience against
    ``AUDIENCES_TIP`` and each duplicate reference against ``ID_MAX``. (A list id outside
    ``ID_MAX`` names no list, so the placement rules already refuse it.)"""

    for name, limit in TIP_TEXT_LIMITS.items():
        value: str | None = getattr(tip, name)
        optional = name in TIP_OPTIONAL
        if value is None:
            problem = None if optional else "is missing"
        else:
            problem = text_problem(value, limit, optional=optional)
        if problem is not None:
            yield f"its {name} {problem}"
    line = line_problem(tip.name, tip.action)
    if line is not None:
        yield line
    if tip.start_s is not None:
        start = start_problem(tip.start_s, duration_s)
        if start is not None:
            yield f"its start_s {start}"
    if not is_bucket(tip.bucket):
        yield f"its bucket is {tip.bucket}; it {NOT_A_BUCKET}"
    if not is_tip_audience(tip.audience):
        yield f"its audience is {tip.audience!r}; it {NOT_A_TIP_AUDIENCE}"
    for name, reference in (
        ("duplicate_of", tip.duplicate_of),
        ("possible_duplicate_of", tip.possible_duplicate_of),
    ):
        if reference is not None and not is_id(reference):
            yield f"its {name} is {reference}, not a tip id (1 to ID_MAX, {ID_MAX})"


def invariant_failures(
    source: ScoredSource, lists: Mapping[int, list_store.ListRow]
) -> Iterator[str]:
    """IS-7: kids lists hold only kids+ tips and list items point only at bucket-1 tips
    (checked by :func:`placement.placement_problems`, the rules ``propose`` and the API
    apply), and every ``limits.py`` limit of a stored tip field holds
    (:func:`limit_problems`)."""

    for tip in source.tips:
        placed = placement_problems(
            bucket=tip.bucket, audience=tip.audience, list_ids=tip.list_ids, lists=lists
        )
        yield from (f"{_shown(tip)}: {problem}" for problem in placed)
        broken = limit_problems(tip, source.packet.duration_s)
        yield from (f"{_shown(tip)}: {problem}" for problem in broken)


def _is7(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        for failure in invariant_failures(source, evidence.lists):
            tally.fail(f"{source.ref}: {failure}")


def filler_failures(source: ScoredSource) -> Iterator[str]:
    """IS-8: tips whose name or action matches ``/subscribe/i``."""

    for tip in source.tips:
        for name, value in (("name", tip.name), ("action", tip.action)):
            if FILLER_PATTERN.search(value):
                yield f"{_shown(tip)}: its {name} {value!r} matches /subscribe/i"


def _is8(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        for failure in filler_failures(source):
            tally.fail(f"{source.ref}: {failure}")


def _is_soft(evidence: Evidence, tally: _Tally) -> None:
    for group in evidence.labels.soft:
        source = evidence.sources[group.ref]
        meeting = 0
        for title in group.titles:
            window = source.chapter_tips.get(title, ())
            if not window:
                tally.note(f"{group.ref}: no tip in the chapter window {title!r}")
                continue
            tip = window[0]
            misses = expectation_misses(tip, group.expect, evidence.duplicate_names)
            if group.expect.exact_name and tip.name != title:
                misses.append("is not named with the exact chapter title")
            if misses:
                tally.note(f"{group.ref}: the tip in {title!r} ({_shown(tip)}) {'; '.join(misses)}")
            else:
                meeting += 1
        if meeting < group.at_least:
            tally.fail(
                f"{group.ref}: {meeting} of {len(group.titles)} chapters "
                f"({'; '.join(group.titles)}) meet the expectation, expected at least "
                f"{group.at_least}"
            )


def graded_sources(evidence: Evidence) -> Iterator[ScoredSource]:
    """The scored sources the per-source criteria grade: neither references nor anchors."""

    return (source for source in evidence.sources.values() if source.label.graded)


def _oos1(evidence: Evidence, tally: _Tally) -> None:
    for source in graded_sources(evidence):
        count = tip_count_failure(source)
        if count is not None:
            tally.fail(count)
        windows = len(source.chapter_tips)
        empty = empty_windows(source)
        covered = windows - len(empty)
        if not windows or Fraction(covered, windows) < OOS_COVERAGE_MIN:
            tally.fail(
                f"{source.ref}: {covered}/{windows} chapter windows hold a tip, expected at least "
                f"{OOS_COVERAGE_MIN.numerator}/{OOS_COVERAGE_MIN.denominator}; empty: "
                f"{'; '.join(empty)}"
            )
        elif empty:
            tally.note(f"{source.ref}: {covered}/{windows} chapter windows hold a tip")
        for failure in invariant_failures(source, evidence.lists):
            tally.fail(f"{source.ref}: IS-7: {failure}")
        for failure in filler_failures(source):
            tally.fail(f"{source.ref}: IS-8: {failure}")


def print_miss(
    tip: ScoredTip, audience: str, activities: printing.ActivitiesSheet | None
) -> str | None:
    """OOS-2: why ``tip`` is not a row of the activities sheet's ``audience`` group, or
    None. A printed row is the tip's name and action (:class:`printing.PrintItem`)."""

    if activities is None:
        return "has no activities print payload to appear in"
    group = next((group for group in activities.groups if group.audience == audience), None)
    if group is None:
        return f"cannot appear in the activities sheet: it has no {audience} group"
    if printing.PrintItem(tip.name, tip.action) not in group.items:
        return f"is not in the {audience} group of the activities print payload"
    return None


def _oos2(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        for chapter in source.label.chapters:
            expect = chapter.expect
            if not expect.placement:
                continue
            tip = tally.tip(source, chapter.title)
            if tip is None:
                continue
            misses = expectation_misses(tip, expect, evidence.duplicate_names)
            if expect.bucket == printing.ACTIVITIES_BUCKET:
                printed = print_miss(tip, expect.audience or tip.audience, evidence.activities)
                if printed is not None:
                    misses.append(printed)
            if misses:
                tally.fail(
                    f"{source.ref}: the tip in {chapter.title!r} ({_shown(tip)}) "
                    + "; ".join(misses)
                )


def _oos3(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        if not source.label.harm_titled:
            continue
        for title, window in source.chapter_tips.items():
            for tip in window:
                if _loose(tip.name) == _loose(title):
                    tally.fail(
                        f"{source.ref}: {_shown(tip)} in the window {title!r} is named with the "
                        "harm the chapter title names"
                    )
        tally.note(
            f"{source.ref}: part 2 is the operator's (M4): every tip's action in these windows "
            "states the healthy behavior"
        )


def _oos4(evidence: Evidence, tally: _Tally) -> None:
    for pair in evidence.labels.not_duplicate_pairs:
        a = tally.tip(evidence.sources[pair.a.ref], pair.a.title)
        b = tally.tip(evidence.sources[pair.b.ref], pair.b.title)
        if a is None or b is None:
            continue
        for one, chapter, other, other_chapter in ((a, pair.a, b, pair.b), (b, pair.b, a, pair.a)):
            if one.duplicate_of == other.id:
                tally.fail(
                    f"{chapter.ref}: the tip in {chapter.title!r} ({_shown(one)}) has "
                    f"duplicate_of {other.id}, the tip in {other_chapter.title!r} of "
                    f"{other_chapter.ref}: a false duplicate"
                )


def _oos5(evidence: Evidence, tally: _Tally) -> None:
    for source in evidence.sources.values():
        anchor = source.label.anchor
        if anchor is None:
            continue
        if len(source.tips) != anchor.expect_tips:
            tips = "; ".join(f"{_shown(tip)} in bucket {tip.bucket}" for tip in source.tips)
            tally.fail(
                f"{source.ref} (the anchor {anchor.path.name}) holds {len(source.tips)} tips, "
                f"expected {anchor.expect_tips}{': ' + tips if tips else ''}"
            )


def _inv1(evidence: Evidence, tally: _Tally) -> None:
    for source in graded_sources(evidence):
        if source.row.kind != SOURCE_KIND_WIKI:
            tally.fail(f"{source.ref} is a {source.row.kind} source, expected {SOURCE_KIND_WIKI}")
        if source.row.mode != SOURCE_MODE_INVESTIGATE:
            tally.fail(
                f"{source.ref} has mode {source.row.mode!r}, expected {SOURCE_MODE_INVESTIGATE}"
            )


def chapter_headings(packet: packet_module.Packet) -> tuple[str, ...]:
    """INV-2: the packet's section headings that name a book chapter ("Ch.N"), in order."""

    return tuple(
        section.heading for section in packet.sections if CHAPTER_HEADING.match(section.heading)
    )


def _inv2(evidence: Evidence, tally: _Tally) -> None:
    for source in graded_sources(evidence):
        headings = chapter_headings(source.packet)
        if len(headings) != CHAPTER_HEADINGS:
            tally.fail(
                f"{source.ref}: the packet holds {len(headings)} 'Ch.N' headings, not the "
                f"{CHAPTER_HEADINGS} INV-2 counts"
            )
            continue
        located = {tip.locator for tip in source.tips}
        missing = [heading for heading in headings if heading not in located]
        covered = len(headings) - len(missing)
        if covered < CHAPTER_HEADINGS_MIN:
            tally.fail(
                f"{source.ref}: {covered} of the {CHAPTER_HEADINGS} 'Ch.N' headings have a tip "
                f"whose locator equals the heading, expected at least {CHAPTER_HEADINGS_MIN}; "
                f"none for: {'; '.join(missing)}"
            )
        elif missing:
            tally.note(f"{source.ref}: no tip for: {'; '.join(missing)}")


def _inv3(evidence: Evidence, tally: _Tally) -> None:
    for source in graded_sources(evidence):
        for tip in source.tips:
            if tip.evidence_url is None:
                tally.fail(f"{source.ref}: {_shown(tip)} has no evidence_url")
            elif not is_http_url(tip.evidence_url):
                tally.fail(
                    f"{source.ref}: {_shown(tip)} cites {tip.evidence_url!r}, not an http(s) URL"
                )


def _inv4(evidence: Evidence, tally: _Tally) -> None:
    for source in graded_sources(evidence):
        total = len(source.tips)
        elsewhere = sum(
            1
            for tip in source.tips
            if tip.evidence_url is not None and not is_wikipedia_url(tip.evidence_url)
        )
        shown = f"{elsewhere} of {total} tips cite a host outside {WIKIPEDIA_DOMAIN}"
        if not total or Fraction(elsewhere, total) < ELSEWHERE_MIN:
            tally.fail(f"{source.ref}: {shown}, expected at least half")
        else:
            tally.note(f"{source.ref}: {shown}")


def _inv6(evidence: Evidence, tally: _Tally) -> None:
    checks = spot_checks(evidence)
    for source in evidence.sources.values():
        if source.row.mode != SOURCE_MODE_INVESTIGATE:
            continue
        count = sum(1 for check in checks if check.ref == source.ref)
        tally.note(
            f"{source.ref}: open the {count} spot-check URLs printed below; at least "
            f"{SPOT_CHECK_SUPPORTED_MIN} of the {SPOT_CHECK_URLS} must open and support their "
            "tip (the operator's verdict, recorded in the M5 result)"
        )
    if not checks:
        tally.note("no scored source in investigate mode cites a URL, so there is no spot check")


def _lacks_nothing(labels: Labels) -> str | None:
    return None


def _lacks_exact_names(labels: Labels) -> str | None:
    sources = labels.sources
    named = any(chapter.expect.exact_name for source in sources for chapter in source.chapters)
    return None if named else 'needs a labelled chapter with "name": "exact"'


def _lacks_placements(labels: Labels) -> str | None:
    sources = labels.sources
    placed = any(chapter.expect.placement for source in sources for chapter in source.chapters)
    return None if placed else "needs a labelled chapter that expects a bucket, audience or list"


def _lacks_pairs(labels: Labels) -> str | None:
    return None if labels.duplicate_pairs else "needs at least one duplicate pair"


def _lacks_soft(labels: Labels) -> str | None:
    return None if labels.soft else "needs at least one soft expectation group"


def _lacks_graded(labels: Labels) -> str | None:
    graded = any(source.graded for source in labels.sources)
    return None if graded else "needs a labelled source that is neither a reference nor an anchor"


def _lacks_printed_placement(labels: Labels) -> str | None:
    printed = any(
        chapter.expect.bucket == printing.ACTIVITIES_BUCKET and chapter.expect.audience
        for source in labels.sources
        for chapter in source.chapters
    )
    if printed:
        return None
    return (
        f"needs a labelled chapter that expects bucket {printing.ACTIVITIES_BUCKET} (the "
        "activities sheet) and an audience"
    )


def _lacks_harm_titled(labels: Labels) -> str | None:
    harmful = any(source.harm_titled for source in labels.sources)
    return None if harmful else 'needs a labelled source with "harm_titled": true'


def _lacks_not_duplicates(labels: Labels) -> str | None:
    return None if labels.not_duplicate_pairs else "needs at least one not_duplicate_pairs entry"


def _lacks_anchor(labels: Labels) -> str | None:
    anchored = any(source.anchor is not None for source in labels.sources)
    return None if anchored else f"needs an anchor ({ANCHOR_FILE_KEY})"


@dataclass(frozen=True)
class Criterion:
    """A scored criterion: its id, gate and logic, and the label data it needs."""

    id: str
    gate: str
    check: Callable[[Evidence, _Tally], None]
    lacks: Callable[[Labels], str | None] = _lacks_nothing

    def evaluate(self, evidence: Evidence) -> CriterionResult:
        tally = _Tally()
        self.check(evidence, tally)
        return CriterionResult(
            id=self.id,
            gate=self.gate,
            passed=not tally.failures,
            failures=tuple(tally.failures),
            notes=tuple(tally.notes),
        )


CRITERIA: Final[Mapping[str, Criterion]] = {
    criterion.id: criterion
    for criterion in (
        Criterion("IS-1", HARD, _is1),
        Criterion("IS-2", HARD, _is2),
        Criterion("IS-3", HARD, _is3, _lacks_exact_names),
        Criterion("IS-4", HARD, _is4, _lacks_placements),
        Criterion("IS-5", HARD, _is5, _lacks_pairs),
        Criterion("IS-6", HARD, _is6),
        Criterion("IS-7", HARD, _is7),
        Criterion("IS-8", HARD, _is8),
        Criterion("IS-S", SOFT, _is_soft, _lacks_soft),
        Criterion("OOS-1", HARD, _oos1, _lacks_graded),
        Criterion("OOS-2", HARD, _oos2, _lacks_printed_placement),
        Criterion("OOS-3", HARD, _oos3, _lacks_harm_titled),
        Criterion("OOS-4", HARD, _oos4, _lacks_not_duplicates),
        Criterion("OOS-5", HARD, _oos5, _lacks_anchor),
        Criterion("OOS-S", SOFT, _is_soft, _lacks_soft),
        Criterion("INV-1", HARD, _inv1, _lacks_graded),
        Criterion("INV-2", HARD, _inv2, _lacks_graded),
        Criterion("INV-3", HARD, _inv3, _lacks_graded),
        Criterion("INV-4", HARD, _inv4, _lacks_graded),
        Criterion("INV-5", HARD, _is7),
        Criterion("INV-6", OPERATOR, _inv6),
    )
}
"""Every criterion the scorer knows, by id. IS-9 is the operator's verdict and is never
listed; INV-6 is the operator's verdict on the spot check, listed so the scorer prints
it (gate :data:`OPERATOR`, never scored). OOS-S evaluates the soft groups as IS-S does,
and INV-5 holds the IS-7 invariants with IS-7's own check (plan §4.8)."""


def evaluate(evidence: Evidence) -> tuple[CriterionResult, ...]:
    """Each criterion the labels name, in their order."""

    return tuple(CRITERIA[name].evaluate(evidence) for name in evidence.labels.criteria)


# --- the report ----------------------------------------------------------------------


def printable(text: str) -> str:
    """``text`` as the report prints it: each character :meth:`str.isprintable` refuses
    (a line break, a tab, ESC and every other control code, a line or paragraph
    separator, a bidirectional override) is written as the escape :func:`repr` gives it,
    such as ``\\x1b`` or ``\\n``. An untrusted string therefore stays inside its own
    line and can neither start a new one nor drive the terminal; printable text is
    unchanged.

    A report line keeps the whole string and shows what was there: it escapes such
    characters, never drops them, and never shortens the text.
    """

    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


@dataclass(frozen=True)
class SourceSummary:
    ref: str
    tips: int
    host: str
    model: str
    skill_sha256: str
    packet_sha256: str
    buckets: tuple[tuple[int, int], ...] = ()
    """Each bucket of ``BUCKET_IDS`` and its tip count: reported, never gated."""
    degraded: bool = False
    """The archived proposal says ``"degraded": true``."""

    def spread(self) -> str:
        """The tips per bucket, such as ``1: 3, 2: 2, 3: 4, 4: 1``."""

        return ", ".join(f"{bucket}: {count}" for bucket, count in self.buckets)

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "tips": self.tips,
            "buckets": {str(bucket): count for bucket, count in self.buckets},
            "host": self.host,
            "model": self.model,
            "skill_sha256": self.skill_sha256,
            "packet_sha256": self.packet_sha256,
            "degraded": self.degraded,
        }


@dataclass(frozen=True)
class SpotCheck:
    """INV-6: one evidence URL of an investigate source for the operator to open."""

    ref: str
    url: str
    sha256: str
    """Of the URL's UTF-8 bytes: the spot check takes the URLs whose sha256 is lowest."""
    tip_id: int
    """The lowest id among the tips that cite the URL."""
    tip_name: str
    cited_by: int
    """How many of the source's tips cite the URL."""

    def line(self) -> str:
        more = f" and {self.cited_by - 1} more" if self.cited_by > 1 else ""
        return (
            f"spot check: {self.ref}: {self.url} (sha256 {self.sha256}; tip #{self.tip_id} "
            f"{self.tip_name!r}{more})"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "url": self.url,
            "sha256": self.sha256,
            "tip_id": self.tip_id,
            "tip_name": self.tip_name,
            "cited_by": self.cited_by,
        }


def url_sha256(url: str) -> str:
    """The sha256 of ``url``'s UTF-8 bytes, as lowercase hex (the spot check's order)."""

    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def spot_checks(evidence: Evidence) -> tuple[SpotCheck, ...]:
    """INV-6: for each scored source in investigate mode, the :data:`SPOT_CHECK_URLS`
    distinct evidence URLs whose sha256 (:func:`url_sha256`) is lowest, lowest first;
    deterministic, so every run of the same rows prints the same URLs. Each URL is
    shown with the lowest-id tip that cites it."""

    checks: list[SpotCheck] = []
    for source in evidence.sources.values():
        if source.row.mode != SOURCE_MODE_INVESTIGATE:
            continue
        citing: dict[str, list[ScoredTip]] = {}
        for tip in source.tips:  # by id
            if tip.evidence_url is not None:
                citing.setdefault(tip.evidence_url, []).append(tip)
        ranked = sorted(citing, key=lambda url: (url_sha256(url), url))[:SPOT_CHECK_URLS]
        checks.extend(
            SpotCheck(
                ref=source.ref,
                url=url,
                sha256=url_sha256(url),
                tip_id=citing[url][0].id,
                tip_name=citing[url][0].name,
                cited_by=len(citing[url]),
            )
            for url in ranked
        )
    return tuple(checks)


@dataclass(frozen=True)
class ScoreReport:
    """One ``eval score`` result; :meth:`to_json` is the score file's content."""

    scored_at: str
    data_dir: Path
    labels: Labels
    skill_sha256: str
    """The current skill fingerprint."""
    sources: tuple[SourceSummary, ...]
    aborts: tuple[Abort, ...]
    criteria: tuple[CriterionResult, ...]
    path: Path | None = None
    """Where the score file was written."""
    spot_checks: tuple[SpotCheck, ...] = ()

    @property
    def degraded_sources(self) -> tuple[str, ...]:
        """The refs whose archived proposal says ``"degraded": true`` (each source's own
        flag is its summary's ``degraded``)."""

        return tuple(source.ref for source in self.sources if source.degraded)

    @property
    def verdict(self) -> str:
        """``ABORT`` when any abort holds; else ``REPORTED (degraded)`` when a proposal is
        degraded, whatever the criteria say; else ``FAIL`` or ``PASS``."""

        if self.aborts:
            return ABORT
        if self.degraded_sources:
            return REPORTED_DEGRADED
        return FAIL if self.failed else PASS

    @property
    def failed(self) -> tuple[str, ...]:
        """The ids of the failed hard criteria."""

        return tuple(c.id for c in self.criteria if c.gate == HARD and not c.passed)

    @property
    def soft_failed(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.criteria if c.gate == SOFT and not c.passed)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(sorted({source.model for source in self.sources}))

    @property
    def exit_code(self) -> int:
        exits = {PASS: EXIT_OK, REPORTED_DEGRADED: EXIT_OK, FAIL: EXIT_EVAL_FAIL}
        return exits.get(self.verdict, EXIT_EVAL_ABORT)

    def abort_message(self) -> str:
        """The one-line ``CC_EVAL_ABORT`` message of an aborted run (:func:`printable`)."""

        count = len(self.aborts)
        summary = "; ".join(printable(abort.line()) for abort in self.aborts)
        return f"{count} abort condition{'' if count == 1 else 's'}, no verdict: {summary}"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCORE_SCHEMA,
            "scored_at": self.scored_at,
            "verdict": self.verdict,
            "failed": list(self.failed),
            "soft_failed": list(self.soft_failed),
            "data_dir": str(self.data_dir),
            "labels": str(self.labels.path),
            "labels_sha256": self.labels.sha256,
            "claim": self.labels.claim,
            "skill_sha256": self.skill_sha256,
            "models": list(self.models),
            "mixed_models": len(self.models) > 1,
            "degraded_sources": list(self.degraded_sources),
            "sources": [source.to_json() for source in self.sources],
            "aborts": [{"rule": abort.rule, "detail": abort.detail} for abort in self.aborts],
            "criteria": [criterion.to_json() for criterion in self.criteria],
            "spot_checks": [check.to_json() for check in self.spot_checks],
        }

    def lines(self) -> list[str]:
        """The printed table, each line through :func:`printable`."""

        lines = [
            f"labels: {self.labels.path} (sha256 {self.labels.sha256})",
            f"claim: {self.labels.claim}",
        ]
        for source in self.sources:
            spread = f"; buckets {source.spread()}" if source.buckets else ""
            lines.append(
                f"source: {source.ref}: {source.tips} tips; host {source.host}; "
                f"model {source.model}{spread}"
            )
        if len(self.models) > 1:
            lines.append(f"models: mixed ({', '.join(self.models)}); reported, not aborted")
        if self.degraded_sources:
            lines.append(
                f"degraded: {', '.join(self.degraded_sources)}: the proposal says degraded: "
                f"true, so the criteria are reported and the verdict is {REPORTED_DEGRADED}, "
                "never a pass"
            )
        for abort in self.aborts:
            lines.append(f"abort: {abort.line()}")
        width = max((len(c.id) for c in self.criteria), default=0)
        for criterion in self.criteria:
            status = criterion.status
            if criterion.gate == SOFT:
                status += " (reported, not gated)"
            elif criterion.gate == OPERATOR:
                status += " (the operator's verdict, not scored)"
            lines.append(f"{criterion.id:<{width}}  {criterion.gate}  {status}")
            lines.extend(f"    fail: {failure}" for failure in criterion.failures)
            lines.extend(f"    note: {note}" for note in criterion.notes)
        lines.extend(check.line() for check in self.spot_checks)
        if self.failed:
            lines.append(f"failed: {', '.join(self.failed)}")
        lines.append(f"verdict: {self.verdict}")
        if self.path is not None:
            lines.append(f"score: {self.path}")
        return [printable(line) for line in lines]


def write_report(data_dir: Path, report: ScoreReport, moment: datetime) -> Path:
    """Write ``eval/score-<stamp>.json`` (``-2``, ``-3`` … when that name is taken) as
    UTF-8 with LF line endings, atomically and never overwriting a file; returns its path.

    The bytes go to a sibling temporary file first (:func:`paths.sibling_path`, as
    ``write_packet`` does), so a reader never sees a partial
    score file. The finished file is then published under the first free name of
    :func:`paths.numbered_paths` with :func:`os.link` (a hard link, as ``PacketSwap``
    uses), which, unlike :func:`os.replace`, fails when the name is taken, so no score
    file is ever replaced; the temporary name is removed either way. On a file system
    without hard links (:func:`paths.lacks_hard_links`, as ``PacketSwap`` tells), the
    name is claimed with an exclusive create instead, which also fails when it is taken,
    and the finished file is renamed over that empty claim (:func:`_publish_unlinked`).
    A failed write is a :class:`ConfigError` (inside ``eval score``, an abort) and
    leaves no file under a score name, and so is an ``eval`` folder that cannot be made
    (a file in its place, a data dir that cannot be written).
    """

    directory = data_dir / paths.EVAL_DIR
    data = (json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    stem = f"{SCORE_FILE_PREFIX}{paths.file_stamp(moment)}"
    temporary = sibling_path(directory / f"{stem}.json", ".tmp")
    try:
        directory.mkdir(exist_ok=True)
        temporary.write_bytes(data)
        names = numbered_paths(directory, stem, ".json")
        while True:
            target = next(names)
            try:
                os.link(temporary, target)
            except FileExistsError:
                continue
            except OSError as error:
                if not lacks_hard_links(error):
                    raise
                try:
                    _publish_unlinked(temporary, target)
                except FileExistsError:
                    continue
            return target
    except OSError as error:
        raise ConfigError(f"cannot write the score file in {directory}: {error}") from error
    finally:
        with suppress(OSError):  # a leftover temporary name is harmless; a lost result is not
            temporary.unlink(missing_ok=True)


def _publish_unlinked(temporary: Path, target: Path) -> None:
    """Publish the finished ``temporary`` file at ``target`` without a hard link: claim
    the name with an exclusive create (``FileExistsError`` when it is taken, so no score
    file is replaced), then rename the file over the empty claim, so the content still
    appears whole; a reader may briefly find the name empty. A failed rename removes the
    claim."""

    target.open("xb").close()
    try:
        os.replace(temporary, target)
    except BaseException:
        with suppress(OSError):
            target.unlink()
        raise


def _summaries(sources: Sequence[ScoredSource]) -> tuple[SourceSummary, ...]:
    return tuple(
        SourceSummary(
            ref=source.ref,
            tips=len(source.tips),
            host=source.archived.host,
            model=source.archived.model,
            skill_sha256=source.archived.skill_sha256,
            packet_sha256=source.packet_sha256,
            buckets=tuple(
                (bucket, sum(1 for tip in source.tips if tip.bucket == bucket))
                for bucket in BUCKET_IDS
            ),
            degraded=source.archived.degraded,
        )
        for source in sources
    )


def data_dir_abort(error: ConfigError) -> EvalAbortError:
    """A data dir that cannot be used (an empty ``--data-dir`` or environment value, a
    directory inside a git checkout) as the abort it is inside ``eval score``: no
    verdict, exit 6, where every other command exits 2."""

    return EvalAbortError(f"{ABORT_DATA_DIR_UNUSABLE}: {error.line()}")


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open the existing database at ``path`` for reading only (``mode=ro``).

    The connection has the settings every handle shares
    (:func:`connection.open_connection`) and the writer's busy timeout
    (:data:`connection.BUSY_TIMEOUT_PRAGMA`, which lasts as long as the connection). It
    never creates the file, sets no persistent pragma and applies no schema: a database
    whose ``user_version`` is not :data:`connection.SCHEMA_VERSION` (an empty or foreign
    file included) is a :class:`ConfigError`, as is a file that is not a SQLite database
    or cannot be opened. On a WAL database whose ``-wal`` and ``-shm`` files are
    missing, SQLite creates them (an empty ``-wal``); a read-only connection cannot
    remove them, and the next writable one does. The database file is never written.
    """

    uri = f"{Path(os.path.abspath(path)).as_uri()}?mode=ro"
    try:
        conn = open_connection(uri, uri=True)
    except sqlite3.Error as error:
        raise ConfigError(f"cannot open the database {path} read-only: {error}") from error
    try:
        conn.execute(BUSY_TIMEOUT_PRAGMA)
        version = connection.schema_version(conn)
    except sqlite3.DatabaseError as error:
        conn.close()
        raise ConfigError(f"{path} is not a readable SQLite database: {error}") from error
    except BaseException:
        conn.close()
        raise
    if version != connection.SCHEMA_VERSION:
        conn.close()
        raise ConfigError(
            f"database {path} has schema version {version}; this checklist-care reads only "
            f"version {connection.SCHEMA_VERSION}"
        )
    return conn


def run(data_dir: paths.DataDir, labels_path: Path) -> ScoreReport:
    """Score the data dir's database against the labels file and write the score file.

    Raises :class:`EvalAbortError` (exit 6, nothing written) when the data dir is not
    explicit, the labels are unusable or there is no readable database. Any later abort
    is the report's ``ABORT`` verdict. The database is opened read-only
    (:func:`connect_read_only`): scoring never changes what it scores.
    """

    if not data_dir.explicit:
        raise EvalAbortError(
            f"{ABORT_DATA_DIR}: eval score scores only a data dir given with --data-dir or "
            f"{paths.DATA_DIR_ENV}, never the default {data_dir.path}"
        )
    labels = load_labels(Path(os.path.abspath(labels_path)))
    db_path = data_dir.db_path
    if not data_dir.has_database():
        raise EvalAbortError(f"{ABORT_DATABASE}: no database at {db_path}")
    moment = connection.utc_now()
    current_skill = context.skill_sha256()
    try:
        conn = connect_read_only(db_path)
    except ConfigError as error:
        raise EvalAbortError(f"{ABORT_DATABASE}: {error.message}") from error
    with closing(conn):
        try:
            collected = collect(conn, data_dir.path, labels, current_skill)
        except sqlite3.DatabaseError as error:
            raise EvalAbortError(f"{ABORT_DATABASE}: cannot read {db_path}: {error}") from error
    evidence = collected.evidence(labels)
    report = ScoreReport(
        scored_at=connection.format_timestamp(moment),
        data_dir=data_dir.path,
        labels=labels,
        skill_sha256=current_skill,
        sources=_summaries(collected.sources),
        aborts=collected.aborts,
        criteria=evaluate(evidence) if evidence is not None else (),
        spot_checks=spot_checks(evidence) if evidence is not None else (),
    )
    return replace(report, path=write_report(data_dir.path, report, moment))
