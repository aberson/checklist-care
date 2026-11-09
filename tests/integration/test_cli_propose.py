"""``checklist-care context`` and ``checklist-care propose`` through ``cli.main`` (plan §4.5).

Every test runs the production path: ``init``, then ``fetch`` with the two network
seams faked from ``tests/fixtures/packet_youtube_synthetic.json`` (real chapter
titles and timings, synthetic transcript text), then ``context`` and ``propose``.
Proposals start from ``tests/fixtures/proposal_valid.json``; its ``skill_sha256``
is a placeholder, always replaced by the value ``context`` printed, because the
real fingerprint changes with every edit to the skill files.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from youtube_transcript_api import FetchedTranscript, FetchedTranscriptSnippet

from checklist_care import cli, context, limits, paths, proposal
from checklist_care.db import connection, seed
from checklist_care.sources import youtube
from checklist_care.store import sources as source_store
from checklist_care.store import tips as tip_store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PACKET_FIXTURE = FIXTURES / "packet_youtube_synthetic.json"
PROPOSAL_FIXTURE = FIXTURES / "proposal_valid.json"

VIDEO = "LpeDuC2sQ-w"
REF = f"youtube:{VIDEO}"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-08T12:00:00Z"
REVIEW_LINE = "Review: http://127.0.0.1:8450/#/review"


def fixture_packet() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(PACKET_FIXTURE.read_bytes().decode("utf-8"))
    return payload


def fixture_proposal() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(PROPOSAL_FIXTURE.read_bytes().decode("utf-8"))
    return payload


# --- fake seams built from the fixture packet ----------------------------------------


@dataclass
class FakeClient:
    """Returns the fixture's segments as the transcript library's own objects."""

    text_suffix: str = ""
    no_segments: bool = False
    calls: list[str] = field(default_factory=list)

    def fetch(self, video_id: str, languages: Iterable[str] = ("en",)) -> FetchedTranscript:
        self.calls.append(video_id)
        segments = [] if self.no_segments else fixture_packet()["transcript"]["segments"]
        return FetchedTranscript(
            snippets=[
                FetchedTranscriptSnippet(
                    text=seg["text"] + self.text_suffix,
                    start=seg["start"],
                    duration=seg["duration"],
                )
                for seg in segments
            ],
            video_id=video_id,
            language="English (auto-generated)",
            language_code="en",
            is_generated=True,
        )


def fake_ytdlp(url: str) -> dict[str, Any]:
    """yt-dlp's JSON for the fixture video: title, channel, duration and chapters."""

    packet = fixture_packet()
    return {
        "id": VIDEO,
        "title": packet["title"],
        "channel": packet["channel"],
        "duration": packet["duration_s"],
        "chapters": [
            {
                "title": chapter["title"],
                "start_time": float(chapter["start_s"]),
                "end_time": float(chapter["end_s"]),
            }
            for chapter in packet["chapters"]
        ],
    }


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(connection, "utc_now", lambda: NOW)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(youtube, "make_transcript_client", lambda: fake)
    monkeypatch.setattr(youtube, "run_ytdlp", fake_ytdlp)
    return fake


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str], client: FakeClient) -> Path:
    """A data dir after ``init`` and ``fetch`` of the fixture video."""

    target = tmp_path / "data"
    code, _, err = run(capsys, "--data-dir", str(target), "init")
    assert code == 0, err
    code, out, err = run(capsys, "--data-dir", str(target), "fetch", WATCH)
    assert code == 0, err
    assert f"packet_sha256: {sha256_of(PACKET_FIXTURE)}" in out.splitlines()
    return target


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tips_line(*counts: int) -> str:
    """The summary's ``tips:`` line for these per-bucket counts, in ``BUCKET_IDS`` order."""

    per_bucket = ", ".join(
        f"bucket {bucket}: {count}" for bucket, count in zip(limits.BUCKET_IDS, counts, strict=True)
    )
    return f"tips: {sum(counts)} {limits.TIP_STATUS_PENDING} ({per_bucket})"


def get_context(
    capsys: pytest.CaptureFixture[str], data_dir: Path, ref: str | None = REF
) -> dict[str, Any]:
    argv = ["--data-dir", str(data_dir), "context"]
    if ref is not None:
        argv += ["--source-ref", ref]
    code, out, err = run(capsys, *argv)
    assert code == 0, err
    assert err == [f"data_dir: {data_dir}"]
    payload: dict[str, Any] = json.loads(out)
    return payload


def proposal_from(ctx: dict[str, Any]) -> dict[str, Any]:
    """The valid fixture proposal with the fingerprints ``context`` printed."""

    document = fixture_proposal()
    assert document["packet_sha256"] == ctx["source"]["packet_sha256"]  # the fixture is coherent
    document["skill_sha256"] = ctx["skill_sha256"]
    return document


