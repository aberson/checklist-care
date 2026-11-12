"""The acceptance scorer's rules in ``scoring/score.py`` (plan §4.8), on in-memory inputs.

Chapter-window matching, the labels schema, every ABORT rule and the IS criteria each
get a known-good and a known-garbage case here. ``tests/integration/test_cli_eval.py``
runs the same rules end to end on databases built by ``propose``.
"""

from __future__ import annotations

import codecs
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from checklist_care import context, limits, paths, placement, proposal, strict_json
from checklist_care.db import connection, seed
from checklist_care.errors import EXIT_EVAL_ABORT, EXIT_EVAL_FAIL, EXIT_OK, EvalAbortError
from checklist_care.limits import (
    ACTION_MAX,
    AUDIENCE_KIDS,
    EVIDENCE_URL_MAX,
    HOST_CLAUDE_CODE,
    HOST_CODEX,
    ID_MAX,
    LINE_MAX,
    LINE_SEPARATOR,
    LOCATOR_MAX,
    NAME_MAX,
    NOTE_MAX,
    REASON_MAX,
    SOURCE_KIND_TEXT,
    SOURCE_KIND_WIKI,
    SOURCE_KIND_YOUTUBE,
    SOURCE_MODE_EXTRACT,
    SOURCE_MODE_INVESTIGATE,
    START_S_MAX,
    TIP_STATUS_APPROVED,
    TRANSCRIPT_KIND_GENERATED,
)
from checklist_care.scoring import score
from checklist_care.sources import packet as packet_module
from checklist_care.sources import text as text_module
from checklist_care.sources import youtube
from checklist_care.sources.packet import Chapter, Packet, Segment, Transcript
from checklist_care.store.lists import ListRow
from checklist_care.store.sources import SourceRow
from checklist_care.store.tips import TipDetail

ROOT = Path(__file__).resolve().parents[2]
LABELS = ROOT / "eval" / "labels" / "in_sample.json"
REF = youtube.source_ref("EXAMPLE0001")
OTHER = youtube.source_ref("EXAMPLE0002")
STAMP = connection.format_timestamp(datetime(2026, 11, 8, 12, tzinfo=UTC))
SKILL = "a" * 64
PACKET_SHA = "b" * 64
LISTS = {
    item.id: ListRow(item.id, item.place, item.audience, item.cue, item.id) for item in seed.LISTS
}
KIDS_LIST = next(item.id for item in seed.LISTS if item.audience == AUDIENCE_KIDS)
"""The first seeded kids list (Kids Bathroom)."""

CHAPTERS = (
    Chapter("Alpha", 0, 60),
    Chapter("Beta", 60, 120),
    Chapter("Gamma", 120, 200),
)


# --- builders -------------------------------------------------------------------------


def tip(
    tip_id: int,
    *,
    name: str = "Plain Tip",
    action: str = "Do the plain thing",
    reason: str | None = "Because it helps.",
    bucket: int = 3,
    audience: str = "adults",
    start_s: int | None = 10,
    list_ids: tuple[int, ...] = (),
    duplicate_of: int | None = None,
    possible_duplicate_of: int | None = None,
    locator: str | None = None,
    note: str | None = None,
    evidence_url: str | None = None,
) -> score.ScoredTip:
    return score.ScoredTip(
        id=tip_id,
        name=name,
        action=action,
        reason=reason,
        bucket=bucket,
        audience=audience,
        start_s=start_s,
        list_ids=list_ids,
        duplicate_of=duplicate_of,
        possible_duplicate_of=possible_duplicate_of,
        locator=locator,
        note=note,
        evidence_url=evidence_url,
    )


def as_archived(item: score.ScoredTip) -> score.TipFields:
    """``item`` as its archived proposal holds it: every compared field, no id."""

    return score.TipFields(**{name: getattr(item, name) for name in score.EDIT_FIELDS})


def packet(
    *,
    ref: str = REF,
    kind: str = SOURCE_KIND_YOUTUBE,
    chapters: Sequence[Chapter] = CHAPTERS,
    segments: int = 3,
    text: str | None = None,
) -> Packet:
    transcript = None
    if kind == SOURCE_KIND_YOUTUBE:
        transcript = Transcript(
            kind=TRANSCRIPT_KIND_GENERATED,
            language_code="en",
            segments=tuple(Segment(float(i), 1.0, "synthetic") for i in range(segments)),
        )
    return Packet(
        source_ref=ref,
        kind=kind,
        url=None,
        title=None,
        channel=None,
        duration_s=200 if kind == SOURCE_KIND_YOUTUBE else None,
        fetched_at=STAMP,
        transcript=transcript,
        chapters=tuple(chapters),
        text=text,
    )


def row(*, ref: str = REF, mode: str = SOURCE_MODE_EXTRACT, reviewed: bool = True) -> SourceRow:
    return SourceRow(
        id=2,
        kind=SOURCE_KIND_YOUTUBE,
        ref=ref,
        title=None,
        url=None,
        revision=None,
        mode=mode,
        fetched_at=STAMP,
        reviewed_at=STAMP if reviewed else None,
    )


def archived(
    tips: Sequence[score.ScoredTip] = (), *, host: str = HOST_CLAUDE_CODE, skill: str = SKILL
) -> score.Archived:
    return score.Archived(
        path=Path("archive.json"),
        packet_sha256=PACKET_SHA,
        skill_sha256=skill,
        host=host,
        model="m",
        mode=SOURCE_MODE_EXTRACT,
        tips=tuple(as_archived(item) for item in tips),
    )


def source(
    tips: Sequence[score.ScoredTip],
    *,
    label: score.SourceLabel | None = None,
    ref: str = REF,
    mode: str = SOURCE_MODE_EXTRACT,
    ref_count: int = 1,
    chapters: Sequence[Chapter] = CHAPTERS,
    host: str = HOST_CLAUDE_CODE,
    skill: str = SKILL,
) -> score.ScoredSource:
    return score.scored_source(
        label=label or score.SourceLabel(ref, ()),
        row=row(ref=ref, mode=mode),
        ref_count=ref_count,
        packet=packet(ref=ref, chapters=chapters),
        packet_sha256=PACKET_SHA,
        archived=archived(tips, host=host, skill=skill),
        tips=tips,
    )


