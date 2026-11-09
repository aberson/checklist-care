"""``proposal.validate`` and ``proposal.read_file`` field by field (plan §4.5).

The boundary variants of each validation rule, run in-process against one
prepared data directory: a database seeded by the production seed and the
fixture packet stored as fetched. ``tests/integration/test_cli_propose.py``
runs the plan Step 3 Done-when cases through ``cli.main``, with the
write-nothing checks.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from checklist_care import context, paths, proposal
from checklist_care.db import connection, seed
from checklist_care.errors import ProposalInvalidError
from checklist_care.limits import (
    ACTION_MAX,
    AUDIENCE_ADULTS,
    AUDIENCE_KIDS,
    BUCKET_DAILY,
    EVIDENCE_URL_MAX,
    ID_MAX,
    LINE_MAX,
    LINE_SEPARATOR,
    LOCATOR_MAX,
    NAME_MAX,
    NOTE_MAX,
    PROPOSAL_FILE_MAX_BYTES,
    REASON_MAX,
    SOURCE_KIND_YOUTUBE,
    SQLITE_INTEGER_MAX,
    START_S_MAX,
    TIPS_MAX_EXTRACT,
    TIPS_MAX_INVESTIGATE,
)
from checklist_care.sources import packet as packet_module
from checklist_care.sources import youtube
from checklist_care.store import sources as source_store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PACKET_FIXTURE = FIXTURES / "packet_youtube_synthetic.json"
PROPOSAL_FIXTURE = FIXTURES / "proposal_valid.json"
REF = "youtube:LpeDuC2sQ-w"
NOW_TEXT = "2026-11-08T12:00:00Z"
URL = "https://example.org/"


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """A seeded database with the fixture video stored as fetched, its packet in place."""

    data_dir = tmp_path_factory.mktemp("data")
    paths.ensure_layout(data_dir)
    target = packet_module.packet_path(data_dir, REF)
    target.write_bytes(PACKET_FIXTURE.read_bytes())
    stored = packet_module.read_packet(target)
    with closing(connection.connect(data_dir / paths.DB_FILENAME)) as conn:
        seed.seed_database(conn, now=NOW_TEXT)
        with connection.transaction(conn):
            source_store.upsert_fetched(
                conn,
                kind=SOURCE_KIND_YOUTUBE,
                ref=REF,
                title=stored.title,
                url=stored.url,
                revision=youtube.revision(stored),
                fetched_at=NOW_TEXT,
            )
        yield conn, data_dir


def valid_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(PROPOSAL_FIXTURE.read_bytes().decode("utf-8"))
    document["skill_sha256"] = context.skill_sha256()
    return document


Mutation = Callable[[dict[str, Any]], None]


def tip(index: int, **values: Any) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        document["tips"][index].update(values)

    return mutate


def drop(index: int, key: str) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        del document["tips"][index][key]

    return mutate


def envelope(**values: Any) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        document.update(values)

    return mutate


def replace_tip(index: int, value: Any) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        document["tips"][index] = value

    return mutate


def tips_copied(count: int) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        document["tips"] = [copy.deepcopy(document["tips"][1]) for _ in range(count)]

    return mutate


REJECTED: list[tuple[str, Mutation, int | None, str]] = [
    # start_s: 0 <= start_s < duration_s (563), a whole number, required with chapters
    ("start_s beyond duration_s", tip(9, start_s=600), 9, "start_s"),
    ("negative start_s", tip(0, start_s=-1), 0, "start_s"),
    ("start_s null for a chaptered video", tip(1, start_s=None), 1, "start_s"),
    ("fractional start_s", tip(1, start_s=60.5), 1, "start_s"),
    ("start_s true", tip(1, start_s=True), 1, "start_s"),
    ("start_s as text", tip(1, start_s="60"), 1, "start_s"),
    ("start_s beyond SQLite INTEGER", tip(9, start_s=SQLITE_INTEGER_MAX + 1), 9, "start_s"),
    # the required one-line texts
    ("over-length action", tip(3, action="A" * (ACTION_MAX + 1)), 3, "action"),
    ("over-length reason", tip(3, reason="R" * (REASON_MAX + 1)), 3, "reason"),
    ("empty action", tip(3, action=""), 3, "action"),
    ("blank reason", tip(3, reason=" "), 3, "reason"),
    ("missing reason", drop(3, "reason"), 3, "reason"),
    ("name not a string", tip(3, name=7), 3, "name"),
    ("tab in a reason", tip(3, reason="One\ttwo"), 3, "reason"),
    ("two-line name", tip(3, name="Two\nLines"), 3, "name"),
    ("line separator in a name", tip(3, name="One\u2028Two"), 3, "name"),
    # the optional texts: null, or one non-blank line within their cap
    ("over-length locator", tip(4, locator="L" * (LOCATOR_MAX + 1)), 4, "locator"),
    ("over-length note", tip(4, note="N" * (NOTE_MAX + 1)), 4, "note"),
    (
        "over-length evidence_url",
        tip(4, evidence_url=URL + "p" * (EVIDENCE_URL_MAX + 1 - len(URL))),
        4,
        "evidence_url",
    ),
    ("two-line note", tip(4, note="first\nsecond"), 4, "note"),
    ("NUL in a locator", tip(4, locator="Chapter\x00One"), 4, "locator"),
    ("escape code in a note", tip(4, note="\x1b[31mred"), 4, "note"),
    ("empty locator", tip(4, locator=""), 4, "locator"),
    ("blank note", tip(4, note="  "), 4, "note"),
    ("locator not a string", tip(4, locator=3), 4, "locator"),
    ("evidence_url with a space", tip(4, evidence_url=URL + "a b"), 4, "evidence_url"),
    ("evidence_url without a host", tip(4, evidence_url="https:///path"), 4, "evidence_url"),
    ("evidence_url not http", tip(4, evidence_url="javascript:alert(1)"), 4, "evidence_url"),
    ("evidence_url a file URL", tip(4, evidence_url="file:///etc/x"), 4, "evidence_url"),
    # bucket, audience, lists and tip references
    ("bucket true", tip(1, bucket=True), 1, "bucket"),
    ("bucket 0", tip(1, bucket=0), 1, "bucket"),
    ("bucket as text", tip(1, bucket="1"), 1, "bucket"),
    ("bucket 5", tip(1, bucket=5), 1, "bucket"),
    ("kids audience on a tip", tip(1, audience=AUDIENCE_KIDS), 1, "audience"),
    ("unknown list", tip(4, list_ids=[99]), 4, "list_ids"),
    ("a list named twice", tip(4, list_ids=[2, 2]), 4, "list_ids"),
    ("list_ids not a list", tip(4, list_ids=2), 4, "list_ids"),
    ("list_ids holding true", tip(4, list_ids=[True]), 4, "list_ids"),
    (
        "unknown possible_duplicate_of",
        tip(5, possible_duplicate_of=999),
        5,
        "possible_duplicate_of",
    ),
    ("duplicate_of zero", tip(2, duplicate_of=0), 2, "duplicate_of"),
    # every whole number is bounded, so none can overflow SQLite INTEGER on insert or query
    ("bucket beyond SQLite INTEGER", tip(1, bucket=SQLITE_INTEGER_MAX + 1), 1, "bucket"),
    ("list id beyond ID_MAX", tip(4, list_ids=[ID_MAX + 1]), 4, "list_ids"),
    ("duplicate_of beyond ID_MAX", tip(2, duplicate_of=ID_MAX + 1), 2, "duplicate_of"),
    (
        "possible_duplicate_of beyond ID_MAX",
        tip(5, possible_duplicate_of=ID_MAX + 1),
        5,
        "possible_duplicate_of",
    ),
    ("a tip that is not an object", replace_tip(4, ["a", "list"]), 4, "tip"),
    ("unknown tip field", tip(4, lists=[2]), 4, "lists"),
    # the envelope
    ("bad schema", envelope(schema="checklist-care.proposal/2"), None, "schema"),
    ("bad mode", envelope(mode="summarize"), None, "mode"),
    ("degraded in extract mode", envelope(degraded=True), None, "degraded"),
    ("unknown source_ref", envelope(source_ref="youtube:EXAMPLE0001"), None, "source_ref"),
    ("the manual source", envelope(source_ref=seed.MANUAL_SOURCE_REF), None, "source_ref"),
    ("too many tips", tips_copied(TIPS_MAX_EXTRACT + 1), None, "tips"),
    ("short packet_sha256", envelope(packet_sha256="ABC"), None, "packet_sha256"),
    ("blank model", envelope(model=" "), None, "model"),
    ("degraded not a boolean", envelope(degraded="no"), None, "degraded"),
    ("tips not a list", envelope(tips={}), None, "tips"),
    ("unknown envelope field", envelope(extra=1), None, "extra"),
    # unpaired surrogates, wherever a string can be
    ("surrogate in an action", tip(0, action="Look \ud800"), 0, "action"),
    ("surrogate in a reason", tip(0, reason="\udbff reason"), 0, "reason"),
    ("surrogate in a locator", tip(4, locator="\udfff"), 4, "locator"),
    ("surrogate in a note", tip(4, note="smile \ud83d"), 4, "note"),
    ("surrogate in an evidence_url", tip(4, evidence_url=URL + "\udc00"), 4, "evidence_url"),
    ("surrogate in the model", envelope(model="model \ud800"), None, "model"),
    ("surrogate in the host", envelope(host="\ud800"), None, "host"),
    ("surrogate inside list_ids", tip(4, list_ids=["\ud800"]), 4, "list_ids"),
    ("surrogate as a whole tip", replace_tip(4, "\ud800"), 4, "tip"),
    ("surrogate in an envelope key", envelope(**{"x\ud800": 1}), None, "x\\ud800"),
]


@pytest.mark.parametrize(
    ("mutate", "index", "field_name"),
    [pytest.param(m, i, f, id=name) for name, m, i, f in REJECTED],
)
def test_a_rejected_field_is_the_one_problem(
    prepared: tuple[sqlite3.Connection, Path],
    mutate: Mutation,
    index: int | None,
    field_name: str,
) -> None:
    conn, data_dir = prepared
    document = valid_document()
    mutate(document)

    with pytest.raises(ProposalInvalidError) as rejected:
        proposal.validate(document, conn, data_dir)

    problems = rejected.value.problems
    assert [(p.index, p.field) for p in problems] == [(index, field_name)], problems
    assert problems[0].problem
    rejected.value.problems_json().encode("utf-8")  # what `propose` prints, strictly


ACCEPTED: list[tuple[str, Mutation]] = [
    ("start_s 0", tip(9, start_s=0)),
    ("start_s one second before the end", tip(9, start_s=562)),
    (
        "name and action filling LINE_MAX",
        tip(3, name="N" * NAME_MAX, action="A" * (LINE_MAX - NAME_MAX - len(LINE_SEPARATOR))),
    ),
    ("reason at REASON_MAX", tip(3, reason="R" * REASON_MAX)),
    ("locator at LOCATOR_MAX", tip(4, locator="L" * LOCATOR_MAX)),
    ("note at NOTE_MAX", tip(4, note="N" * NOTE_MAX)),
    (
        "evidence_url at EVIDENCE_URL_MAX",
        tip(4, evidence_url=URL + "p" * (EVIDENCE_URL_MAX - len(URL))),
    ),
    ("a locator left out", drop(4, "locator")),
    ("an emoji (a supplementary character) in a note", tip(4, note="smile \U0001f600")),
    ("TIPS_MAX_EXTRACT tips", tips_copied(TIPS_MAX_EXTRACT)),
]


@pytest.mark.parametrize("mutate", [pytest.param(m, id=name) for name, m in ACCEPTED])
def test_a_value_at_its_limit_is_accepted(
    prepared: tuple[sqlite3.Connection, Path], mutate: Mutation
) -> None:
    conn, data_dir = prepared
    document = valid_document()
    mutate(document)
    proposal.validate(document, conn, data_dir)


def test_a_surrogate_pair_escape_decodes_to_one_accepted_character(
    prepared: tuple[sqlite3.Connection, Path],
) -> None:
    conn, data_dir = prepared
    document = valid_document()
    document["tips"][4]["note"] = "smile \U0001f600"
    data = json.dumps(document).encode("ascii")
    assert b"\\ud83d\\ude00" in data

    valid = proposal.validate(proposal.decode(data), conn, data_dir)

    assert valid.tips[4].tip.note == "smile \U0001f600"


def test_surrogates_are_reported_before_any_other_problem(
    prepared: tuple[sqlite3.Connection, Path],
) -> None:
    conn, data_dir = prepared
    document = valid_document()
    document["host"] = "chatgpt"  # a problem the later checks would report
    document["tips"][0]["name"] = "\ud800"
    document["tips"][2]["note"] = "\udc00"

    with pytest.raises(ProposalInvalidError) as rejected:
        proposal.validate(document, conn, data_dir)

    assert [(p.index, p.field) for p in rejected.value.problems] == [(0, "name"), (2, "note")]


# --- read_file: the size cap -------------------------------------------------------------


def test_read_file_reads_a_file_at_the_size_cap(tmp_path: Path) -> None:
    target = tmp_path / "proposal.json"
    target.write_bytes(b" " * PROPOSAL_FILE_MAX_BYTES)
    assert len(proposal.read_file(target)) == PROPOSAL_FILE_MAX_BYTES


def test_read_file_rejects_one_byte_over_the_size_cap(tmp_path: Path) -> None:
    target = tmp_path / "proposal.json"
    target.write_bytes(b" " * (PROPOSAL_FILE_MAX_BYTES + 1))
    with pytest.raises(ProposalInvalidError) as rejected:
        proposal.read_file(target)
    assert [(p.index, p.field) for p in rejected.value.problems] == [(None, "file")]


def test_the_size_cap_holds_a_full_proposal_with_every_character_escaped() -> None:
    # The PROPOSAL_FILE_MAX_BYTES claim: TIPS_MAX_INVESTIGATE tips, every text at its
    # limit, every character a six-byte \uXXXX escape, written with indent=2.
    wide = "\u00e9"
    big = {
        "name": wide * NAME_MAX,
        "action": wide * ACTION_MAX,
        "reason": wide * REASON_MAX,
        "bucket": BUCKET_DAILY,
        "audience": AUDIENCE_ADULTS,
        "list_ids": list(range(1, 8)),
        "start_s": START_S_MAX,
        "locator": wide * LOCATOR_MAX,
        "evidence_url": wide * EVIDENCE_URL_MAX,
        "duplicate_of": ID_MAX,
        "possible_duplicate_of": ID_MAX,
        "note": wide * NOTE_MAX,
    }
    document = valid_document()
    document["model"] = wide * 200
    document["tips"] = [big] * TIPS_MAX_INVESTIGATE
    size = len(json.dumps(document, indent=2).encode("ascii"))
    assert size < PROPOSAL_FILE_MAX_BYTES