def draft_path(data_dir: Path) -> Path:
    return data_dir / "proposals" / f"draft-{paths.source_key(REF)}.json"


def archive_file(data_dir: Path, ref: str = REF) -> Path:
    return data_dir / "proposals" / f"{paths.source_key(ref)}.1.json"


def write_draft(data_dir: Path, document: Any, *, encoding: str = "utf-8") -> Path:
    """Write the draft where the skill writes it: ``proposals/draft-<source-key>.json``."""

    target = draft_path(data_dir)
    target.write_bytes(json.dumps(document, ensure_ascii=False, indent=2).encode(encoding))
    return target


def propose(
    capsys: pytest.CaptureFixture[str], data_dir: Path, file: Path
) -> tuple[int, str, list[str]]:
    return run(capsys, "--data-dir", str(data_dir), "propose", str(file))


def dump(data_dir: Path) -> list[str]:
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        return list(conn.iterdump())


def proposals_listing(data_dir: Path) -> list[str]:
    return sorted(path.name for path in (data_dir / "proposals").iterdir())


def query(data_dir: Path, sql: str, *params: Any) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def source_row(data_dir: Path, ref: str = REF) -> dict[str, Any]:
    rows = query(data_dir, "SELECT * FROM source WHERE ref = ?", ref)
    assert len(rows) == 1
    return rows[0]


def add_manual_tip(data_dir: Path, *, list_ids: Iterable[int] = ()) -> int:
    """Store one approved manual tip ("Brush your teeth") through the production store."""

    with closing(connection.connect(data_dir / "checklist-care.db")) as conn:
        manual = source_store.get_by_ref(conn, seed.MANUAL_SOURCE_REF)
        assert manual is not None
        with connection.transaction(conn):
            tip_id = tip_store.insert_tip(
                conn,
                tip_store.NewTip(
                    name="Brush Your Teeth",
                    action="Brush for two minutes",
                    reason=None,
                    bucket=limits.BUCKET_DAILY,
                    audience=limits.AUDIENCE_KIDS_PLUS,
                ),
                source_id=manual.id,
                status=limits.TIP_STATUS_APPROVED,
                now=NOW_TEXT,
            )
            for position, list_id in enumerate(list_ids, start=1):
                conn.execute(
                    "INSERT INTO list_item (list_id, tip_id, position) VALUES (?, ?, ?)",
                    (list_id, tip_id, position),
                )
    return tip_id


# --- the fixture packet is what fetch writes ------------------------------------------


def test_fetch_with_the_fixture_seams_writes_the_fixture_packet_byte_for_byte(
    data_dir: Path,
) -> None:
    written = data_dir / "packets" / f"{paths.source_key(REF)}.json"
    assert written.read_bytes() == PACKET_FIXTURE.read_bytes()
    assert fixture_proposal()["packet_sha256"] == sha256_of(PACKET_FIXTURE)


def test_the_fixture_packet_carries_the_real_chapters_and_duration() -> None:
    packet = fixture_packet()
    assert packet["source_ref"] == REF
    assert packet["duration_s"] == 563
    assert [chapter["title"] for chapter in packet["chapters"]] == [
        "The Accountability Text",
        "Your Alarm Is Lying To You",
        "Make Laziness Your Superpower",
        "The Sunlight Stare",
        "Sit Up Straight, You're Not Dead Yet",
        "The Golden Rule of Habit-Building",
        "Fire Your Morning Brain",
        "The Two-Minute Brain Trick",
        "Unfollow Your Ghosts",
        "Your Brain Is A Raisin",
    ]
    starts = [chapter["start_s"] for chapter in packet["chapters"]]
    assert starts == [0, 60, 112, 170, 218, 276, 332, 389, 443, 503]
    assert [chapter["end_s"] for chapter in packet["chapters"]] == [*starts[1:], 563]
    assert all(
        seg["text"].startswith("synthetic transcript") for seg in packet["transcript"]["segments"]
    )


# --- context ---------------------------------------------------------------------------