def labels(
    sources: Sequence[score.SourceLabel] = (),
    *,
    pairs: Sequence[score.DuplicatePair] = (),
    soft: Sequence[score.SoftGroup] = (),
    criteria: Sequence[str] = ("IS-1",),
) -> score.Labels:
    return score.Labels(
        path=Path("labels.json"),
        sha256="c" * 64,
        claim="test",
        sources=tuple(sources) or (score.SourceLabel(REF, ()),),
        duplicate_pairs=tuple(pairs),
        soft=tuple(soft),
        criteria=tuple(criteria),
    )


def evidence(
    *sources: score.ScoredSource, the_labels: score.Labels | None = None
) -> score.Evidence:
    return score.Evidence(
        labels=the_labels or labels([item.label for item in sources]),
        sources={item.ref: item for item in sources},
        lists=LISTS,
    )


def result(criterion: str, the_evidence: score.Evidence) -> score.CriterionResult:
    return score.CRITERIA[criterion].evaluate(the_evidence)


# --- chapter windows ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start_s", "expected"),
    [
        (0, 0),
        (30, 0),
        (54, 0),  # 6 s before the boundary at 60: still Alpha
        (55, 1),  # 5 s before the boundary: counts toward Beta
        (59, 1),
        (60, 1),
        (119, 2),
        (120, 2),
        (195, 2),  # the end of the last chapter is no boundary
        (199, 2),
        (200, None),  # past the last chapter
        (None, None),
    ],
)
def test_chapter_index_with_the_five_second_lead(start_s: int | None, expected: int | None) -> None:
    assert score.chapter_index(start_s, CHAPTERS) == expected


def test_chapter_index_leaves_a_tip_in_a_gap_out_and_leads_into_a_late_first_chapter() -> None:
    chapters = (Chapter("One", 10, 50), Chapter("Two", 80, 100))

    assert score.chapter_index(5, chapters) == 0  # 5 s before the first start
    assert score.chapter_index(4, chapters) is None
    assert score.chapter_index(60, chapters) is None  # the gap, more than 5 s from Two
    assert score.chapter_index(75, chapters) == 1


def test_chapter_index_picks_the_nearest_boundary_among_short_chapters() -> None:
    chapters = (Chapter("Long", 0, 100), Chapter("Blip", 100, 102), Chapter("Next", 102, 150))

    assert score.chapter_index(98, chapters) == 1
    assert score.chapter_index(101, chapters) == 2  # 1 s before Next; inside Blip


def test_group_by_chapter_orders_each_window_by_id_and_keeps_empty_windows() -> None:
    tips = [tip(9, start_s=10), tip(3, start_s=50), tip(5, start_s=57), tip(7, start_s=None)]

    groups = score.group_by_chapter(tips, CHAPTERS)

    assert list(groups) == ["Alpha", "Beta", "Gamma"]
    assert [item.id for item in groups["Alpha"]] == [3, 9]
    assert [item.id for item in groups["Beta"]] == [5]
    assert groups["Gamma"] == ()


# --- labels ---------------------------------------------------------------------------


def labels_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(LABELS.read_bytes().decode("utf-8"))
    return document


def parse(document: Any) -> score.Labels:
    return score.parse_labels(json.dumps(document).encode("utf-8"), Path("labels.json"))


LABEL_DEFECTS: dict[str, tuple[Callable[[dict[str, Any]], Any], str]] = {
    "unknown top-level key": (lambda d: d.update(notes="x"), "unknown key(s) notes"),
    "unknown expect key": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update(on_list=[5]),
        "unknown key(s) on_list",
    ),
    "unknown chapter key": (
        lambda d: d["sources"][0]["chapters"][0].update(start_s=485),
        "unknown key(s) start_s",
    ),
    "required keys missing": (
        lambda d: [d.pop(key) for key in ("claim", "sources", "criteria")],
        "the labels file lacks claim, sources, criteria",
    ),
    "wrong schema": (lambda d: d.update(schema="x"), "schema must be"),
    "no sources": (lambda d: d.update(sources=[]), "sources must be a non-empty list"),
    "repeated ref": (lambda d: d["sources"].append(d["sources"][0]), "repeats a ref"),
    "repeated chapter title": (
        lambda d: d["sources"][1]["chapters"].append(d["sources"][1]["chapters"][0]),
        "repeats",
    ),
    "bucket as a bool": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update(bucket=True),
        "bucket must be one of",
    ),
    "bucket 5": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update(bucket=5),
        "bucket must be one of",
    ),
    "unknown audience": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update(audience="kids"),
        "audience must be one of",
    ),
    "name other than exact": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update(name="loose"),
        f'name must be "{score.NAME_EXACT}"',
    ),
    "empty on_any_of": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update(on_any_of=[]),
        "at least one list id",
    ),
    "empty expect": (lambda d: d["sources"][0]["chapters"][0].update(expect={}), "is empty"),
    "pair with an unlabelled ref": (
        lambda d: d["duplicate_pairs"][0].update(a=[REF, "X"]),
        "not one of the labelled sources",
    ),
    "pair with itself": (
        lambda d: d["duplicate_pairs"][0].update(b=d["duplicate_pairs"][0]["a"]),
        "with itself",
    ),
    "pair list_if_first 0": (
        lambda d: d["duplicate_pairs"][0].update(list_if_first=0),
        "list_if_first",
    ),
    "pair list_if_first above ID_MAX": (
        lambda d: d["duplicate_pairs"][0].update(list_if_first=ID_MAX + 1),
        "list_if_first",
    ),
    "list id above ID_MAX": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update(on_any_of=[ID_MAX + 1]),
        "must be a list of list ids",
    ),
    "soft at_least too high": (lambda d: d["soft"][0].update(at_least=2), "at_least"),
    "no criteria": (lambda d: d.update(criteria=[]), "criteria must be a non-empty list"),
    "repeated criterion": (lambda d: d["criteria"].append("IS-1"), "repeats an id"),
    "unknown criterion": (lambda d: d["criteria"].append("OOS-1"), "'OOS-1' is not one"),
    "unpaired surrogate in a chapter title": (
        lambda d: d["sources"][1]["chapters"][0].update(title="\ud800"),
        "sources[1] holds an unpaired surrogate",
    ),
    "unpaired surrogate in a nested key": (
        lambda d: d["sources"][0]["chapters"][0]["expect"].update({"\udc00": 1}),
        "sources[0] holds an unpaired surrogate",
    ),
    "IS-3 without an exact name": (
        lambda d: [
            chapter["expect"].pop("name", None)
            for source in d["sources"]
            for chapter in source["chapters"]
        ],
        f'IS-3 needs a labelled chapter with "name": "{score.NAME_EXACT}"',
    ),
    "IS-4 without a placement": (
        lambda d: [
            chapter.update(expect={"name": score.NAME_EXACT})
            for source in d["sources"]
            for chapter in source["chapters"]
        ],
        "IS-4 needs",
    ),
    "IS-5 without a pair": (lambda d: d.pop("duplicate_pairs"), "IS-5 needs"),
    "IS-S without a soft group": (lambda d: d.pop("soft"), "IS-S needs"),
}