def test_context_without_a_source_ref_has_limits_buckets_lists_and_no_source(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir, ref=None)

    assert list(ctx) == ["schema", "data_dir", "skill_sha256", "limits", "buckets", "lists", "tips"]
    assert ctx["schema"] == "checklist-care.context/1"
    assert ctx["data_dir"] == str(data_dir)
    assert ctx["skill_sha256"] == context.skill_sha256()
    assert ctx["limits"] == {
        "NAME_MAX": limits.NAME_MAX,
        "ACTION_MAX": limits.ACTION_MAX,
        "LINE_MAX": limits.LINE_MAX,
        "REASON_MAX": limits.REASON_MAX,
        "TIPS_MAX_EXTRACT": limits.TIPS_MAX_EXTRACT,
        "TIPS_MAX_INVESTIGATE": limits.TIPS_MAX_INVESTIGATE,
    }
    assert [(b["id"], b["title"]) for b in ctx["buckets"]] == [
        (limits.BUCKET_DAILY, "Daily checklist"),
        (limits.BUCKET_OCCASIONAL, "Occasional activity"),
        (limits.BUCKET_OPTIONAL, "Optional / when time allows"),
        (limits.BUCKET_LEFT_OUT, "Left out"),
    ]
    assert all(b["rubric"] for b in ctx["buckets"])
    assert ctx["lists"][6] == {
        "id": 7,
        "place": "Coffee Machine",
        "audience": limits.AUDIENCE_ADULTS,
        "cue": "First coffee or hot drink of the day",
    }
    assert [item["id"] for item in ctx["lists"]] == [1, 2, 3, 4, 5, 6, 7]
    assert ctx["tips"] == []


def test_context_reads_limits_from_the_limits_module_at_call_time(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Red anchor for "limits.py is the only definition": a changed constant reaches context.
    monkeypatch.setattr(limits, "NAME_MAX", limits.NAME_MAX + 1)
    assert get_context(capsys, data_dir, ref=None)["limits"]["NAME_MAX"] == limits.NAME_MAX


def test_context_with_a_source_ref_describes_the_fetched_packet(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    packet = fixture_packet()
    packet_file = data_dir / "packets" / f"{paths.source_key(REF)}.json"

    assert ctx["source"] == {
        "ref": REF,
        "kind": limits.SOURCE_KIND_YOUTUBE,
        "packet_path": str(packet_file),
        "packet_sha256": sha256_of(packet_file),
        "duration_s": 563,
        "chapters": packet["chapters"],
        "section_headings": [],
    }


def test_context_lists_comparable_tips_with_their_lists(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    manual_id = add_manual_tip(data_dir, list_ids=[3, 1])
    # A bucket-4 duplicate is not compared against; a bucket-4 rejected idea is.
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        conn.execute(
            "INSERT INTO tip (name, action, reason, bucket, audience, source_id, duplicate_of, "
            "created_at, updated_at) VALUES ('Dup', 'Do it', 'r', ?, ?, 1, ?, ?, ?)",
            (limits.BUCKET_LEFT_OUT, limits.AUDIENCE_ADULTS, manual_id, NOW_TEXT, NOW_TEXT),
        )
        conn.execute(
            "INSERT INTO tip (name, action, reason, bucket, audience, source_id, "
            "created_at, updated_at) VALUES ('Rejected', 'Skip it', 'r', ?, ?, 1, ?, ?)",
            (limits.BUCKET_LEFT_OUT, limits.AUDIENCE_ADULTS, NOW_TEXT, NOW_TEXT),
        )
        conn.commit()

    tips = get_context(capsys, data_dir, ref=None)["tips"]

    assert tips == [
        {
            "id": manual_id,
            "name": "Brush Your Teeth",
            "action": "Brush for two minutes",
            "bucket": limits.BUCKET_DAILY,
            "audience": limits.AUDIENCE_KIDS_PLUS,
            "status": limits.TIP_STATUS_APPROVED,
            "source_ref": seed.MANUAL_SOURCE_REF,
            "list_ids": [1, 3],
        },
        {
            "id": manual_id + 2,
            "name": "Rejected",
            "action": "Skip it",
            "bucket": limits.BUCKET_LEFT_OUT,
            "audience": limits.AUDIENCE_ADULTS,
            "status": limits.TIP_STATUS_PENDING,
            "source_ref": seed.MANUAL_SOURCE_REF,
            "list_ids": [],
        },
    ]


def test_context_for_an_unfetched_source_exits_2(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code, out, err = run(
        capsys, "--data-dir", str(data_dir), "context", "--source-ref", "youtube:EXAMPLE0001"
    )
    assert code == 2
    assert out == ""
    assert err[1].startswith("CC_E_CONFIG: no source youtube:EXAMPLE0001")


def test_context_without_a_database_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, err = run(capsys, "--data-dir", str(tmp_path / "empty"), "context")
    assert code == 2
    assert out == ""
    assert "run `checklist-care init` first" in err[1]


# --- the round trip ----------------------------------------------------------------------


def test_round_trip_imports_pending_tips_with_placements_and_archives_the_proposal(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    draft = write_draft(data_dir, proposal_from(ctx))

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 0, err
    assert err == [f"data_dir: {data_dir}"]
    lines = out.splitlines()
    assert lines == [
        f"proposal: {archive_file(data_dir)}",
        f"source_ref: {REF}",
        f"mode: {limits.SOURCE_MODE_EXTRACT}",
        tips_line(5, 2, 2, 1),
        "duplicates: 0",
        "possible duplicates: 0",
        "unplaced daily tips: 1 (#8)",
        REVIEW_LINE,
    ]
    assert lines[-1] == REVIEW_LINE

    tips = query(data_dir, "SELECT * FROM tip ORDER BY id")
    expected = fixture_proposal()["tips"]
    assert len(tips) == 10
    for row, tip in zip(tips, expected, strict=True):
        assert row["status"] == limits.TIP_STATUS_PENDING
        assert row["source_id"] == source_row(data_dir)["id"]
        for name in (
            "name",
            "action",
            "reason",
            "bucket",
            "audience",
            "start_s",
            "locator",
            "evidence_url",
            "duplicate_of",
            "possible_duplicate_of",
            "note",
        ):
            assert row[name] == tip[name], name
        assert row["created_at"] == row["updated_at"] == NOW_TEXT
    assert tips[3]["locator"] == "The Sunlight Stare"  # a renamed chapter keeps its title

    placements = query(
        data_dir, "SELECT list_id, tip_id, position FROM list_item ORDER BY tip_id, list_id"
    )
    assert placements == [
        {"list_id": 5, "tip_id": 4, "position": 1},
        {"list_id": 6, "tip_id": 4, "position": 1},
        {"list_id": 2, "tip_id": 5, "position": 1},
        {"list_id": 1, "tip_id": 7, "position": 1},
        {"list_id": 3, "tip_id": 10, "position": 1},
        {"list_id": 7, "tip_id": 10, "position": 1},
    ]

    source = source_row(data_dir)
    assert source["reviewed_at"] == NOW_TEXT
    assert source["mode"] == limits.SOURCE_MODE_EXTRACT
    assert archive_file(data_dir).read_bytes() == draft.read_bytes()
    assert proposals_listing(data_dir) == sorted([draft.name, archive_file(data_dir).name])


def test_after_propose_context_shows_the_new_tips_and_fetch_refuses_the_source(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    assert propose(capsys, data_dir, write_draft(data_dir, proposal_from(ctx)))[0] == 0

    tips = get_context(capsys, data_dir, ref=None)["tips"]
    assert [tip["id"] for tip in tips] == list(range(1, 11))  # bucket 4 #3 is no duplicate
    assert tips[3]["list_ids"] == [5, 6]
    assert {tip["source_ref"] for tip in tips} == {REF}

    code, out, err = run(capsys, "--data-dir", str(data_dir), "fetch", f"{WATCH}&t=10s")
    assert code == 3
    assert out == ""
    assert err[1].startswith("CC_ALREADY_REVIEWED:")


def test_placements_are_appended_after_existing_list_items(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    add_manual_tip(data_dir, list_ids=[7])
    ctx = get_context(capsys, data_dir)

    assert propose(capsys, data_dir, write_draft(data_dir, proposal_from(ctx)))[0] == 0

    assert query(data_dir, "SELECT tip_id, position FROM list_item WHERE list_id = 7") == [
        {"tip_id": 1, "position": 1},
        {"tip_id": 11, "position": 2},
    ]


def test_duplicates_and_possible_duplicates_are_stored_and_summarized(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    manual_id = add_manual_tip(data_dir)
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    document["tips"][9].update(
        bucket=limits.BUCKET_LEFT_OUT, list_ids=[], duplicate_of=manual_id, note="duplicate of #1"
    )
    document["tips"][6]["possible_duplicate_of"] = manual_id

    code, out, err = propose(capsys, data_dir, write_draft(data_dir, document))

    assert code == 0, err
    assert "duplicates: 1 (#11 of #1)" in out.splitlines()
    assert "possible duplicates: 1 (#8 near #1)" in out.splitlines()
    rows = query(data_dir, "SELECT id, duplicate_of, possible_duplicate_of FROM tip WHERE id > 1")
    assert rows[9] == {"id": 11, "duplicate_of": manual_id, "possible_duplicate_of": None}
    assert rows[6] == {"id": 8, "duplicate_of": None, "possible_duplicate_of": manual_id}


def test_validate_accepts_the_fixture_in_process_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    before = dump(data_dir)
    with closing(connection.connect(data_dir / "checklist-care.db")) as conn:
        valid = proposal.validate(proposal_from(ctx), conn, data_dir)
    assert valid.source.ref == REF
    assert valid.mode == limits.SOURCE_MODE_EXTRACT
    assert [item.tip.name for item in valid.tips] == [t["name"] for t in fixture_proposal()["tips"]]
    assert valid.tips[3].list_ids == (5, 6)
    assert dump(data_dir) == before


# --- rejected proposals write nothing ---------------------------------------------------


Mutation = Callable[[dict[str, Any], dict[str, Any], int], None]
"""Changes a valid proposal in place, given the context and an existing tip id."""


def _set_tip(index: int, **values: Any) -> Mutation:
    def mutate(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
        document["tips"][index].update(values)

    return mutate


def _over_length_name(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
    document["tips"][0]["name"] = "N" * (ctx["limits"]["NAME_MAX"] + 1)


def _line_max_breach(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
    # Name and action are each at their own limit, so only the line rule fails.
    document["tips"][7]["name"] = "N" * ctx["limits"]["NAME_MAX"]
    document["tips"][7]["action"] = "A" * ctx["limits"]["ACTION_MAX"]
    assert limits.line_length("N" * limits.NAME_MAX, "A" * limits.ACTION_MAX) > limits.LINE_MAX


def _duplicate_of_existing(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
    document["tips"][8]["duplicate_of"] = existing  # tip 8 is bucket 2


def _drop_start(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
    del document["tips"][1]["start_s"]


def _stale_packet(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
    document["packet_sha256"] = hashlib.sha256(b"an older packet").hexdigest()


def _other_skill(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
    document["skill_sha256"] = hashlib.sha256(b"an edited skill").hexdigest()
    assert document["skill_sha256"] != ctx["skill_sha256"]


def _set_envelope(**values: Any) -> Mutation:
    def mutate(document: dict[str, Any], ctx: dict[str, Any], existing: int) -> None:
        document.update(values)

    return mutate


# The Done-when cases of plan Step 3. Every other rule, and the boundary variants of
# these, is in the REJECTED table of tests/unit/test_proposal_validate.py.
INVALID_CASES: list[tuple[str, Mutation, int | None, str]] = [
    ("over-length name", _over_length_name, 0, "name"),
    ("LINE_MAX breach", _line_max_breach, 7, "action"),
    ("adults tip on a kids list", _set_tip(6, list_ids=[3]), 6, "list_ids"),
    ("list_ids on a bucket-2 tip", _set_tip(0, list_ids=[6]), 0, "list_ids"),
    ("duplicate_of with bucket 2", _duplicate_of_existing, 8, "duplicate_of"),
    ("unknown duplicate_of", _set_tip(2, duplicate_of=999), 2, "duplicate_of"),
    ("start_s equal to duration_s", _set_tip(9, start_s=563), 9, "start_s"),
    ("start_s missing for a chaptered video", _drop_start, 1, "start_s"),
    ("stale packet_sha256", _stale_packet, None, "packet_sha256"),
    ("skill_sha256 other than context's", _other_skill, None, "skill_sha256"),
    ("bad host", _set_envelope(host="chatgpt"), None, "host"),
]


@pytest.mark.parametrize(
    ("mutate", "index", "field_name"),
    [pytest.param(m, i, f, id=name) for name, m, i, f in INVALID_CASES],
)
def test_an_invalid_proposal_exits_5_with_one_field_problem_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    mutate: Mutation,
    index: int | None,
    field_name: str,
) -> None:
    existing = add_manual_tip(data_dir)
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    mutate(document, ctx, existing)
    draft = write_draft(data_dir, document)
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 5, err
    problems = json.loads(out)
    assert [(p["index"], p["field"]) for p in problems] == [(index, field_name)], problems
    assert isinstance(problems[0]["problem"], str) and problems[0]["problem"]
    assert err[0] == f"data_dir: {data_dir}"
    assert err[1].startswith("CC_E_PROPOSAL: the proposal has 1 problem,")
    assert "nothing was written" in err[1]
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing
    assert source_row(data_dir)["reviewed_at"] is None


def test_every_problem_is_reported_in_one_run(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    document["host"] = "chatgpt"
    document["tips"][0]["name"] = "N" * (limits.NAME_MAX + 1)
    document["tips"][2].update(action="", duplicate_of=999)  # two problems on one tip
    document["tips"][6]["list_ids"] = [3]
    draft = write_draft(data_dir, document)
    before = dump(data_dir)

    code, out, _ = propose(capsys, data_dir, draft)

    assert code == 5
    assert [(p["index"], p["field"]) for p in json.loads(out)] == [
        (None, "host"),
        (0, "name"),
        (2, "action"),
        (6, "list_ids"),
        (2, "duplicate_of"),  # tip references are checked in one query, after every tip
    ]
    assert dump(data_dir) == before


def test_problem_texts_name_the_limit_and_the_rule(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    document["tips"][0]["name"] = "N" * (limits.NAME_MAX + 1)
    document["tips"][6]["list_ids"] = [3]
    document["tips"][9]["start_s"] = 563
    code, out, _ = propose(capsys, data_dir, write_draft(data_dir, document))

    assert code == 5
    texts = [p["problem"] for p in json.loads(out)]
    assert texts[0] == (
        f"is {limits.NAME_MAX + 1} characters; NAME_MAX is {limits.NAME_MAX}; shorten it"
    )
    assert texts[1] == (
        f"list 3 (Kids Bathroom{limits.LIST_TITLE_SEPARATOR}Kids) is a {limits.AUDIENCE_KIDS} "
        f"list and accepts only {limits.AUDIENCE_KIDS_PLUS} tips; "
        f"this tip is {limits.AUDIENCE_ADULTS}"
    )
    assert texts[2] == "is 563; it must be 0 <= start_s < duration_s (563)"


def _proposal_for_a_packet_without_duration(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Fetch the video again with no transcript segments while yt-dlp fails, so the
    packet has neither ``duration_s`` nor chapters; the valid proposal for that packet."""

    def ytdlp_fails(url: str) -> dict[str, Any]:
        raise youtube.YtDlpError("exit 1: ERROR: [youtube] Sign in to confirm")

    client.no_segments = True
    monkeypatch.setattr(youtube, "run_ytdlp", ytdlp_fails)
    code, _, err = run(capsys, "--data-dir", str(data_dir), "fetch", WATCH)
    assert code == 0, err
    ctx = get_context(capsys, data_dir)
    assert ctx["source"]["duration_s"] is None
    assert ctx["source"]["chapters"] == []
    document = fixture_proposal()
    document["packet_sha256"] = ctx["source"]["packet_sha256"]
    document["skill_sha256"] = ctx["skill_sha256"]
    return document


@pytest.mark.parametrize(
    "start_s",
    [
        pytest.param(limits.SQLITE_INTEGER_MAX + 1, id="beyond SQLite INTEGER"),
        pytest.param(limits.START_S_MAX + 1, id="START_S_MAX + 1"),
    ],
)
def test_a_start_s_over_start_s_max_exits_5_when_the_packet_has_no_duration(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    start_s: int,
) -> None:
    document = _proposal_for_a_packet_without_duration(capsys, data_dir, client, monkeypatch)
    document["tips"][9]["start_s"] = start_s
    draft = write_draft(data_dir, document)
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 5, err  # not 9: an int SQLite cannot store never reaches insert_tip
    problems = json.loads(out)
    assert [(p["index"], p["field"]) for p in problems] == [(9, "start_s")]
    assert "START_S_MAX" in problems[0]["problem"]
    assert err[1].startswith("CC_E_PROPOSAL: the proposal has 1 problem,")
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing
    assert source_row(data_dir)["reviewed_at"] is None


def test_start_s_max_is_accepted_when_the_packet_has_no_duration(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _proposal_for_a_packet_without_duration(capsys, data_dir, client, monkeypatch)
    document["tips"][9]["start_s"] = limits.START_S_MAX

    code, _, err = propose(capsys, data_dir, write_draft(data_dir, document))

    assert code == 0, err
    assert query(data_dir, "SELECT start_s FROM tip WHERE id = 10") == [
        {"start_s": limits.START_S_MAX}
    ]


def test_a_packet_fetched_again_after_context_makes_the_proposal_stale(
    capsys: pytest.CaptureFixture[str], data_dir: Path, client: FakeClient
) -> None:
    ctx = get_context(capsys, data_dir)
    client.text_suffix = " (a newer caption track)"
    code, _, err = run(capsys, "--data-dir", str(data_dir), "fetch", WATCH)
    assert code == 0, err
    draft = write_draft(data_dir, proposal_from(ctx))
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, _ = propose(capsys, data_dir, draft)

    assert code == 5
    problems = json.loads(out)
    assert [(p["index"], p["field"]) for p in problems] == [(None, "packet_sha256")]
    assert "context --source-ref" in problems[0]["problem"]
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing


def test_a_skill_edited_after_context_makes_the_proposal_stale(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    core = checkout / context.SKILL_FILES[0]
    core.parent.mkdir(parents=True)
    core.write_bytes(b"# checklist-ingest core\n")
    monkeypatch.setattr(context, "REPO_ROOT", checkout)
    ctx = get_context(capsys, data_dir)
    core.write_bytes(b"# checklist-ingest core, edited\n")
    draft = write_draft(data_dir, proposal_from(ctx))
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, _ = propose(capsys, data_dir, draft)

    assert code == 5
    assert [(p["index"], p["field"]) for p in json.loads(out)] == [(None, "skill_sha256")]
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing

    # The fingerprint context prints now is the one propose accepts.
    fresh = get_context(capsys, data_dir)
    assert fresh["skill_sha256"] != ctx["skill_sha256"]
    assert propose(capsys, data_dir, write_draft(data_dir, proposal_from(fresh)))[0] == 0


def test_the_unmodified_fixture_fails_only_on_its_placeholder_skill_sha256(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code, out, _ = propose(capsys, data_dir, write_draft(data_dir, fixture_proposal()))
    assert code == 5
    assert [(p["index"], p["field"]) for p in json.loads(out)] == [(None, "skill_sha256")]


# --- encodings --------------------------------------------------------------------------


def test_an_uppercase_packet_sha256_is_accepted(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    document["packet_sha256"] = document["packet_sha256"].upper()
    document["skill_sha256"] = document["skill_sha256"].upper()
    assert document["packet_sha256"] != ctx["source"]["packet_sha256"]

    code, out, err = propose(capsys, data_dir, write_draft(data_dir, document))

    assert code == 0, err
    assert out.splitlines()[-1] == REVIEW_LINE
    assert source_row(data_dir)["reviewed_at"] == NOW_TEXT


def test_a_bom_prefixed_utf8_proposal_is_accepted(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    draft = write_draft(data_dir, proposal_from(ctx), encoding="utf-8-sig")
    assert draft.read_bytes().startswith(b"\xef\xbb\xbf")

    code, _, err = propose(capsys, data_dir, draft)

    assert code == 0, err
    assert len(query(data_dir, "SELECT id FROM tip")) == 10
    assert archive_file(data_dir).read_bytes() == draft.read_bytes()  # the file, copied


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_a_utf16_proposal_gets_the_save_as_utf8_problem(
    capsys: pytest.CaptureFixture[str], data_dir: Path, encoding: str
) -> None:
    ctx = get_context(capsys, data_dir)
    draft = write_draft(data_dir, proposal_from(ctx), encoding=encoding)
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 5
    problems = json.loads(out)
    assert [(p["index"], p["field"]) for p in problems] == [(None, "file")]
    assert "save as UTF-8" in problems[0]["problem"]
    assert "JSON" not in problems[0]["problem"]
    assert err[1].startswith("CC_E_PROPOSAL:")
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'{"schema": ', "the file is not valid JSON"),
        (b"[]", "the proposal must be one JSON object"),
        (b'{"host": "codex", "host": "codex"}', "the key 'host' appears twice"),
        (b'{"degraded": NaN}', "NaN is not a JSON number"),
        ('{"model": "caf\xe9"}'.encode("cp1252"), "save as UTF-8"),
    ],
    ids=["truncated", "not an object", "duplicate key", "NaN", "cp1252"],
)
def test_an_undecodable_file_exits_5_with_a_file_problem(
    capsys: pytest.CaptureFixture[str], data_dir: Path, data: bytes, expected: str
) -> None:
    draft = draft_path(data_dir)
    draft.write_bytes(data)
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, _ = propose(capsys, data_dir, draft)

    assert code == 5
    problems = json.loads(out)
    assert [(p["index"], p["field"]) for p in problems] == [(None, "file")]
    assert expected in problems[0]["problem"]
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing


def test_a_missing_proposal_file_exits_5(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code, out, _ = propose(capsys, data_dir, data_dir / "proposals" / "nowhere.json")
    assert code == 5
    assert [(p["index"], p["field"]) for p in json.loads(out)] == [(None, "file")]


def test_a_file_over_the_size_cap_exits_5_without_being_decoded(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    draft = write_draft(data_dir, proposal_from(ctx))
    # A valid proposal, padded with JSON whitespace past the cap.
    padding = limits.PROPOSAL_FILE_MAX_BYTES + 1 - draft.stat().st_size
    draft.write_bytes(draft.read_bytes() + b" " * padding)
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 5, err
    problems = json.loads(out)
    assert [(p["index"], p["field"]) for p in problems] == [(None, "file")]
    assert "PROPOSAL_FILE_MAX_BYTES" in problems[0]["problem"]
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing
    assert source_row(data_dir)["reviewed_at"] is None


def _surrogate_in_name(document: dict[str, Any]) -> None:
    document["tips"][0]["name"] = "Sun \ud800 Stare"


def _surrogate_in_source_ref(document: dict[str, Any]) -> None:
    document["source_ref"] = "youtube:\udc00"


def _surrogate_in_a_tip_key(document: dict[str, Any]) -> None:
    document["tips"][4]["\ud800x"] = None


@pytest.mark.parametrize(
    ("mutate", "index", "field_name"),
    [
        pytest.param(_surrogate_in_name, 0, "name", id="tip name"),
        pytest.param(_surrogate_in_source_ref, None, "source_ref", id="source_ref"),
        pytest.param(_surrogate_in_a_tip_key, 4, "\\ud800x", id="tip key"),
    ],
)
def test_an_unpaired_surrogate_escape_exits_5_on_its_field(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    mutate: Callable[[dict[str, Any]], None],
    index: int | None,
    field_name: str,
) -> None:
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    mutate(document)
    draft = draft_path(data_dir)
    # json.dumps writes a lone surrogate as the six-character escape \udXXX, as a host could.
    draft.write_bytes(json.dumps(document).encode("ascii"))
    assert b"\\ud" in draft.read_bytes()
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 5, err  # not 9: nothing stores, queries or prints the surrogate
    problems = json.loads(out)
    assert [(p["index"], p["field"]) for p in problems] == [(index, field_name)]
    assert "unpaired surrogate" in problems[0]["problem"]
    assert err[1].startswith("CC_E_PROPOSAL: the proposal has 1 problem,")
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing
    assert source_row(data_dir)["reviewed_at"] is None


# --- empty, reviewed and failed imports -------------------------------------------------


def test_an_empty_tips_list_marks_the_source_reviewed_with_no_tips(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    document["tips"] = []

    code, out, err = propose(capsys, data_dir, write_draft(data_dir, document))

    assert code == 0, err
    assert tips_line(0, 0, 0, 0) in out.splitlines()
    assert out.splitlines()[-1] == REVIEW_LINE
    assert query(data_dir, "SELECT count(*) AS n FROM tip") == [{"n": 0}]
    assert source_row(data_dir)["reviewed_at"] == NOW_TEXT
    assert archive_file(data_dir).is_file()


def test_reproposing_a_reviewed_source_exits_3_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    draft = write_draft(data_dir, proposal_from(ctx))
    assert propose(capsys, data_dir, draft)[0] == 0
    archived = archive_file(data_dir).read_bytes()
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 3
    assert out == ""
    assert err[1].startswith(f"CC_ALREADY_REVIEWED: {REF} was already reviewed")
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing
    assert archive_file(data_dir).read_bytes() == archived


def test_a_reviewed_source_exits_3_even_when_the_proposal_has_other_problems(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    document = proposal_from(ctx)
    assert propose(capsys, data_dir, write_draft(data_dir, document))[0] == 0
    document["host"] = "chatgpt"

    code, _, _ = propose(capsys, data_dir, write_draft(data_dir, document))

    assert code == 3


class CommitFails:
    """A real connection whose ``COMMIT`` fails, as on a full disk or an I/O error."""

    def __init__(self, conn: sqlite3.Connection, on_commit: Callable[[], None]) -> None:
        self._conn = conn
        self._on_commit = on_commit

    def execute(self, sql: str, *params: Any) -> sqlite3.Cursor:
        if sql.strip().upper() == "COMMIT":
            self._on_commit()
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_a_failed_commit_removes_the_archive_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = get_context(capsys, data_dir)
    draft = write_draft(data_dir, proposal_from(ctx))
    before, listing = dump(data_dir), proposals_listing(data_dir)
    seen_at_commit: list[bool] = []
    real_connect = connection.connect
    monkeypatch.setattr(
        connection,
        "connect",
        lambda path: CommitFails(
            real_connect(path), lambda: seen_at_commit.append(archive_file(data_dir).exists())
        ),
    )

    code, out, err = propose(capsys, data_dir, draft)

    assert code == 9
    assert out == ""
    assert err[1] == "CC_E_INTERNAL: unexpected OperationalError: disk I/O error"
    assert seen_at_commit == [True]  # the archive was in place at COMMIT (not vacuous)
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing
    assert source_row(data_dir)["reviewed_at"] is None


def test_an_archive_left_for_an_unreviewed_source_is_kept_and_exits_2(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    ctx = get_context(capsys, data_dir)
    draft = write_draft(data_dir, proposal_from(ctx))
    archive_file(data_dir).write_bytes(b"an archive from a restored backup")
    before, listing = dump(data_dir), proposals_listing(data_dir)

    code, _, err = propose(capsys, data_dir, draft)

    assert code == 2
    assert err[1].startswith("CC_E_CONFIG:")
    assert "already exists" in err[1]
    assert archive_file(data_dir).read_bytes() == b"an archive from a restored backup"
    assert dump(data_dir) == before
    assert proposals_listing(data_dir) == listing


def test_propose_without_a_database_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    file = tmp_path / "proposal.json"
    file.write_bytes(PROPOSAL_FIXTURE.read_bytes())
    code, out, err = run(capsys, "--data-dir", str(tmp_path / "empty"), "propose", str(file))
    assert code == 2
    assert out == ""
    assert not (tmp_path / "empty").exists()


def test_both_subcommands_are_in_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "context", "--help")
    assert code == 0
    assert "--source-ref REF" in out
    code, out, _ = run(capsys, "propose", "--help")
    assert code == 0
    assert "FILE" in out