@pytest.mark.parametrize("defect", sorted(LABEL_DEFECTS))
def test_a_defective_labels_file_is_refused(defect: str) -> None:
    change, message = LABEL_DEFECTS[defect]
    document = labels_document()
    change(document)

    with pytest.raises(score.LabelsError) as raised:
        parse(document)

    assert message in str(raised.value)


def test_labels_whose_json_is_not_an_object_are_refused() -> None:
    # Strict-JSON defects (truncation, repeated keys, NaN, UTF-16 ...) are asserted for
    # both readers in tests/unit/test_strict_json.py; this is the labels' own shape rule.
    with pytest.raises(score.LabelsError, match="the labels file must be a JSON object"):
        score.parse_labels(b"[]", Path("labels.json"))


def test_load_labels_adds_the_labels_advice_and_aborts(tmp_path: Path) -> None:
    # The committed labels saved as UTF-16: the shared decoder's message (asserted in
    # test_strict_json.py), this reader's advice, and the abort that wraps both.
    labels_file = tmp_path / "labels.json"
    text = LABELS.read_bytes().decode("utf-8")
    labels_file.write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))

    with pytest.raises(EvalAbortError) as aborted:
        score.load_labels(labels_file)

    assert aborted.value.message == (
        f"{score.ABORT_LABELS}: {labels_file}: the file is UTF-16 (it starts with a UTF-16 "
        "byte-order mark); save the labels as UTF-8"
    )


def test_a_labels_file_over_the_size_cap_is_refused_unparsed(tmp_path: Path) -> None:
    labels_file = tmp_path / "labels.json"
    committed = LABELS.read_bytes()
    padding = score.LABELS_FILE_MAX_BYTES - len(committed)
    labels_file.write_bytes(committed + b" " * padding)
    assert score.load_labels(labels_file).criteria  # exactly at the cap: read

    labels_file.write_bytes(committed + b" " * (padding + 1))

    with pytest.raises(EvalAbortError, match="larger than LABELS_FILE_MAX_BYTES"):
        score.load_labels(labels_file)


def test_a_labels_path_that_is_not_a_regular_file_is_refused_unopened(tmp_path: Path) -> None:
    # A folder stands in for a device or a pipe, which can block a reader or never end.
    with pytest.raises(EvalAbortError) as aborted:
        score.load_labels(tmp_path)

    assert aborted.value.message == (
        f"{score.ABORT_LABELS}: {tmp_path}: {strict_json.NOT_REGULAR_FILE}"
    )


def test_the_criteria_registry_holds_the_in_sample_ids_and_their_gates() -> None:
    gates = {criterion_id: item.gate for criterion_id, item in score.CRITERIA.items()}

    assert gates == {
        **{f"IS-{n}": score.HARD for n in range(1, 9)},
        "IS-S": score.SOFT,
    }
    assert all(item.id == criterion_id for criterion_id, item in score.CRITERIA.items())


# --- ABORT rules ------------------------------------------------------------------------


def rules(aborts: Sequence[score.Abort]) -> list[str]:
    return [abort.rule for abort in aborts]


def test_a_well_formed_youtube_packet_has_no_abort() -> None:
    assert score.packet_aborts(REF, SOURCE_KIND_YOUTUBE, packet()) == []


@pytest.mark.parametrize(
    ("made", "rule", "detail"),
    [
        (packet(segments=0), score.ABORT_PACKET_EMPTY, "zero segments"),
        (replace(packet(), transcript=None), score.ABORT_PACKET_EMPTY, "zero segments"),
        (packet(chapters=()), score.ABORT_PACKET_EMPTY, "zero chapters"),
        (
            packet(chapters=(Chapter("A", 0, 60), Chapter("B", 50, 90))),
            score.ABORT_PACKET,
            "before the previous chapter ends",
        ),
        (
            packet(chapters=(Chapter("A", 60, 90), Chapter("B", 0, 60))),
            score.ABORT_PACKET,
            "before the previous chapter ends",
        ),
        (packet(chapters=(Chapter("A", 10, 10),)), score.ABORT_PACKET, "runs from 10 to 10"),
        (
            packet(chapters=(Chapter("A", 0, 10), Chapter("A", 10, 20))),
            score.ABORT_PACKET,
            "repeats an earlier chapter title",
        ),
        (packet(ref=OTHER), score.ABORT_PACKET, f"belongs to {OTHER}"),
    ],
)
def test_a_youtube_packet_that_cannot_be_scored_aborts(
    made: Packet, rule: str, detail: str
) -> None:
    aborts = score.packet_aborts(REF, SOURCE_KIND_YOUTUBE, made)

    assert rules(aborts) == [rule]
    assert detail in aborts[0].detail


@pytest.mark.parametrize("kind", [SOURCE_KIND_WIKI, SOURCE_KIND_TEXT])
@pytest.mark.parametrize("text", [None, "", " \n\t "])
def test_a_wiki_or_text_packet_with_empty_text_aborts(kind: str, text: str | None) -> None:
    aborts = score.packet_aborts(REF, kind, packet(kind=kind, chapters=(), text=text))

    assert rules(aborts) == [score.ABORT_PACKET_EMPTY]
    assert "empty text" in aborts[0].detail


@pytest.mark.parametrize("kind", [SOURCE_KIND_WIKI, SOURCE_KIND_TEXT])
def test_a_wiki_or_text_packet_with_text_has_no_abort(kind: str) -> None:
    assert score.packet_aborts(REF, kind, packet(kind=kind, chapters=(), text="Advice.")) == []


def test_a_packet_of_another_kind_than_its_source_aborts() -> None:
    aborts = score.packet_aborts(REF, SOURCE_KIND_WIKI, packet())

    assert rules(aborts) == [score.ABORT_PACKET]


def test_a_label_title_must_be_byte_equal_to_a_chapter_title() -> None:
    chapters = (Chapter("Sit Up Straight, You're Not Dead Yet", 0, 60), Chapter("Beta", 60, 90))
    made = packet(chapters=chapters)

    assert score.title_aborts(REF, ["Sit Up Straight, You're Not Dead Yet", "Beta"], made) == []
    drifted = score.title_aborts(
        REF, ["Sit Up Straight, You’re Not Dead Yet", "Beta ", "beta", "Gamma"], made
    )

    assert rules(drifted) == [score.ABORT_CHAPTER_TITLE] * 4
    assert '(the packet has "Sit Up Straight, You\'re Not Dead Yet")' in drifted[0].detail
    assert "(the packet has 'Beta')" in drifted[1].detail
    assert "the packet has" not in drifted[3].detail


def test_the_criteria_see_a_tip_only_through_the_fields_compared_with_the_archive() -> None:
    # Every field a criterion reads is compared with the archive: a ScoredTip holds
    # exactly the compared fields and its id, so a new field cannot skip the comparison.
    assert tuple(item.name for item in fields(score.TipFields)) == score.EDIT_FIELDS
    assert tuple(item.name for item in fields(score.ScoredTip)) == (*score.EDIT_FIELDS, "id")


def test_a_scored_tip_copies_the_stored_tip_with_its_lists_sorted() -> None:
    stored = TipDetail(
        id=7,
        name="Name",
        action="Act",
        reason="Why",
        bucket=1,
        audience="kids+",
        status=TIP_STATUS_APPROVED,
        source_id=2,
        source_ref=REF,
        source_kind=SOURCE_KIND_YOUTUBE,
        start_s=42,
        locator="Ch",
        evidence_url=None,
        duplicate_of=None,
        possible_duplicate_of=3,
        note="n",
        created_at=STAMP,
        updated_at=STAMP,
        list_ids=(6, 2),
    )

    assert score.ScoredTip.of(stored) == tip(
        7,
        name="Name",
        action="Act",
        reason="Why",
        bucket=1,
        audience="kids+",
        start_s=42,
        list_ids=(2, 6),
        possible_duplicate_of=3,
        locator="Ch",
        note="n",
    )


def test_an_unchanged_tip_is_not_an_edit() -> None:
    # List order is no part of the comparison: ScoredTip.of and read_archive both sort.
    stored = [tip(1, list_ids=(5, 6), bucket=1), tip(2)]

    assert score.edit_aborts(REF, stored, archived(stored).tips) == []


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": "Other"}, "name"),
        ({"action": "Other action"}, "action"),
        ({"reason": "Another reason."}, "reason"),
        ({"bucket": 4}, "bucket"),
        ({"audience": "kids+"}, "audience"),
        ({"start_s": 70}, "start_s"),  # moves the tip from window Alpha to Beta
        ({"start_s": None}, "start_s"),
        ({"list_ids": (7,)}, "list_ids"),
        ({"duplicate_of": 9}, "duplicate_of"),
        ({"possible_duplicate_of": 9}, "possible_duplicate_of"),
        ({"locator": "Another chapter"}, "locator"),
        ({"note": "Another note."}, "note"),
        ({"evidence_url": "https://example.com/another"}, "evidence_url"),
    ],
)
def test_each_edited_field_aborts(changes: dict[str, Any], field: str) -> None:
    original = archived([tip(1)]).tips

    aborts = score.edit_aborts(REF, [tip(1, **changes)], original)

    assert rules(aborts) == [score.ABORT_OPERATOR_EDIT]
    assert aborts[0].detail.endswith(f"tip #1 differs from its archived proposal in {field}")


ARCHIVE_TIP: dict[str, Any] = {
    "name": "Name",
    "action": "Act",
    "reason": "Why",
    "bucket": 3,
    "audience": "adults",
    "list_ids": [],
    "start_s": 12,
}


def write_archive(
    data_dir: Path, *, tip_fields: dict[str, Any] | None = None, **envelope: Any
) -> None:
    """An archived proposal of ``REF`` holding one tip, with ``envelope`` and
    ``tip_fields`` changed."""

    document: dict[str, Any] = {
        "schema": proposal.PROPOSAL_SCHEMA,
        "source_ref": REF,
        "packet_sha256": PACKET_SHA,
        "skill_sha256": SKILL,
        "host": HOST_CLAUDE_CODE,
        "model": "m",
        "mode": SOURCE_MODE_EXTRACT,
        "degraded": False,
        "tips": [{**ARCHIVE_TIP, **(tip_fields or {})}],
        **envelope,
    }
    path = proposal.archive_path(data_dir, REF)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(document).encode("utf-8"))


def test_read_archive_holds_every_compared_field_and_the_mode(tmp_path: Path) -> None:
    write_archive(
        tmp_path,
        tip_fields={
            "list_ids": [6, 2],
            "possible_duplicate_of": 4,
            "locator": "Ch",
            "evidence_url": "https://example.com/a",
        },
    )

    read = score.read_archive(tmp_path, REF)

    assert (read.host, read.model, read.mode) == (HOST_CLAUDE_CODE, "m", SOURCE_MODE_EXTRACT)
    assert read.tips == (
        score.TipFields(
            name="Name",
            action="Act",
            reason="Why",
            bucket=3,
            audience="adults",
            start_s=12,
            list_ids=(2, 6),
            duplicate_of=None,
            possible_duplicate_of=4,
            locator="Ch",
            note=None,
            evidence_url="https://example.com/a",
        ),
    )


@pytest.mark.parametrize(
    ("envelope", "tip_fields", "detail"),
    [
        ({"mode": None}, {}, "lacks its host, model or mode"),
        ({}, {"reason": None}, "lacks its name, action, reason or audience"),
        ({}, {"start_s": "12"}, "tips[0].start_s is not a whole second"),
        ({}, {"start_s": True}, "tips[0].start_s is not a whole second"),
        ({}, {"duplicate_of": 1.5}, "tips[0].duplicate_of is not a tip id"),
        ({}, {"note": 5}, "tips[0].note is not a string or null"),
    ],
)
def test_an_archive_without_a_readable_compared_field_is_unusable(
    tmp_path: Path, envelope: dict[str, Any], tip_fields: dict[str, Any], detail: str
) -> None:
    write_archive(tmp_path, tip_fields=tip_fields, **envelope)

    with pytest.raises(score.ArchiveError, match=re.escape(detail)):
        score.read_archive(tmp_path, REF)


def test_an_added_or_deleted_tip_aborts() -> None:
    original = archived([tip(1), tip(2)]).tips

    for stored in ([tip(1)], [tip(1), tip(2), tip(3)]):
        aborts = score.edit_aborts(REF, stored, original)
        assert rules(aborts) == [score.ABORT_OPERATOR_EDIT]
        assert "added or deleted" in aborts[0].detail


def test_consistent_current_proposals_have_no_abort_and_models_may_differ() -> None:
    first = source([], ref=REF)
    second = replace(source([], ref=OTHER), archived=replace(archived(), model="other-model"))

    assert score.consistency_aborts([first, second], SKILL) == []


def test_mixed_hosts_abort() -> None:
    sources = [source([], ref=REF), source([], ref=OTHER, host=HOST_CODEX)]

    assert rules(score.consistency_aborts(sources, SKILL)) == [score.ABORT_MIXED_HOST]


def test_mixed_skill_versions_abort_and_name_the_stale_one() -> None:
    sources = [source([], ref=REF, skill="d" * 64), source([], ref=OTHER)]

    aborts = score.consistency_aborts(sources, SKILL)

    assert rules(aborts) == [score.ABORT_MIXED_SKILL, score.ABORT_SKILL_STALE]
    assert REF in aborts[1].detail and OTHER not in aborts[1].detail


def test_a_skill_changed_since_the_proposals_aborts_as_stale() -> None:
    sources = [source([], ref=REF), source([], ref=OTHER)]

    assert rules(score.consistency_aborts(sources, "e" * 64)) == [score.ABORT_SKILL_STALE]


# --- criteria ----------------------------------------------------------------------------


def test_is1_fails_on_a_mode_other_than_extract_or_a_repeated_ref() -> None:
    assert result("IS-1", evidence(source([]))).passed
    assert not result("IS-1", evidence(source([], mode=SOURCE_MODE_INVESTIGATE))).passed
    failed = result("IS-1", evidence(source([], ref_count=2)))
    assert failed.failures == (f"{REF} is stored 2 times, expected once",)


@pytest.mark.parametrize(("count", "passed"), [(7, False), (8, True), (12, True), (13, False)])
def test_is2_needs_8_to_12_tips(count: int, passed: bool) -> None:
    # Spread evenly over the 200 s, so every chapter window holds a tip at each count
    # and only the count rule can fail a case.
    tips = [tip(i, start_s=i * 200 // (count + 1)) for i in range(1, count + 1)]

    assert result("IS-2", evidence(source(tips))).passed is passed


def test_is2_needs_every_chapter_window_to_hold_a_tip() -> None:
    tips = [tip(i, start_s=10 + i) for i in range(1, 9)] + [tip(20, start_s=130)]

    failed = result("IS-2", evidence(source(tips)))

    assert failed.failures == (f"{REF}: 2/3 chapter windows hold a tip; empty: Beta",)


def labelled(*chapters: score.ChapterLabel) -> score.SourceLabel:
    return score.SourceLabel(REF, chapters)


def test_is3_scores_the_lowest_id_in_the_window_and_notes_the_others() -> None:
    label = labelled(score.ChapterLabel("Beta", score.Expect(exact_name=True)))
    good = [tip(4, name="Beta", start_s=70), tip(6, name="Other", start_s=80)]
    bad = [tip(4, name="Other", start_s=70), tip(6, name="Beta", start_s=80)]

    passed = result("IS-3", evidence(source(good, label=label)))
    failed = result("IS-3", evidence(source(bad, label=label)))

    assert passed.passed
    assert passed.notes == (
        f"{REF}: the window 'Beta' holds 2 tips (#4, #6); scored #4, the lowest id",
    )
    assert not failed.passed
    assert "#4 'Other'" in failed.failures[0]


def test_a_labelled_window_without_a_tip_fails() -> None:
    label = labelled(score.ChapterLabel("Gamma", score.Expect(bucket=1, exact_name=True)))

    for criterion in ("IS-3", "IS-4"):
        failed = result(criterion, evidence(source([tip(1, start_s=5)], label=label)))
        assert failed.failures == (f"{REF}: no tip in the chapter window 'Gamma'",)


@pytest.mark.parametrize(
    ("expect", "fields", "misses"),
    [
        (score.Expect(bucket=2), {"bucket": 2}, []),
        (score.Expect(bucket=2), {"bucket": 3}, ["is in bucket 3, expected 2"]),
        (score.Expect(not_bucket=1), {"bucket": 1}, ["is in bucket 1, expected any bucket but 1"]),
        (score.Expect(audience="adults"), {"audience": "kids+"}, ["is kids+, expected adults"]),
        (score.Expect(on_lists=()), {"list_ids": ()}, []),
        (score.Expect(on_lists=()), {"list_ids": (7,)}, ["is on lists [7], expected no list"]),
        (score.Expect(on_lists=(1,)), {"list_ids": (1, 4)}, []),
        (score.Expect(on_lists=(1,)), {"list_ids": (4,)}, ["is on lists [4], not on [1]"]),
        (score.Expect(on_any_of=(5, 6)), {"list_ids": (6,)}, []),
        (score.Expect(on_any_of=(5, 6)), {"list_ids": (7,)}, ["is on lists [7], none of [5, 6]"]),
        (score.Expect(on_any_of=(5, 6)), {"list_ids": ()}, ["is on lists [], none of [5, 6]"]),
    ],
)
def test_expectation_misses(
    expect: score.Expect, fields: dict[str, Any], misses: list[str]
) -> None:
    assert score.expectation_misses(tip(1, **fields), expect) == misses


def test_is4_reports_every_miss_of_the_tip_in_the_window() -> None:
    label = labelled(score.ChapterLabel("Alpha", score.Expect(bucket=2, audience="adults")))
    found = tip(1, bucket=3, audience="kids+")

    failed = result("IS-4", evidence(source([found], label=label)))

    assert failed.failures == (
        f"{REF}: the tip in 'Alpha' (#1 'Plain Tip') is in bucket 3, expected 2; "
        "is kids+, expected adults",
    )


def pair_evidence(
    first: score.ScoredTip, second: score.ScoredTip, *extra: score.ScoredTip
) -> score.Evidence:
    pair = score.DuplicatePair(score.ChapterRef(REF, "Alpha"), score.ChapterRef(OTHER, "Beta"), 7)
    one = source([first], ref=REF, label=score.SourceLabel(REF, ()))
    two = source([second, *extra], ref=OTHER, label=score.SourceLabel(OTHER, ()))
    the_labels = labels([one.label, two.label], pairs=[pair])
    return evidence(one, two, the_labels=the_labels)


@pytest.mark.parametrize("earlier_in_a", [True, False])
def test_is5_is_order_symmetric(earlier_in_a: bool) -> None:
    low, high = (1, 11) if earlier_in_a else (11, 1)
    a = tip(low, start_s=5, bucket=1, list_ids=(7,))
    b = tip(high, start_s=70, bucket=4, duplicate_of=low)
    if not earlier_in_a:
        a, b = (
            replace(a, bucket=4, list_ids=(), duplicate_of=high),
            replace(b, bucket=1, list_ids=(7,), duplicate_of=None),
        )

    assert result("IS-5", pair_evidence(a, b)).passed
    assert result("IS-6", pair_evidence(a, b)).passed


@pytest.mark.parametrize(
    ("earlier", "later", "failure"),
    [
        ({"bucket": 3, "list_ids": ()}, {}, "is in bucket 3, expected 1"),
        ({"list_ids": (6,)}, {}, "is on lists [6], not on 7"),
        ({}, {"bucket": 3, "duplicate_of": None}, "is in bucket 3, expected 4"),
        ({}, {"duplicate_of": 99}, "has duplicate_of 99, expected 1"),
    ],
)
def test_is5_fails_when_either_side_of_the_pair_is_wrong(
    earlier: dict[str, Any], later: dict[str, Any], failure: str
) -> None:
    a = tip(1, **{"start_s": 5, "bucket": 1, "list_ids": (7,), **earlier})
    b = tip(11, **{"start_s": 70, "bucket": 4, "duplicate_of": 1, **later})

    failed = result("IS-5", pair_evidence(a, b))

    assert not failed.passed
    assert any(failure in text for text in failed.failures)


def test_is6_allows_only_the_later_pair_tip_a_duplicate_of_and_any_tip_a_possible_one() -> None:
    a = tip(1, start_s=5, bucket=1, list_ids=(7,))
    b = tip(11, start_s=70, bucket=4, duplicate_of=1)
    near = tip(12, start_s=130, bucket=1, possible_duplicate_of=1)
    stray = tip(13, start_s=140, bucket=4, duplicate_of=12)

    passed = result("IS-6", pair_evidence(a, b, near))
    failed = result("IS-6", pair_evidence(a, b, near, stray))

    assert passed.passed
    assert passed.notes == ("#12 'Plain Tip' has possible_duplicate_of 1",)
    assert not failed.passed
    assert "#13 'Plain Tip' has duplicate_of 12" in failed.failures[0]


@pytest.mark.parametrize(
    ("fields", "failure"),
    [
        # The placement rules are placement.py's, so the failures carry its texts.
        (
            {"bucket": 1, "audience": "adults", "list_ids": (KIDS_LIST,)},
            f"#1 'Plain Tip': list {KIDS_LIST} (Kids Bathroom — Kids) is a kids list and "
            "accepts only kids+ tips; this tip is adults",
        ),
        ({"bucket": 2, "list_ids": (7,)}, "only bucket-1 (daily) tips go on lists"),
        ({"list_ids": (99,), "bucket": 1}, "list 99 does not exist"),
        (
            {"name": "N" * (NAME_MAX + 1)},
            f"its name is {NAME_MAX + 1} characters; NAME_MAX is {NAME_MAX}",
        ),
        ({"action": "A" * (ACTION_MAX + 1)}, f"its action is {ACTION_MAX + 1} characters"),
        ({"reason": None}, "its reason is missing"),
        ({"reason": "line\nbreak"}, "its reason must be one line"),
        # Each text within its own limit, the printed line over LINE_MAX.
        (
            {"name": "N" * NAME_MAX, "action": "A" * (LINE_MAX - NAME_MAX)},
            f"LINE_MAX is {LINE_MAX}",
        ),
        (
            {"reason": "R" * (REASON_MAX + 1)},
            f"its reason is {REASON_MAX + 1} characters; REASON_MAX is {REASON_MAX}",
        ),
        (
            {"locator": "L" * (LOCATOR_MAX + 1)},
            f"its locator is {LOCATOR_MAX + 1} characters; LOCATOR_MAX is {LOCATOR_MAX}",
        ),
        ({"locator": ""}, "its locator must not be empty; use null when there is none"),
        (
            {"note": "N" * (NOTE_MAX + 1)},
            f"its note is {NOTE_MAX + 1} characters; NOTE_MAX is {NOTE_MAX}",
        ),
        (
            {"evidence_url": "https://example.com/" + "p" * EVIDENCE_URL_MAX},
            f"; EVIDENCE_URL_MAX is {EVIDENCE_URL_MAX}",
        ),
        # The packet lasts 200 s (the builder's duration), the tighter start_s bound.
        ({"start_s": -1}, "its start_s is -1; it must be 0 <= start_s < duration_s (200)"),
        ({"start_s": 200}, "its start_s is 200; it must be 0 <= start_s < duration_s (200)"),
        ({"bucket": 5}, f"its bucket is 5; it {proposal.NOT_A_BUCKET}"),
        (
            {"audience": "teens"},
            f"its audience is 'teens'; it {proposal.NOT_A_TIP_AUDIENCE}",
        ),
        (
            {"possible_duplicate_of": ID_MAX + 1},
            f"its possible_duplicate_of is {ID_MAX + 1}, not a tip id",
        ),
        ({"bucket": 4, "duplicate_of": 0}, "its duplicate_of is 0, not a tip id"),
    ],
)
def test_is7_fails_on_each_broken_invariant(fields: dict[str, Any], failure: str) -> None:
    failed = result("IS-7", evidence(source([tip(1, **fields)])))

    assert not failed.passed
    assert any(failure in text for text in failed.failures), failed.failures


def test_placed_tips_within_every_limit_keep_is7() -> None:
    adults = tip(1, bucket=1, list_ids=(1,))
    kids = tip(2, bucket=1, audience="kids+", list_ids=(KIDS_LIST, 6))
    at_every_limit = tip(
        3,
        name="N" * NAME_MAX,
        action="A" * (LINE_MAX - NAME_MAX - len(LINE_SEPARATOR)),
        reason="R" * REASON_MAX,
        bucket=4,
        start_s=199,  # the last whole second of the 200 s packet
        duplicate_of=ID_MAX,
        possible_duplicate_of=1,
        locator="L" * LOCATOR_MAX,
        note="N" * NOTE_MAX,
        evidence_url="https://example.com/" + "p" * (EVIDENCE_URL_MAX - 20),
    )
    first_second = tip(4, start_s=0)

    assert result("IS-7", evidence(source([adults, kids, at_every_limit, first_second]))).passed


def test_is7_bounds_start_s_by_start_s_max_when_the_packet_has_no_duration() -> None:
    def scored(start_s: int) -> score.ScoredSource:
        held = source([tip(1, start_s=start_s)])
        return replace(held, packet=replace(held.packet, duration_s=None))

    failed = result("IS-7", evidence(scored(START_S_MAX + 1)))

    assert failed.failures == (
        f"{REF}: #1 'Plain Tip': its start_s is {START_S_MAX + 1}; it must be "
        f"0 <= start_s <= START_S_MAX ({START_S_MAX})",
    )
    assert result("IS-7", evidence(scored(START_S_MAX))).passed


def test_every_tip_text_limit_names_its_limits_constant_and_a_compared_field() -> None:
    # IS-7 reads each text field through proposal.TIP_TEXT_LIMITS, so each one must be
    # a field the scorer holds (and compares with the archive), and the constant name
    # its message gives must be the limits.py constant of that value.
    assert set(proposal.TIP_TEXT_LIMITS) <= set(score.EDIT_FIELDS)
    for constant, value in proposal.TIP_TEXT_LIMITS.values():
        assert getattr(limits, constant) == value


def test_is8_fails_on_subscribe_in_any_case() -> None:
    assert result("IS-8", evidence(source([tip(1)]))).passed

    for filler in (
        {"name": "Subscribe Today"},
        {"action": "Hit SUBSCRIBE and the bell"},
        {"action": "unsubscribe"},
    ):
        assert not result("IS-8", evidence(source([tip(1, **filler)]))).passed, filler


def test_is_soft_counts_the_chapters_that_meet_the_expectation() -> None:
    group = score.SoftGroup(REF, ("Alpha", "Beta", "Gamma"), 2, score.Expect(bucket=3))
    the_labels = labels(soft=[group], criteria=["IS-S"])
    two = [tip(1, start_s=5, bucket=3), tip(2, start_s=70, bucket=3), tip(3, start_s=130, bucket=2)]
    one = [tip(1, start_s=5, bucket=3), tip(2, start_s=70, bucket=2)]

    passed = result("IS-S", evidence(source(two), the_labels=the_labels))
    failed = result("IS-S", evidence(source(one), the_labels=the_labels))

    assert passed.passed and passed.gate == score.SOFT
    assert not failed.passed
    assert failed.failures == (
        f"{REF}: 1 of 3 chapters (Alpha; Beta; Gamma) meet the expectation, expected at least 2",
    )


# --- the report ------------------------------------------------------------------------


def report(
    *criteria: score.CriterionResult, aborts: Sequence[score.Abort] = ()
) -> score.ScoreReport:
    return score.ScoreReport(
        scored_at=STAMP,
        data_dir=Path("data"),
        labels=labels(),
        skill_sha256=SKILL,
        sources=(),
        aborts=tuple(aborts),
        criteria=tuple(criteria),
    )


def test_a_failed_soft_criterion_never_changes_the_verdict() -> None:
    made = report(
        score.CriterionResult("IS-1", score.HARD, True),
        score.CriterionResult("IS-S", score.SOFT, False, ("missed",)),
    )

    assert (made.verdict, made.exit_code, made.failed, made.soft_failed) == (
        score.PASS,
        EXIT_OK,
        (),
        ("IS-S",),
    )


def test_a_failed_hard_criterion_fails_and_an_abort_wins() -> None:
    failed = report(score.CriterionResult("IS-2", score.HARD, False, ("x",)))
    aborted = report(aborts=[score.Abort(score.ABORT_SOURCE_MISSING, "gone")])

    assert (failed.verdict, failed.exit_code, failed.failed) == (
        score.FAIL,
        EXIT_EVAL_FAIL,
        ("IS-2",),
    )
    assert (aborted.verdict, aborted.exit_code) == (score.ABORT, EXIT_EVAL_ABORT)
    assert aborted.abort_message() == (
        f"1 abort condition, no verdict: {score.ABORT_SOURCE_MISSING}: gone"
    )


UNTRUSTED = "Title\x1b[2K\r\nverdict: PASS\x85\x9b1A\t\x7f\N{RIGHT-TO-LEFT OVERRIDE}"
"""Text a chapter title, ref, host or model may hold: ESC and C1 control sequences, a
line break with a forged verdict line, a tab, DEL and a bidirectional override."""


def test_every_printed_report_path_escapes_untrusted_text_and_the_json_keeps_it() -> None:
    summary = score.SourceSummary(REF, 1, UNTRUSTED, UNTRUSTED, SKILL, PACKET_SHA)
    criterion = score.CriterionResult("IS-2", score.HARD, False, (UNTRUSTED,), (UNTRUSTED,))
    failed = replace(report(criterion), sources=(summary,))
    aborted = report(aborts=[score.Abort(score.ABORT_CHAPTER_TITLE, UNTRUSTED)])
    escaped = r"Title\x1b[2K\r\nverdict: PASS\x85\x9b1A\t\x7f"

    for made in (failed, aborted):
        printed = made.lines()
        assert all(line.isprintable() for line in printed), printed
        assert [line for line in printed if line.startswith("verdict:")] == [
            f"verdict: {made.verdict}"
        ]
    printed = failed.lines()
    assert any(line.startswith(f"source: {REF}: 1 tips; host {escaped}") for line in printed)
    assert any(line.startswith(f"    fail: {escaped}") for line in printed)
    assert any(line.startswith(f"    note: {escaped}") for line in printed)
    abort_line = f"{score.ABORT_CHAPTER_TITLE}: {escaped}"
    assert any(line.startswith(f"abort: {abort_line}") for line in aborted.lines())
    message = aborted.abort_message()
    assert message.isprintable()
    assert message.startswith(f"1 abort condition, no verdict: {abort_line}")
    # The score file keeps every string exact.
    assert failed.to_json()["criteria"][0]["failures"] == [UNTRUSTED]
    assert failed.to_json()["sources"][0]["host"] == UNTRUSTED
    assert aborted.to_json()["aborts"][0]["detail"] == UNTRUSTED


def test_printable_text_prints_unchanged() -> None:
    text = "Sit Up Straight, You’re Not Dead Yet: C:\\data\\eval é 'x' \"y\""

    assert score.printable(text) == text


OWNED: tuple[tuple[ModuleType, ModuleType, str], ...] = (
    # (the module that imports it, its one owner, the name); strict_json's readers are
    # in tests/unit/test_strict_json.py.
    (score, proposal, "HASH_FIELDS"),
    (score, proposal, "is_sha256"),
    (score, proposal, "is_id"),
    (score, proposal, "is_bucket"),
    (score, proposal, "NOT_A_BUCKET"),
    (score, proposal, "is_tip_audience"),
    (score, proposal, "NOT_A_TIP_AUDIENCE"),
    (score, proposal, "TIP_TEXT_LIMITS"),
    (score, proposal, "TIP_OPTIONAL"),
    (score, proposal, "text_problem"),
    (score, proposal, "line_problem"),
    (score, proposal, "start_problem"),
    (score, proposal, "holds_surrogate"),
    (score, placement, "placement_problems"),
    (score, connection, "BUSY_TIMEOUT_PRAGMA"),
    (score, connection, "open_connection"),
    (score, context, "decode_source_packet"),
    (score, paths, "sibling_path"),
    (score, paths, "numbered_paths"),
    (score, paths, "lacks_hard_links"),
    (packet_module, paths, "sibling_path"),
    (packet_module, paths, "lacks_hard_links"),
)


@pytest.mark.parametrize(
    ("consumer", "owner", "name"),
    OWNED,
    ids=[f"{c.__name__.rsplit('.', 1)[-1]}.{name}" for c, _, name in OWNED],
)
def test_each_consumer_holds_its_owners_object_not_a_copy(
    consumer: ModuleType, owner: ModuleType, name: str
) -> None:
    # The consumer's attribute against the owner's: a re-spelled copy is equal, not the same.
    assert consumer is not owner
    assert getattr(consumer, name) is getattr(owner, name)


def test_the_packet_cap_holds_the_text_ingest_caps_object_not_a_copy() -> None:
    # score binds the cap under its own name, which OWNED's one-name entries cannot pin.
    # A freshly built equal int is another object, so a re-spelled copy fails the pin.
    assert int(str(text_module.TEXT_FILE_MAX_BYTES)) is not text_module.TEXT_FILE_MAX_BYTES
    assert score.PACKET_FILE_MAX_BYTES is text_module.TEXT_FILE_MAX_BYTES


@pytest.mark.parametrize("given", ["absolute", "relative"])
def test_the_read_only_open_takes_any_database_path_and_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    # A space, "#" and "%20" must reach SQLite as part of the file name, not as URI syntax.
    folder = tmp_path / "data dir #1 %20"
    folder.mkdir()
    database = folder / paths.DB_FILENAME
    with closing(connection.connect(database)):
        pass
    before = database.read_bytes()
    monkeypatch.chdir(tmp_path)
    opened = Path(folder.name) / paths.DB_FILENAME if given == "relative" else database

    with closing(score.connect_read_only(opened)) as conn:
        assert connection.schema_version(conn) == connection.SCHEMA_VERSION
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE scratch (x)")

    assert database.read_bytes() == before
