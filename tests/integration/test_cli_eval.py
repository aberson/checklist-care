"""``checklist-care eval score`` through ``cli.main`` (plan §4.8, Step 15).

Every scored data dir is built by the production path: ``init``, then ``fetch`` of the
two in-sample videos with the network seams faked from the fixture packets
(``packet_RPK-xw3SrVw_synthetic.json`` and ``packet_youtube_synthetic.json``: the real
chapter titles and timings, synthetic transcript text), then ``context`` and
``propose``. Every run is scored against the **committed**
``eval/labels/in_sample.json``, so a drift between a label title and a fixture title
aborts inside the gate.

Calibration (plan §4.8):

* the known-good runs A (RPK-xw3SrVw first) and B (LpeDuC2sQ-w first) pass;
* one known-garbage run per condition the plan names for a scored hard IS criterion
  fails exactly that criterion: IS-2 for each of its three conditions, IS-3 for each of its
  four labelled chapters, IS-4 for each part of each chapter's placement, IS-5 for each
  part of the pair's verdict in both ingest orders, IS-6 also for the pair the plan names
  (The Doorway Posture Check as a duplicate of Sit Up Straight) and IS-8 for a name and
  an action. IS-1 and IS-7 cannot be broken through ``propose`` and the triggers, so
  their garbage runs tamper with a copy of a good run's data dir (IS-1's mode and a
  repeated ref; each IS-7 placement invariant and each ``limits.py`` limit of a stored
  tip field), with the archived proposal edited to match so the operator-edit abort does
  not fire first;
* each ABORT condition that applies to YouTube sources exits 6, and so do a stale
  ``skill_sha256``, a data dir that came from neither the flag nor the env var and one
  that cannot be used (an empty value, a git checkout), where other commands exit 2.
  An edit made in the database after the import aborts, an archived proposal or a
  cached packet that is over its cap or not a regular file aborts unread, and scoring
  leaves the data dir as it found it.

A chapter title holding control characters cannot add or rewrite a printed line, and
neither can a labels key in an abort line or an exception's text in the crash line.
Without hard links the score file is still written, and still never over another.

The rule details only in-memory inputs reach (every labels-file defect, the tip scored
in a crowded window, each edited field, a packet with no duration) are asserted in
``tests/unit/test_scorer_rules.py``; this module asserts the end-to-end behavior through
``cli.main``.

The tip texts below are self-written for these tests; the names that equal chapter
titles come from the public chapter titles (plan Appendix F), never from transcripts.
"""

from __future__ import annotations

import errno
import io
import itertools
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from youtube_transcript_api import FetchedTranscript, FetchedTranscriptSnippet

from checklist_care import cli, context, paths, proposal, strict_json
from checklist_care.db import connection
from checklist_care.errors import (
    CODE_CONFIG,
    CODE_DATA_IN_REPO,
    CODE_EVAL_ABORT,
    CODE_INTERNAL,
    EXIT_CONFIG,
    EXIT_EVAL_ABORT,
    EXIT_EVAL_FAIL,
    EXIT_OK,
)
from checklist_care.limits import (
    ACTION_MAX,
    EVIDENCE_URL_MAX,
    HOST_CLAUDE_CODE,
    HOST_CODEX,
    ID_MAX,
    LINE_MAX,
    LOCATOR_MAX,
    NAME_MAX,
    NOTE_MAX,
    PROPOSAL_FILE_MAX_BYTES,
    REASON_MAX,
    SOURCE_MODE_EXTRACT,
    SOURCE_MODE_INVESTIGATE,
    START_S_MAX,
    TIP_STATUS_PENDING,
)
from checklist_care.scoring import score
from checklist_care.sources import packet as packet_module
from checklist_care.sources import youtube
from checklist_care.store import lists as list_store
from checklist_care.store import sources as source_store
from checklist_care.store import tips as tip_store

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
LABELS = ROOT / "eval" / "labels" / "in_sample.json"

RPK = "RPK-xw3SrVw"
LPE = "LpeDuC2sQ-w"
PACKET_FIXTURES = {
    RPK: FIXTURES / "packet_RPK-xw3SrVw_synthetic.json",
    LPE: FIXTURES / "packet_youtube_synthetic.json",
}

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = connection.format_timestamp(NOW)
MODEL = "synthetic-model"
HARD_IS = ("IS-1", "IS-2", "IS-3", "IS-4", "IS-5", "IS-6", "IS-7", "IS-8")

LABELLED_CHAPTERS: dict[str, str] = {
    # The four chapters IS-3 and IS-4 name (plan §4.8), each with its video.
    "The Accountability Text": LPE,
    "Sit Up Straight, You're Not Dead Yet": LPE,
    "Fire Your Morning Brain": LPE,
    "The Three-Second Mirror Check": RPK,
}


def score_files(data_dir: Path, count: int = 1) -> list[Path]:
    """The first ``count`` names a score written at ``NOW`` takes, in order."""

    stem = f"{score.SCORE_FILE_PREFIX}{paths.file_stamp(NOW)}"
    names = paths.numbered_paths(data_dir / paths.EVAL_DIR, stem, ".json")
    return list(itertools.islice(names, count))


def fixture_packet(video: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(PACKET_FIXTURES[video].read_bytes().decode("utf-8"))
    return payload


# --- fake network seams, answering from the fixture packets ---------------------------


@dataclass
class FakeVideos:
    """The transcript client and yt-dlp, both answering from ``packets`` by video id.
    A test edits ``packets`` first to fetch a degraded video."""

    packets: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {video: fixture_packet(video) for video in PACKET_FIXTURES}
    )

    def fetch(self, video_id: str, languages: Iterable[str] = ("en",)) -> FetchedTranscript:
        segments = self.packets[video_id]["transcript"]["segments"]
        return FetchedTranscript(
            snippets=[
                FetchedTranscriptSnippet(
                    text=seg["text"], start=seg["start"], duration=seg["duration"]
                )
                for seg in segments
            ],
            video_id=video_id,
            language="English (auto-generated)",
            language_code="en",
            is_generated=True,
        )

    def ytdlp(self, url: str) -> dict[str, Any]:
        video = youtube.parse_video_id(url)
        packet = self.packets[video]
        return {
            "id": video,
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
def videos(monkeypatch: pytest.MonkeyPatch) -> FakeVideos:
    fake = FakeVideos()
    monkeypatch.setattr(youtube, "make_transcript_client", lambda: fake)
    monkeypatch.setattr(youtube, "run_ytdlp", fake.ytdlp)
    return fake


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def skill_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the four skill files that ``context.skill_sha256`` fingerprints, so a
    test can change the skill after a proposal, as an edit to the real skill would."""

    root = tmp_path / "skill-root"
    for relative in context.SKILL_FILES:
        source = ROOT / relative
        if source.is_file():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, root / relative)
    monkeypatch.setattr(context, "REPO_ROOT", root)
    return root


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


# --- the in-sample runs, through fetch, context and propose ----------------------------


REASON = "A small habit that is easy to keep, written for this synthetic test run."


@dataclass(frozen=True)
class TipSpec:
    """One proposed tip. ``duplicate_of`` and ``possible_duplicate_of`` name a stored
    tip, whose id ``context`` gives."""

    name: str
    action: str
    bucket: int
    audience: str
    start_s: int
    list_ids: tuple[int, ...] = ()
    locator: str | None = None
    duplicate_of: str | None = None
    possible_duplicate_of: str | None = None
    reason: str = REASON
    note: str | None = None
    evidence_url: str | None = None


def rpk_specs(*, first: bool) -> list[TipSpec]:
    """A known-good proposal for RPK-xw3SrVw; ``first`` when it is ingested first."""

    water_name, water_action = "Drink Water Before Coffee", "Have a glass of water before coffee"
    doorway_name, doorway_action = "The Doorway Posture Check", "Stand tall in every doorway"
    if first:
        water = TipSpec(water_name, water_action, 1, "adults", 293, (7,))
        doorway = TipSpec(doorway_name, doorway_action, 1, "kids+", 387, (5, 6))
    else:
        water = TipSpec(
            water_name, water_action, 4, "adults", 293, duplicate_of="Your Brain Is A Raisin"
        )
        doorway = TipSpec(
            doorway_name,
            doorway_action,
            1,
            "kids+",
            387,
            (5, 6),
            possible_duplicate_of="Sit Up Straight, You're Not Dead Yet",
        )
    return [
        TipSpec("The One-Color Outfit Trick", "Pick one color family per outfit", 3, "adults", 0),
        TipSpec(
            "Don't Carry Your Phone In Your Hand", "Pocket the phone when walking", 3, "adults", 84
        ),
        TipSpec(
            "The Clean Wallet Rule",
            "Empty old receipts out weekly",
            2,
            "adults",
            141,
            note="Occasional: a weekly tidy, not a daily step.",
            evidence_url="https://example.com/clean-wallet",
        ),
        TipSpec("Slow Down Your Walk", "Walk slower when you are not late", 3, "kids+", 203),
        TipSpec("The Ice Cube Dryer Trick", "Dry shirts with one ice cube", 3, "adults", 251),
        water,
        TipSpec("The One-Side Hair Tuck", "Tuck your hair behind one ear", 4, "adults", 346),
        doorway,
        TipSpec("Stop Overexplaining", "Give one short reason, then stop", 3, "adults", 434),
        TipSpec(
            "The Three-Second Mirror Check", "Mirror check before leaving", 1, "kids+", 485, (5, 6)
        ),
    ]


def lpe_specs(*, first: bool) -> list[TipSpec]:
    """A known-good proposal for LpeDuC2sQ-w; ``first`` when it is ingested first.

    The Two-Minute Brain Trick tip starts at 386 s, 3 s before its chapter boundary at
    389 s, so the run passes only if matching counts it toward that chapter."""

    raisin_name, raisin_action = "Your Brain Is A Raisin", "Drink water before anything else"
    sit_up_name, sit_up_action = "Sit Up Straight, You're Not Dead Yet", "Roll your shoulders back"
    if first:
        raisin = TipSpec(raisin_name, raisin_action, 1, "adults", 503, (7,))
        sit_up = TipSpec(sit_up_name, sit_up_action, 1, "kids+", 218, (2,))
    else:
        raisin = TipSpec(
            raisin_name, raisin_action, 4, "adults", 503, duplicate_of="Drink Water Before Coffee"
        )
        sit_up = TipSpec(
            sit_up_name,
            sit_up_action,
            1,
            "kids+",
            218,
            (2,),
            possible_duplicate_of="The Doorway Posture Check",
        )
    daylight = TipSpec(
        "Step Into Morning Daylight",
        "Get daylight on the way out",
        1,
        "kids+",
        170,
        (5, 6),
        locator="The Sunlight Stare",
    )
    return [
        TipSpec("The Accountability Text", "Text a friend this week's new habit", 2, "adults", 0),
        TipSpec("Your Alarm Is Lying To You", "Keep one fixed wake-up time", 3, "adults", 60),
        TipSpec(
            "Make Laziness Your Superpower", "Make the good choice the easy one", 3, "adults", 112
        ),
        daylight,
        sit_up,
        TipSpec(
            "The Golden Rule of Habit-Building", "Start habits too small to skip", 3, "adults", 276
        ),
        TipSpec(
            "Fire Your Morning Brain", "Splash cold water on your face", 1, "adults", 332, (1,)
        ),
        TipSpec(
            "The Two-Minute Brain Trick", "Do two minutes of a dull task", 1, "kids+", 386, (2,)
        ),
        TipSpec("Unfollow Your Ghosts", "Unfollow accounts that drain you", 2, "adults", 443),
        raisin,
    ]


def good_specs(first: str) -> dict[str, list[TipSpec]]:
    return {RPK: rpk_specs(first=first == RPK), LPE: lpe_specs(first=first == LPE)}


def changed(specs: list[TipSpec], target: str, /, **changes: Any) -> list[TipSpec]:
    """``specs`` with the one tip named ``target`` changed."""

    assert sum(spec.name == target for spec in specs) == 1
    return [replace(spec, **changes) if spec.name == target else spec for spec in specs]


def without(specs: list[TipSpec], name: str) -> list[TipSpec]:
    assert sum(spec.name == name for spec in specs) == 1
    return [spec for spec in specs if spec.name != name]


def init(capsys: pytest.CaptureFixture[str], data_dir: Path) -> None:
    code, _, err = run(capsys, "--data-dir", str(data_dir), "init")
    assert code == EXIT_OK, err


def fetch(capsys: pytest.CaptureFixture[str], data_dir: Path, video: str) -> None:
    code, _, err = run(capsys, "--data-dir", str(data_dir), "fetch", youtube.watch_url(video))
    assert code == EXIT_OK, err


def ingest(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    video: str,
    specs: list[TipSpec],
    *,
    host: str = HOST_CLAUDE_CODE,
    model: str = MODEL,
) -> None:
    """What the skill does for one video: fetch, read context, write and submit a proposal."""

    fetch(capsys, data_dir, video)
    code, out, err = run(
        capsys, "--data-dir", str(data_dir), "context", "--source-ref", youtube.source_ref(video)
    )
    assert code == EXIT_OK, err
    ctx = json.loads(out)
    ids = {tip["name"]: tip["id"] for tip in ctx["tips"]}
    document = {
        "schema": proposal.PROPOSAL_SCHEMA,
        "source_ref": ctx["source"]["ref"],
        "packet_sha256": ctx["source"]["packet_sha256"],
        "skill_sha256": ctx["skill_sha256"],
        "host": host,
        "model": model,
        "mode": SOURCE_MODE_EXTRACT,
        "degraded": False,
        "tips": [
            {
                "name": spec.name,
                "action": spec.action,
                "reason": spec.reason,
                "bucket": spec.bucket,
                "audience": spec.audience,
                "list_ids": list(spec.list_ids),
                "start_s": spec.start_s,
                "locator": spec.locator,
                "evidence_url": spec.evidence_url,
                "duplicate_of": None if spec.duplicate_of is None else ids[spec.duplicate_of],
                "possible_duplicate_of": (
                    None if spec.possible_duplicate_of is None else ids[spec.possible_duplicate_of]
                ),
                "note": spec.note,
            }
            for spec in specs
        ],
    }
    key = paths.source_key(youtube.source_ref(video))
    draft = data_dir / paths.PROPOSALS_DIR / f"draft-{key}.json"
    draft.write_bytes(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
    code, out, err = run(capsys, "--data-dir", str(data_dir), "propose", str(draft))
    assert code == EXIT_OK, (out, err)


def build_run(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    *,
    first: str = RPK,
    specs: dict[str, list[TipSpec]] | None = None,
    hosts: dict[str, str] | None = None,
) -> Path:
    """A data dir after ``init`` and the ingest of both in-sample videos, ``first`` first."""

    chosen = good_specs(first) if specs is None else specs
    init(capsys, data_dir)
    for video in (first, LPE if first == RPK else RPK):
        host = (hosts or {}).get(video, HOST_CLAUDE_CODE)
        ingest(capsys, data_dir, video, chosen[video], host=host)
    return data_dir


@dataclass(frozen=True)
class Scored:
    code: int
    payload: dict[str, Any] | None
    out: str
    err: list[str]

    @property
    def abort_rules(self) -> list[str]:
        assert self.payload is not None
        return [abort["rule"] for abort in self.payload["aborts"]]


def score_run(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path | None,
    *,
    labels: Path = LABELS,
    as_json: bool = True,
) -> Scored:
    argv = [] if data_dir is None else ["--data-dir", str(data_dir)]
    argv += ["eval", "score", "--labels", str(labels)]
    if as_json:
        argv.append("--json")
    code, out, err = run(capsys, *argv)
    payload = json.loads(out) if as_json and out.strip() else None
    return Scored(code, payload, out, err)


def tip_id(data_dir: Path, name: str) -> int:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        rows = conn.execute("SELECT id FROM tip WHERE name = ?", (name,)).fetchall()
    assert len(rows) == 1
    return int(rows[0][0])


def archive_file(data_dir: Path, video: str) -> Path:
    return proposal.archive_path(data_dir, youtube.source_ref(video))


def rewrite_archive(data_dir: Path, video: str, change: Callable[[dict[str, Any]], None]) -> None:
    """Apply ``change`` to the archived proposal of ``video``, to match a tampered row."""

    path = archive_file(data_dir, video)
    document = json.loads(path.read_bytes().decode("utf-8"))
    change(document)
    path.write_bytes(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))


def edit_archive(data_dir: Path, video: str, target: str, /, **changes: Any) -> None:
    """Change the archived proposal's tip named ``target`` to match a tampered row."""

    def change(document: dict[str, Any]) -> None:
        matching = [tip for tip in document["tips"] if tip["name"] == target]
        assert len(matching) == 1
        matching[0].update(changes)

    rewrite_archive(data_dir, video, change)


def data_files(data_dir: Path) -> dict[Path, bytes]:
    """Every file of the data dir but the score files, with its bytes."""

    eval_dir = data_dir / paths.EVAL_DIR
    return {
        path: path.read_bytes()
        for path in data_dir.rglob("*")
        if path.is_file() and path.parent != eval_dir
    }


def assert_left_as_it_was(data_dir: Path, before: dict[Path, bytes]) -> None:
    """Scoring changed no file of the data dir. Opening a WAL database read-only makes
    SQLite add its own index files when they are missing (an empty ``-wal`` and the
    ``-shm``), which the next writable connection removes; nothing else is new."""

    after = data_files(data_dir)
    assert {path: after.get(path) for path in before} == before
    database = data_dir / paths.DB_FILENAME
    added = after.keys() - before.keys()
    assert added <= {Path(f"{database}-wal"), Path(f"{database}-shm")}
    assert after.get(Path(f"{database}-wal"), b"") == b""


def copy_data_dir(source: Path, target: Path) -> Path:
    """A copy of a closed data dir: database, packets and archived proposals."""

    shutil.copytree(source, target)
    return target


def assert_pass(scored: Scored) -> None:
    assert scored.code == EXIT_OK, (scored.out, scored.err)
    assert scored.payload is not None
    assert scored.payload["verdict"] == score.PASS
    assert scored.payload["failed"] == []
    assert scored.payload["soft_failed"] == []
    statuses = {c["id"]: (c["gate"], c["status"]) for c in scored.payload["criteria"]}
    assert statuses == {
        **{criterion: (score.HARD, score.PASS) for criterion in HARD_IS},
        "IS-S": (score.SOFT, score.PASS),
    }


def assert_fails_exactly(scored: Scored, criterion: str) -> None:
    assert scored.code == EXIT_EVAL_FAIL, (scored.out, scored.err)
    assert scored.payload is not None
    assert scored.payload["verdict"] == score.FAIL
    assert scored.payload["failed"] == [criterion]
    hard_failed = [
        c["id"] for c in scored.payload["criteria"] if c["gate"] == score.HARD and c["failures"]
    ]
    assert hard_failed == [criterion]
    assert scored.err == [f"data_dir: {scored.payload['data_dir']}"]


def criterion_of(scored: Scored, criterion: str) -> dict[str, Any]:
    assert scored.payload is not None
    return next(c for c in scored.payload["criteria"] if c["id"] == criterion)


def assert_aborts(scored: Scored, rule: str) -> None:
    assert scored.code == EXIT_EVAL_ABORT, (scored.out, scored.err)
    assert scored.err[-1].startswith(f"{CODE_EVAL_ABORT}: ")
    assert rule in scored.err[-1]


# --- the committed labels and the fixture packet ---------------------------------------


def test_the_committed_labels_hold_every_in_sample_expectation_of_the_plan() -> None:
    labels = score.load_labels(LABELS)

    assert labels.path == LABELS
    assert labels.criteria == (*HARD_IS, "IS-S")
    assert [source.ref for source in labels.sources] == [
        youtube.source_ref(RPK),
        youtube.source_ref(LPE),
    ]
    labelled = {
        (source.ref, chapter.title): chapter.expect
        for source in labels.sources
        for chapter in source.chapters
    }
    # Plan §4.8: IS-3 needs the exact chapter title in all four labelled chapters, and
    # IS-4 places each one. A missing part would be a check that can never fail.
    accountability, sit_up, fire, mirror = (
        (youtube.source_ref(video), title) for title, video in LABELLED_CHAPTERS.items()
    )
    assert labelled == {
        accountability: score.Expect(bucket=2, audience="adults", on_lists=(), exact_name=True),
        sit_up: score.Expect(bucket=1, exact_name=True),
        fire: score.Expect(bucket=1, on_lists=(1,), exact_name=True),
        mirror: score.Expect(bucket=1, on_any_of=(5, 6), exact_name=True),
    }
    assert labels.duplicate_pairs == (
        score.DuplicatePair(
            score.ChapterRef(youtube.source_ref(RPK), "Drink Water Before Coffee"),
            score.ChapterRef(youtube.source_ref(LPE), "Your Brain Is A Raisin"),
            7,
        ),
    )
    assert sorted(set(labels.list_ids())) == [1, 5, 6, 7]


def test_the_rpk_fixture_packet_is_what_fetch_writes_with_the_real_chapters(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    init(capsys, data_dir)
    fetch(capsys, data_dir, RPK)

    written = packet_module.packet_path(data_dir, youtube.source_ref(RPK))
    assert written.read_bytes() == PACKET_FIXTURES[RPK].read_bytes()
    packet = fixture_packet(RPK)
    assert packet["duration_s"] == 554
    assert [(c["title"], c["start_s"], c["end_s"]) for c in packet["chapters"]] == [
        ("The One-Color Outfit Trick", 0, 84),
        ("Don't Carry Your Phone In Your Hand", 84, 141),
        ("The Clean Wallet Rule", 141, 203),
        ("Slow Down Your Walk", 203, 251),
        ("The Ice Cube Dryer Trick", 251, 297),
        ("Drink Water Before Coffee", 297, 346),
        ("The One-Side Hair Tuck", 346, 387),
        ("The Doorway Posture Check", 387, 434),
        ("Stop Overexplaining", 434, 485),
        ("The Three-Second Mirror Check", 485, 554),
    ]
    assert all(
        seg["text"].startswith("synthetic transcript") for seg in packet["transcript"]["segments"]
    )


# --- known-good runs ------------------------------------------------------------------


@pytest.mark.parametrize("first", [RPK, LPE], ids=["run-A", "run-B"])
def test_a_known_good_in_sample_run_passes(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos, first: str
) -> None:
    build_run(capsys, data_dir, first=first)
    before = data_files(data_dir)

    scored = score_run(capsys, data_dir)

    assert_pass(scored)
    assert scored.err == [f"data_dir: {data_dir}"]
    assert_left_as_it_was(data_dir, before)  # read-only: the score file is its one write
    payload = scored.payload
    assert payload is not None
    assert [source["ref"] for source in payload["sources"]] == [
        youtube.source_ref(RPK),
        youtube.source_ref(LPE),
    ]
    assert {source["tips"] for source in payload["sources"]} == {10}
    assert payload["skill_sha256"] == context.skill_sha256()
    assert payload["models"] == [MODEL]
    assert payload["mixed_models"] is False
    assert payload["labels"] == str(LABELS)
    is5 = criterion_of(scored, "IS-5")
    earlier = "Drink Water Before Coffee" if first == RPK else "Your Brain Is A Raisin"
    assert is5["notes"][0].startswith("earlier: #") and repr(earlier) in is5["notes"][0]


def test_the_score_file_holds_the_printed_result(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir)

    first, second, third = (score_run(capsys, data_dir) for _ in range(3))

    assert first.payload is not None and second.payload is not None
    assert third.payload is not None
    target, second_file, third_file = score_files(data_dir, 3)
    assert first.payload["score_file"] == str(target)
    raw = target.read_bytes()
    assert b"\r\n" not in raw
    stored = json.loads(raw.decode("utf-8"))
    assert stored == {key: value for key, value in first.payload.items() if key != "score_file"}
    assert stored["schema"] == score.SCORE_SCHEMA
    assert stored["scored_at"] == NOW_TEXT
    # A later score in the same second never overwrites an earlier one.
    assert second.payload["score_file"] == str(second_file)
    assert third.payload["score_file"] == str(third_file)
    assert target.read_bytes() == raw
    # Each file was published from a temporary name, and none is left behind.
    assert set((data_dir / paths.EVAL_DIR).iterdir()) == {target, second_file, third_file}


def no_hard_links(source: Any, destination: Any) -> None:
    """``os.link`` on a file system without hard links, as CreateHardLink fails on FAT."""

    raise OSError(errno.EPERM, "Incorrect function", str(source))


def test_without_hard_links_the_score_file_is_written_and_still_never_over_another(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    videos: FakeVideos,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_run(capsys, data_dir)
    first_name, second_name = score_files(data_dir, 2)
    linked = score_run(capsys, data_dir)  # published with a hard link, at the first name
    assert linked.payload is not None
    assert linked.payload["score_file"] == str(first_name)
    written = first_name.read_bytes()

    monkeypatch.setattr(os, "link", no_hard_links)
    unlinked = score_run(capsys, data_dir)

    assert unlinked.code == linked.code == EXIT_OK
    assert unlinked.payload is not None
    assert unlinked.payload["verdict"] == score.PASS
    # The first name is taken, so the score goes to the next one; the file there holds
    # exactly the bytes the hard-link path wrote, and the taken file is untouched.
    assert unlinked.payload["score_file"] == str(second_name)
    assert second_name.read_bytes() == written
    assert first_name.read_bytes() == written
    assert set((data_dir / paths.EVAL_DIR).iterdir()) == {first_name, second_name}


class DiskFullWriter:
    """A binary file handle that writes half of what it is given, then fails as a full
    disk does, so a write that reached the final name would leave a truncated file."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle

    def __enter__(self) -> DiskFullWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._handle.close()

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        self._handle.write(view[: len(view) // 2])
        self._handle.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self) -> None:
        self._handle.close()


def test_a_failure_while_writing_the_score_file_aborts_and_leaves_no_score_file(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    videos: FakeVideos,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_run(capsys, data_dir)
    real_open = io.open

    def disk_full_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        return DiskFullWriter(handle) if any(flag in mode for flag in "wxa") else handle

    with monkeypatch.context() as patch:
        # Every file write in the run, whichever way the scorer opens it, fails halfway.
        patch.setattr(io, "open", disk_full_open)
        failed = score_run(capsys, data_dir)

    assert failed.code == EXIT_EVAL_ABORT
    assert failed.out == ""
    assert failed.err[-1].startswith(
        f"{CODE_EVAL_ABORT}: {CODE_CONFIG}: cannot write the score file in "
        f"{data_dir / paths.EVAL_DIR}: "
    )
    assert "No space left on device" in failed.err[-1]
    assert list((data_dir / paths.EVAL_DIR).iterdir()) == []  # no score file, no temporary file
    # The final name was never taken: the next score gets it.
    scored = score_run(capsys, data_dir)
    assert scored.code == EXIT_OK
    assert scored.payload is not None
    assert scored.payload["score_file"] == str(score_files(data_dir)[0])


def test_the_table_lists_every_criterion_and_the_verdict(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir)

    scored = score_run(capsys, data_dir, as_json=False)

    assert scored.code == EXIT_OK
    lines = scored.out.splitlines()
    assert lines[0] == f"labels: {LABELS} (sha256 {score.load_labels(LABELS).sha256})"
    for criterion in HARD_IS:
        assert f"{criterion}  {score.HARD}  {score.PASS}" in lines
    assert f"IS-S  {score.SOFT}  {score.PASS} (reported, not gated)" in lines
    assert lines[-2:] == [f"verdict: {score.PASS}", f"score: {score_files(data_dir)[0]}"]


HOSTILE_TITLE = (
    "The Sunlight Stare\x1b[2K\r\nverdict: PASS"
    "\N{LINE SEPARATOR}\x85\x9b1A\N{RIGHT-TO-LEFT OVERRIDE}"
)
"""An unlabelled chapter title as yt-dlp's metadata may carry it: an ESC and a C1 control
sequence, a line break and two other line separators with a forged verdict line, and a
bidirectional override."""


def test_a_chapter_title_cannot_add_or_rewrite_a_printed_report_line(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    sunlight = videos.packets[LPE]["chapters"][3]
    assert sunlight["title"] == "The Sunlight Stare"
    sunlight["title"] = HOSTILE_TITLE
    # The title's window is left empty, so the IS-2 failure names it.
    specs = good_specs(RPK)
    specs[LPE] = without(specs[LPE], "Step Into Morning Daylight")
    build_run(capsys, data_dir, specs=specs)

    table = score_run(capsys, data_dir, as_json=False)
    as_json = score_run(capsys, data_dir)

    assert table.code == as_json.code == EXIT_EVAL_FAIL
    lines = table.out.split("\n")
    assert lines.pop() == ""  # print ends each line with one "\n" ...
    assert lines == table.out.splitlines()  # ... and no other line boundary is printed
    assert all(line.isprintable() for line in lines), lines
    assert [line for line in lines if line.startswith("verdict:")] == [f"verdict: {score.FAIL}"]
    escaped = r"The Sunlight Stare\x1b[2K\r\nverdict: PASS\u2028\x85\x9b1A\u202e"
    assert any(line.startswith("    fail: ") and line.endswith(escaped) for line in lines)
    # The --json output is ASCII, every other character a JSON escape; the score files
    # keep the title exact.
    assert as_json.out.isascii()
    assert criterion_of(as_json, "IS-2")["failures"][-1].endswith(f"empty: {HOSTILE_TITLE}")
    for score_file in score_files(data_dir, 2):
        stored = json.loads(score_file.read_bytes().decode("utf-8"))
        is2 = next(c for c in stored["criteria"] if c["id"] == "IS-2")
        assert is2["failures"][-1].endswith(f"empty: {HOSTILE_TITLE}")


def test_the_data_dir_may_come_from_the_env_var_or_after_the_subcommand(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    videos: FakeVideos,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_run(capsys, data_dir)

    code, _, err = run(
        capsys, "eval", "score", "--labels", str(LABELS), "--data-dir", str(data_dir)
    )
    assert code == EXIT_OK, err
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(data_dir))
    code, _, err = run(capsys, "eval", "score", "--labels", str(LABELS))
    assert code == EXIT_OK, err
    assert err == [f"data_dir: {data_dir}"]


# --- one garbage run per hard IS criterion ---------------------------------------------


Specs = dict[str, list[TipSpec]]


@dataclass(frozen=True)
class Garbage:
    """A known-garbage run through ``propose``: the good specs of the run that ingests
    ``first`` first, with ``change`` applied. It fails exactly ``criterion``, and one of
    that criterion's failures holds every text in ``named``."""

    criterion: str
    change: Callable[[Specs], Specs]
    named: tuple[str, ...]
    first: str = RPK


def one_tip_changed(
    criterion: str,
    video: str,
    target: str,
    named: tuple[str, ...],
    *,
    first: str = RPK,
    **changes: Any,
) -> Garbage:
    """The garbage run whose one tip named ``target``, in ``video``, has ``changes``."""

    def change(specs: Specs) -> Specs:
        return {**specs, video: changed(specs[video], target, **changes)}

    return Garbage(criterion, change, named, first)


IS3_NEAR_MISSES: dict[str, str] = {
    # The tip in each labelled chapter under a name close to, but not, the chapter title.
    "The Accountability Text": "Text A Friend Your New Habit",
    "Sit Up Straight, You're Not Dead Yet": "Sit Up Straight",
    "Fire Your Morning Brain": "Fire Up Your Morning Brain",
    "The Three-Second Mirror Check": "The Three-Second Mirror Glance",
}

IS4_MISPLACEMENTS: dict[tuple[str, str], tuple[dict[str, Any], str]] = {
    # Each part (bucket, audience, lists) of each labelled chapter's placement broken,
    # and the miss reported. Only a bucket-1 tip goes on a list, so a run that takes a
    # tip off bucket 1 also takes it off its lists, and the one that puts the
    # Accountability Text on a list also puts it in bucket 1.
    ("The Accountability Text", "bucket"): ({"bucket": 3}, "is in bucket 3, expected 2"),
    ("The Accountability Text", "audience"): ({"audience": "kids+"}, "is kids+, expected adults"),
    ("The Accountability Text", "lists"): (
        {"bucket": 1, "list_ids": (1,)},
        "is on lists [1], expected no list",
    ),
    ("Sit Up Straight, You're Not Dead Yet", "bucket"): (
        {"bucket": 3, "list_ids": ()},
        "is in bucket 3, expected 1",
    ),
    ("Fire Your Morning Brain", "bucket"): (
        {"bucket": 2, "list_ids": ()},
        "is in bucket 2, expected 1",
    ),
    ("Fire Your Morning Brain", "lists"): ({"list_ids": (4,)}, "is on lists [4], not on [1]"),
    ("The Three-Second Mirror Check", "bucket"): (
        {"bucket": 2, "list_ids": ()},
        "is in bucket 2, expected 1",
    ),
    ("The Three-Second Mirror Check", "lists"): (
        {"list_ids": (2,)},
        "is on lists [2], none of [5, 6]",
    ),
}


def renamed(title: str) -> Garbage:
    """IS-3: the tip in the labelled chapter ``title`` under its near-miss name; it stays
    in its window."""

    name = IS3_NEAR_MISSES[title]
    named = (repr(title), repr(name))
    return one_tip_changed("IS-3", LABELLED_CHAPTERS[title], title, named, name=name)


def misplaced(title: str, part: str) -> Garbage:
    """IS-4: the tip in the labelled chapter ``title`` placed against ``part`` of its
    label."""

    changes, miss = IS4_MISPLACEMENTS[title, part]
    named = (repr(title), miss)
    return one_tip_changed("IS-4", LABELLED_CHAPTERS[title], title, named, **changes)


PAIR_TIPS: dict[str, str] = {RPK: "Drink Water Before Coffee", LPE: "Your Brain Is A Raisin"}
"""The tip in each video's chapter of the labelled duplicate pair (IS-5)."""

UNPAIRED_TIPS: dict[str, str] = {RPK: "Stop Overexplaining", LPE: "Unfollow Your Ghosts"}
"""A tip of each video outside the pair, for a later pair tip to point at instead."""


def pair_tip_changed(first: str, side: str, named: tuple[str, ...], **changes: Any) -> Garbage:
    """IS-5: the run that ingests ``first`` first, with the ``earlier`` or ``later`` tip
    of the labelled pair changed."""

    video = first if side == "earlier" else (LPE if first == RPK else RPK)
    return one_tip_changed("IS-5", video, PAIR_TIPS[video], named, first=first, **changes)


def pair_garbage(first: str) -> dict[str, Garbage]:
    """IS-5, in the run that ingests ``first`` first: each of the four things the plan
    asks of the pair broken."""

    return {
        "the later not left out": pair_tip_changed(
            first, "later", ("bucket 3, expected 4",), bucket=3, duplicate_of=None
        ),
        "the later a duplicate of another tip": pair_tip_changed(
            first, "later", ("the later", "has duplicate_of"), duplicate_of=UNPAIRED_TIPS[first]
        ),
        "the earlier not daily": pair_tip_changed(
            first, "earlier", ("the earlier", "is in bucket 3, expected 1"), bucket=3, list_ids=()
        ),
        "the earlier off its list": pair_tip_changed(
            first, "earlier", ("the earlier", "not on 7"), list_ids=(1,)
        ),
    }


EXTRA_RPK_TIPS = [
    # Three more unlabelled tips, each in a window that already holds one.
    TipSpec("Fold Socks In Pairs", "Pair socks before the drawer", 3, "adults", 10),
    TipSpec("Keys On One Hook", "Hang your keys on the same hook", 3, "adults", 90),
    TipSpec("Water The Desk Plant", "Water the desk plant on Mondays", 3, "adults", 150),
]


def rpk_without(*names: str) -> Callable[[Specs], Specs]:
    def change(specs: Specs) -> Specs:
        kept = specs[RPK]
        for name in names:
            kept = without(kept, name)
        return {**specs, RPK: kept}

    return change


GARBAGE: dict[str, Garbage] = {
    # IS-2: a chapter window without a tip (9 tips, 9/10 windows).
    "IS-2 an empty window": Garbage(
        "IS-2",
        lambda specs: {**specs, LPE: without(specs[LPE], "Step Into Morning Daylight")},
        ("9/10 chapter windows hold a tip; empty: The Sunlight Stare",),
    ),
    # IS-2: 13 tips, every window still held (the lowest id in each is unchanged).
    "IS-2 13 tips": Garbage(
        "IS-2",
        lambda specs: {**specs, RPK: [*specs[RPK], *EXTRA_RPK_TIPS]},
        ("holds 13 tips, expected 8 to 12",),
    ),
    # IS-2: 7 tips (ten windows cannot all hold one, so three are empty too).
    "IS-2 7 tips": Garbage(
        "IS-2",
        rpk_without(
            "The One-Color Outfit Trick", "The Clean Wallet Rule", "The Ice Cube Dryer Trick"
        ),
        ("holds 7 tips, expected 8 to 12",),
    ),
    # IS-3: one run for each of the four labelled chapters; IS-4: one for each part of
    # each one's placement.
    **{f"IS-3 {title}": renamed(title) for title in LABELLED_CHAPTERS},
    **{f"IS-4 {title} {part}": misplaced(title, part) for title, part in IS4_MISPLACEMENTS},
    # IS-5: each part of the pair's verdict broken, in both ingest orders.
    **{
        f"IS-5 {run}, {what}": garbage
        for first, run in ((RPK, "run A"), (LPE, "run B"))
        for what, garbage in pair_garbage(first).items()
    },
    # IS-6: an unlabelled tip is called a duplicate of a tip in the other video.
    "IS-6": one_tip_changed(
        "IS-6",
        LPE,
        "Make Laziness Your Superpower",
        ("'Make Laziness Your Superpower' has duplicate_of",),
        bucket=4,
        duplicate_of="Stop Overexplaining",
    ),
    # IS-6, the pair the plan names: The Doorway Posture Check may be a possible duplicate
    # of Sit Up Straight, never a duplicate. In run B it is the later of the two.
    "IS-6 Doorway Posture Check": one_tip_changed(
        "IS-6",
        RPK,
        "The Doorway Posture Check",
        ("'The Doorway Posture Check' has duplicate_of",),
        first=LPE,
        bucket=4,
        list_ids=(),
        duplicate_of="Sit Up Straight, You're Not Dead Yet",
        possible_duplicate_of=None,
    ),
    # IS-8: the outro's "subscribe" made it into a tip's action, or (in capitals) its name.
    "IS-8 the action": one_tip_changed(
        "IS-8",
        LPE,
        "Unfollow Your Ghosts",
        ("its action 'Unfollow ghosts, then subscribe to one calm feed' matches /subscribe/i",),
        action="Unfollow ghosts, then subscribe to one calm feed",
    ),
    "IS-8 the name": one_tip_changed(
        "IS-8",
        LPE,
        "Unfollow Your Ghosts",
        ("its name 'SUBSCRIBE To Calm Feeds' matches /subscribe/i",),
        name="SUBSCRIBE To Calm Feeds",
    ),
}


def test_each_part_of_each_labelled_placement_has_its_garbage_run() -> None:
    # A part of a label with no garbage run is a check nothing shows can fail.
    labels = score.load_labels(LABELS)
    parts = {
        (chapter.title, part)
        for source in labels.sources
        for chapter in source.chapters
        for part, value in (
            ("bucket", chapter.expect.bucket),
            ("audience", chapter.expect.audience),
            ("lists", chapter.expect.on_lists),
            ("lists", chapter.expect.on_any_of),
        )
        if value is not None
    }

    assert set(IS4_MISPLACEMENTS) == parts
    assert set(IS3_NEAR_MISSES) == {
        chapter.title
        for source in labels.sources
        for chapter in source.chapters
        if chapter.expect.exact_name
    }


@pytest.mark.parametrize("case", sorted(GARBAGE))
def test_a_garbage_run_through_propose_fails_exactly_its_criterion(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos, case: str
) -> None:
    garbage = GARBAGE[case]
    specs = garbage.change(good_specs(garbage.first))
    build_run(capsys, data_dir, first=garbage.first, specs=specs)

    scored = score_run(capsys, data_dir)

    assert_fails_exactly(scored, garbage.criterion)
    failures = criterion_of(scored, garbage.criterion)["failures"]
    assert any(all(text in failure for text in garbage.named) for failure in failures), failures


@dataclass(frozen=True)
class Tamper:
    """A known-garbage run built from a copy of good run A: ``apply`` changes the copy's
    database straight, and its archived proposal to match, so the operator-edit abort
    does not fire first. It fails exactly ``criterion``, and one of that criterion's
    failures holds every text in ``named``."""

    criterion: str
    apply: Callable[[Path], None]
    named: tuple[str, ...]


def raw_connection(data_dir: Path) -> sqlite3.Connection:
    """A plain connection to the copy's database that neither the foreign keys nor the
    CHECK constraints stop, as a hand edit of the file would be."""

    conn = sqlite3.connect(data_dir / paths.DB_FILENAME)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA ignore_check_constraints = ON")
    return conn


def tamper_mode(data_dir: Path) -> None:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn, conn:
        conn.execute(
            "UPDATE source SET mode = ? WHERE ref = ?",
            (SOURCE_MODE_INVESTIGATE, youtube.source_ref(LPE)),
        )
    rewrite_archive(data_dir, LPE, lambda document: document.update(mode=SOURCE_MODE_INVESTIGATE))


def tamper_adults_tip_on_kids_list(data_dir: Path) -> None:
    fire = tip_id(data_dir, "Fire Your Morning Brain")
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn, conn:
        conn.execute("DROP TRIGGER list_item_insert_requires_kids_tip_on_kids_list")
        conn.execute("INSERT INTO list_item (list_id, tip_id, position) VALUES (3, ?, 1)", (fire,))
    edit_archive(data_dir, LPE, "Fire Your Morning Brain", list_ids=[1, 3])


def tamper_ref_stored_twice(data_dir: Path) -> None:
    # source.ref is UNIQUE, so the copy's source table is rebuilt without that
    # constraint (SQLite's way to change one), then LpeDuC2sQ-w's row is stored again.
    with closing(raw_connection(data_dir)) as conn:
        (table,) = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'source'"
        ).fetchone()
        assert table.count(" UNIQUE") == 1
        loose = table.replace("CREATE TABLE source", "CREATE TABLE loose", 1)
        with conn:
            conn.execute(loose.replace(" UNIQUE", "", 1))
            conn.execute("INSERT INTO loose SELECT * FROM source")
            conn.execute("DROP TABLE source")
            conn.execute("ALTER TABLE loose RENAME TO source")
            columns = "kind, ref, title, url, revision, mode, fetched_at, reviewed_at"
            conn.execute(
                f"INSERT INTO source ({columns}) SELECT {columns} FROM source WHERE ref = ?",
                (youtube.source_ref(LPE),),
            )


def tamper_list_item_on_a_bucket_3_tip(data_dir: Path) -> None:
    target = tip_id(data_dir, TAMPERED_TIP)
    with closing(raw_connection(data_dir)) as conn, conn:
        conn.execute("DROP TRIGGER list_item_insert_requires_bucket_1")
        conn.execute(
            "INSERT INTO list_item (list_id, tip_id, position) VALUES (1, ?, 2)", (target,)
        )
    edit_archive(data_dir, RPK, TAMPERED_TIP, list_ids=[1])


TAMPERED_TIP = "Stop Overexplaining"
"""The RPK-xw3SrVw tip the limit runs change: unlabelled, bucket 3, adults, on no list."""


def tip_edited(**columns: Any) -> Callable[[Path], None]:
    """Set ``columns`` on :data:`TAMPERED_TIP` in the copy's database and archive."""

    def apply(data_dir: Path) -> None:
        assignments = ", ".join(f"{column} = ?" for column in columns)
        with closing(raw_connection(data_dir)) as conn, conn:
            changed = conn.execute(
                f"UPDATE tip SET {assignments} WHERE name = ?", (*columns.values(), TAMPERED_TIP)
            )
            assert changed.rowcount == 1
        edit_archive(data_dir, RPK, TAMPERED_TIP, **columns)

    return apply


def tip_added(**columns: Any) -> Callable[[Path], None]:
    """Add a copy of :data:`TAMPERED_TIP` with ``columns`` to the copy's database and,
    last, to its archive: a ``start_s`` outside every window would leave the tip's own
    window empty (IS-2), so the tampered row is an extra one (11 tips)."""

    def apply(data_dir: Path) -> None:
        with closing(raw_connection(data_dir)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tip WHERE name = ?", (TAMPERED_TIP,)).fetchone()
            values = {key: row[key] for key in row.keys() if key != "id"} | columns
            conn.execute(
                f"INSERT INTO tip ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                tuple(values.values()),
            )

        def change(document: dict[str, Any]) -> None:
            (original,) = [tip for tip in document["tips"] if tip["name"] == TAMPERED_TIP]
            document["tips"].append({**original, **columns})

        rewrite_archive(data_dir, RPK, change)

    return apply


def tamper_reference_over_id_max(data_dir: Path) -> None:
    # ID_MAX is SQLite's largest INTEGER, so the value past it is stored as the REAL it
    # becomes, and the store reads it back as that whole number.
    with closing(raw_connection(data_dir)) as conn, conn:
        conn.execute(
            "UPDATE tip SET possible_duplicate_of = ? WHERE name = ?",
            (float(ID_MAX + 1), TAMPERED_TIP),
        )
    edit_archive(data_dir, RPK, TAMPERED_TIP, possible_duplicate_of=ID_MAX + 1)


RPK_DURATION_S: int = fixture_packet(RPK)["duration_s"]

TAMPERS: dict[str, Tamper] = {
    # IS-1: the mode, and the ref stored twice. (A missing or unreviewed source aborts.)
    "IS-1 mode not extract": Tamper(
        "IS-1", tamper_mode, (f"has mode {SOURCE_MODE_INVESTIGATE!r}, expected extract",)
    ),
    "IS-1 ref stored twice": Tamper(
        "IS-1", tamper_ref_stored_twice, ("is stored 2 times, expected once",)
    ),
    # IS-7: the two placement invariants, each with its trigger dropped.
    "IS-7 an adults tip on a kids list": Tamper(
        "IS-7", tamper_adults_tip_on_kids_list, ("is a kids list and accepts only kids+ tips",)
    ),
    "IS-7 a list item on a bucket-3 tip": Tamper(
        "IS-7", tamper_list_item_on_a_bucket_3_tip, ("only bucket-1 (daily) tips go on lists",)
    ),
    # IS-7: each limits.py limit of a stored tip field, broken alone.
    "IS-7 NAME_MAX": Tamper(
        "IS-7",
        tip_edited(name="N" * (NAME_MAX + 1)),
        (f"its name is {NAME_MAX + 1} characters; NAME_MAX is {NAME_MAX}",),
    ),
    "IS-7 ACTION_MAX": Tamper(
        "IS-7",
        tip_edited(name="Stop", action="A" * (ACTION_MAX + 1)),  # the line stays short
        (f"its action is {ACTION_MAX + 1} characters; ACTION_MAX is {ACTION_MAX}",),
    ),
    "IS-7 LINE_MAX": Tamper(
        "IS-7",
        tip_edited(name="N" * NAME_MAX, action="A" * (LINE_MAX - NAME_MAX)),
        (f"LINE_MAX is {LINE_MAX}",),
    ),
    "IS-7 REASON_MAX": Tamper(
        "IS-7",
        tip_edited(reason="R" * (REASON_MAX + 1)),
        (f"its reason is {REASON_MAX + 1} characters; REASON_MAX is {REASON_MAX}",),
    ),
    "IS-7 LOCATOR_MAX": Tamper(
        "IS-7",
        tip_edited(locator="L" * (LOCATOR_MAX + 1)),
        (f"its locator is {LOCATOR_MAX + 1} characters; LOCATOR_MAX is {LOCATOR_MAX}",),
    ),
    "IS-7 NOTE_MAX": Tamper(
        "IS-7",
        tip_edited(note="N" * (NOTE_MAX + 1)),
        (f"its note is {NOTE_MAX + 1} characters; NOTE_MAX is {NOTE_MAX}",),
    ),
    "IS-7 EVIDENCE_URL_MAX": Tamper(
        "IS-7",
        tip_edited(evidence_url="https://example.com/" + "p" * EVIDENCE_URL_MAX),
        (f"; EVIDENCE_URL_MAX is {EVIDENCE_URL_MAX}",),
    ),
    "IS-7 START_S_MAX": Tamper(
        "IS-7",
        tip_added(name="Late Copy", start_s=START_S_MAX + 1),
        # The packet's duration is the tighter bound, so it is the one reported.
        (f"its start_s is {START_S_MAX + 1}; it must be 0 <= start_s < duration_s",),
    ),
    "IS-7 start_s at the duration": Tamper(
        "IS-7",
        tip_added(name="Late Copy", start_s=RPK_DURATION_S),
        (f"its start_s is {RPK_DURATION_S}; it must be 0 <= start_s < duration_s",),
    ),
    "IS-7 start_s below 0": Tamper(
        "IS-7",
        tip_added(name="Early Copy", start_s=-1),
        ("its start_s is -1; it must be 0 <= start_s < duration_s",),
    ),
    "IS-7 BUCKET_IDS": Tamper(
        "IS-7", tip_edited(bucket=5), (f"its bucket is 5; it {proposal.NOT_A_BUCKET}",)
    ),
    "IS-7 AUDIENCES_TIP": Tamper(
        "IS-7",
        tip_edited(audience="teens"),
        (f"its audience is 'teens'; it {proposal.NOT_A_TIP_AUDIENCE}",),
    ),
    "IS-7 ID_MAX": Tamper(
        "IS-7",
        tamper_reference_over_id_max,
        (f"its possible_duplicate_of is {ID_MAX + 1}, not a tip id",),
    ),
}


@pytest.mark.parametrize("case", sorted(TAMPERS))
def test_a_garbage_run_built_by_tampering_with_a_copy_fails_exactly_its_criterion(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    data_dir: Path,
    videos: FakeVideos,
    case: str,
) -> None:
    tamper = TAMPERS[case]
    build_run(capsys, data_dir)
    tampered = copy_data_dir(data_dir, tmp_path / "tampered")

    tamper.apply(tampered)

    scored = score_run(capsys, tampered)
    assert_fails_exactly(scored, tamper.criterion)
    failures = criterion_of(scored, tamper.criterion)["failures"]
    assert any(all(text in failure for text in tamper.named) for failure in failures), failures
    assert_pass(score_run(capsys, data_dir))  # the original stays a known-good run


def test_every_tip_text_limit_has_its_tampered_garbage_run() -> None:
    # IS-7 checks every text field through proposal.TIP_TEXT_LIMITS; a limit added
    # there needs its run here.
    for constant, _ in proposal.TIP_TEXT_LIMITS.values():
        assert TAMPERS[f"IS-7 {constant}"].criterion == "IS-7"


# --- ABORT: no verdict (exit 6) ---------------------------------------------------------


def test_a_data_dir_from_neither_flag_nor_env_aborts_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scored = score_run(capsys, None)

    assert_aborts(scored, score.ABORT_DATA_DIR)
    assert scored.out == ""
    assert not (tmp_path / "localappdata").exists()
    assert not (tmp_path / "xdg").exists()


@pytest.mark.parametrize("given_by", ["flag", "env"])
def test_an_empty_data_dir_value_aborts_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    given_by: str,
) -> None:
    argv = ["eval", "score", "--labels", str(LABELS), "--json"]
    if given_by == "flag":
        argv = ["--data-dir", "", *argv]
        name = "--data-dir"
    else:
        monkeypatch.setenv(paths.DATA_DIR_ENV, "")
        name = paths.DATA_DIR_ENV

    code, out, err = run(capsys, *argv)

    assert code == EXIT_EVAL_ABORT
    assert out == ""
    assert err == [
        f"{CODE_EVAL_ABORT}: {score.ABORT_DATA_DIR_UNUSABLE}: {CODE_CONFIG}: {name} is empty; "
        "give it a directory path"
    ]
    assert not (tmp_path / "localappdata").exists()


def test_a_data_dir_inside_a_git_checkout_aborts_where_other_commands_exit_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    inside = checkout / "data"

    scored = score_run(capsys, inside)

    assert scored.code == EXIT_EVAL_ABORT
    assert scored.out == ""
    assert scored.err[0] == f"data_dir: {inside}"
    assert scored.err[-1].startswith(
        f"{CODE_EVAL_ABORT}: {score.ABORT_DATA_DIR_UNUSABLE}: {CODE_DATA_IN_REPO}: refusing a "
        "data directory inside a git checkout"
    )
    assert not inside.exists()
    code, _, err = run(capsys, "--data-dir", str(inside), "status")
    assert code == EXIT_CONFIG
    assert err[-1].startswith(f"{CODE_DATA_IN_REPO}: ")


def test_a_missing_labelled_source_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    init(capsys, data_dir)
    ingest(capsys, data_dir, RPK, rpk_specs(first=True))

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_SOURCE_MISSING)
    assert scored.abort_rules == [score.ABORT_SOURCE_MISSING]
    assert scored.payload is not None
    assert scored.payload["verdict"] == score.ABORT
    assert scored.payload["criteria"] == []
    # An abort is a result too: it is written, with no criterion scored.
    stored = json.loads(score_files(data_dir)[0].read_bytes())
    assert stored["verdict"] == score.ABORT


def test_an_unreviewed_labelled_source_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    init(capsys, data_dir)
    ingest(capsys, data_dir, RPK, rpk_specs(first=True))
    fetch(capsys, data_dir, LPE)

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_SOURCE_UNREVIEWED)
    assert scored.abort_rules == [score.ABORT_SOURCE_UNREVIEWED]


def test_a_packet_with_zero_segments_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    videos.packets[LPE]["transcript"]["segments"] = []
    build_run(capsys, data_dir)

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_PACKET_EMPTY)
    assert scored.abort_rules == [score.ABORT_PACKET_EMPTY]
    assert "zero segments" in scored.err[-1]


def test_a_packet_with_zero_chapters_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    videos.packets[LPE]["chapters"] = []
    build_run(capsys, data_dir)

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_PACKET_EMPTY)
    assert scored.abort_rules == [score.ABORT_PACKET_EMPTY]
    assert "zero chapters" in scored.err[-1]


def test_a_label_title_that_is_not_byte_equal_to_the_packet_title_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    # The live chapter title drifts to a typographic apostrophe; the label keeps ASCII.
    chapter = videos.packets[LPE]["chapters"][4]
    assert chapter["title"] == "Sit Up Straight, You're Not Dead Yet"
    chapter["title"] = "Sit Up Straight, You’re Not Dead Yet"
    build_run(capsys, data_dir)

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_CHAPTER_TITLE)
    assert scored.abort_rules == [score.ABORT_CHAPTER_TITLE]
    assert "You’re" in scored.err[-1]  # the near match is named


def test_a_packet_changed_after_the_proposal_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir)
    packet_file = packet_module.packet_path(data_dir, youtube.source_ref(LPE))
    raw = packet_file.read_bytes()
    packet_file.write_bytes(raw.replace(b"line 1: placeholder", b"line 1: replaced", 1))

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_PACKET_STALE)
    assert scored.abort_rules == [score.ABORT_PACKET_STALE]


def test_a_missing_archived_proposal_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir)
    archive_file(data_dir, RPK).unlink()

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_ARCHIVE)
    assert scored.abort_rules == [score.ABORT_ARCHIVE]


INPUT_FILES: dict[str, tuple[Callable[[Path], Path], str, int, str]] = {
    # The input file, its abort rule, its cap and the cap's name.
    "archived proposal": (
        lambda data_dir: archive_file(data_dir, RPK),
        score.ABORT_ARCHIVE,
        PROPOSAL_FILE_MAX_BYTES,
        "PROPOSAL_FILE_MAX_BYTES",
    ),
    "cached packet": (
        lambda data_dir: packet_module.packet_path(data_dir, youtube.source_ref(LPE)),
        score.ABORT_PACKET,
        score.PACKET_FILE_MAX_BYTES,
        "PACKET_FILE_MAX_BYTES",
    ),
}


@pytest.mark.parametrize("unusable", ["one byte over its cap", "a folder"])
@pytest.mark.parametrize("input_file", sorted(INPUT_FILES))
def test_an_input_file_over_its_cap_or_not_a_regular_file_aborts_unread(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    videos: FakeVideos,
    input_file: str,
    unusable: str,
) -> None:
    locate, rule, cap, cap_name = INPUT_FILES[input_file]
    build_run(capsys, data_dir)
    path = locate(data_dir)
    if unusable == "a folder":  # a folder, a device or a pipe is never opened
        path.unlink()
        path.mkdir()
        named = strict_json.NOT_REGULAR_FILE
    else:
        # Trailing spaces keep the same JSON value, so only the cap can refuse the file:
        # read whole, the archive would still pass and the packet would abort as stale.
        data = path.read_bytes()
        path.write_bytes(data + b" " * (cap + 1 - len(data)))
        named = f"it is larger than {cap_name} ({cap} bytes)"

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, rule)
    assert scored.abort_rules == [rule]
    assert named in scored.err[-1]


@pytest.mark.parametrize("unusable", ["missing", "not JSON", "JSON of the wrong shape"])
def test_a_missing_or_malformed_cached_packet_aborts(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    videos: FakeVideos,
    unusable: str,
) -> None:
    # The packet-only siblings of the cases above: a missing file is an OSError and a
    # malformed one a PacketFormatError, each the packet-unusable abort, never a crash.
    locate, rule, _, _ = INPUT_FILES["cached packet"]
    build_run(capsys, data_dir)
    path = locate(data_dir)
    if unusable == "missing":
        path.unlink()
        named = f"cannot read the packet of {youtube.source_ref(LPE)} at {path}: "
    elif unusable == "not JSON":
        path.write_bytes(b"{not JSON")
        named = "packet is not UTF-8 JSON"
    else:
        path.write_bytes(b"[]")
        named = f"packet is not a {packet_module.PACKET_SCHEMA!r} object"

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, rule)
    assert scored.abort_rules == [rule]
    assert named in scored.err[-1]


def test_proposals_from_two_hosts_abort(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir, hosts={LPE: HOST_CODEX})

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_MIXED_HOST)
    assert scored.abort_rules == [score.ABORT_MIXED_HOST]


def test_a_skill_that_changed_after_the_proposals_aborts_as_stale(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos, skill_root: Path
) -> None:
    build_run(capsys, data_dir)
    assert_pass(score_run(capsys, data_dir))
    core = skill_root / context.SKILL_FILES[0]
    core.write_bytes(core.read_bytes() + b"\nOne more line.\n")

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_SKILL_STALE)
    assert scored.abort_rules == [score.ABORT_SKILL_STALE]
    assert scored.payload is not None
    assert scored.payload["skill_sha256"] == context.skill_sha256()


def test_proposals_from_two_skill_versions_abort(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos, skill_root: Path
) -> None:
    init(capsys, data_dir)
    ingest(capsys, data_dir, RPK, rpk_specs(first=True))
    core = skill_root / context.SKILL_FILES[0]
    core.write_bytes(core.read_bytes() + b"\nOne more line.\n")
    ingest(capsys, data_dir, LPE, lpe_specs(first=False))

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_MIXED_SKILL)
    assert scored.abort_rules == [score.ABORT_MIXED_SKILL, score.ABORT_SKILL_STALE]
    assert youtube.source_ref(RPK) in scored.err[-1]


def _edit_lists(conn: sqlite3.Connection, data_dir: Path) -> None:
    list_store.append_item(conn, 6, tip_id(data_dir, "Fire Your Morning Brain"))


def _tamper_source_mode(conn: sqlite3.Connection, data_dir: Path) -> None:
    conn.execute(
        "UPDATE source SET mode = ? WHERE ref = ?",
        (SOURCE_MODE_INVESTIGATE, youtube.source_ref(RPK)),
    )


def _delete_tip(conn: sqlite3.Connection, data_dir: Path) -> None:
    assert tip_store.delete_tip(conn, tip_id(data_dir, "Slow Down Your Walk"))


def _add_tip(conn: sqlite3.Connection, data_dir: Path) -> None:
    source = source_store.get_by_ref(conn, youtube.source_ref(LPE))
    assert source is not None
    tip_store.insert_tip(
        conn,
        tip_store.NewTip(
            name="Extra",
            action="Do one more thing",
            reason=REASON,
            bucket=3,
            audience="adults",
            start_s=500,
        ),
        source_id=source.id,
        status=TIP_STATUS_PENDING,
        now=NOW_TEXT,
    )


OPERATOR_EDITS: dict[str, Any] = {
    # Each change, and the text that names it in the abort. Only what the unit layer
    # (tests/unit/test_scorer_rules.py, which compares in-memory tips field by field)
    # cannot see: one field edit through the production store, read back from the
    # database into the compared tip (list_ids comes from its own table), the source
    # row's mode, and a tip deleted or added in the database.
    "list_ids": (_edit_lists, "differs from its archived proposal in list_ids"),
    "source mode (tampered)": (
        _tamper_source_mode,
        f"is stored in mode {SOURCE_MODE_INVESTIGATE}",
    ),
    "a deleted tip": (_delete_tip, "a tip was added or deleted"),
    "an added tip": (_add_tip, "a tip was added or deleted"),
}


@pytest.mark.parametrize("edit", sorted(OPERATOR_EDITS))
def test_an_operator_edit_after_the_import_aborts(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos, edit: str
) -> None:
    change, named = OPERATOR_EDITS[edit]
    build_run(capsys, data_dir)
    with closing(connection.connect(data_dir / paths.DB_FILENAME)) as conn:
        with connection.transaction(conn):
            change(conn, data_dir)

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_OPERATOR_EDIT)
    assert scored.abort_rules == [score.ABORT_OPERATOR_EDIT]
    assert named in scored.err[-1]


def test_approving_every_tip_is_not_an_edit(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir)
    with closing(connection.connect(data_dir / paths.DB_FILENAME)) as conn:
        with connection.transaction(conn):
            ids = [row[0] for row in conn.execute("SELECT id FROM tip")]
            assert tip_store.approve(conn, ids, now=NOW_TEXT) == 20

    assert_pass(score_run(capsys, data_dir))


# --- ABORT: unusable labels, database or a crash ----------------------------------------


def write_labels(tmp_path: Path, change: Any) -> Path:
    document = json.loads(LABELS.read_bytes().decode("utf-8"))
    change(document)
    target = tmp_path / "labels.json"
    target.write_bytes(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    return target


@pytest.mark.parametrize("unusable", ["missing", "operator-only criterion id"])
def test_an_unusable_labels_file_aborts_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    data_dir: Path,
    videos: FakeVideos,
    unusable: str,
) -> None:
    # One unreadable and one defective file: the two ways load_labels refuses one. Every
    # other labels defect is asserted in tests/unit/test_scorer_rules.py.
    build_run(capsys, data_dir)
    if unusable == "missing":
        labels, named = tmp_path / "nope.json", "cannot read the labels file"
    else:  # IS-9 is the operator's verdict (plan §4.8), never a scored id
        labels = write_labels(tmp_path, lambda doc: doc["criteria"].append("IS-9"))
        named = "'IS-9' is not one this scorer knows"

    scored = score_run(capsys, data_dir, labels=labels)

    assert_aborts(scored, score.ABORT_LABELS)
    assert named in scored.err[-1]
    assert scored.out == ""
    assert list((data_dir / paths.EVAL_DIR).iterdir()) == []


HOSTILE_KEY = "x\x1b[2J\r\nCC_EVAL_ABORT: forged\x07\x9b1A\N{RIGHT-TO-LEFT OVERRIDE}"
"""A labels key holding an ESC and a C1 control sequence, a line break with a forged abort
line, a BEL and a bidirectional override."""


def test_a_labels_key_cannot_add_or_drive_an_abort_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, data_dir: Path
) -> None:
    labels = tmp_path / "labels.json"
    labels.write_bytes(json.dumps({HOSTILE_KEY: 1}).encode("utf-8"))

    code = cli.main(["--data-dir", str(data_dir), "eval", "score", "--labels", str(labels)])
    captured = capsys.readouterr()

    assert code == EXIT_EVAL_ABORT
    assert captured.out == ""
    lines = captured.err.split("\n")
    assert lines.pop() == ""  # each line ends with one "\n" ...
    assert lines == captured.err.splitlines()  # ... and no other line boundary is printed
    assert all(line.isprintable() for line in lines), lines
    # The line break folds into one space, as every abort line's white space does; the
    # control characters print as their escapes.
    escaped = r"x\x1b[2J CC_EVAL_ABORT: forged\x07\x9b1A\u202e"
    assert lines == [
        f"data_dir: {data_dir}",
        f"{CODE_EVAL_ABORT}: {score.ABORT_LABELS}: {labels}: the labels has the unknown "
        f"key(s) {escaped}; the scorer never skips a label it cannot read",
    ]
    assert not data_dir.exists()


def test_a_label_list_that_does_not_exist_aborts(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir)
    labels = write_labels(
        tmp_path, lambda doc: doc["sources"][1]["chapters"][0]["expect"].update(on_lists=[9])
    )

    scored = score_run(capsys, data_dir, labels=labels)

    assert_aborts(scored, score.ABORT_LABEL_LIST)
    assert scored.abort_rules == [score.ABORT_LABEL_LIST]


def test_a_data_dir_without_a_database_aborts_and_creates_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_DATABASE)
    assert not data_dir.exists()


def test_a_foreign_database_aborts_and_is_left_as_it_was(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    # Schema version 0 and no tables, yet not empty: a writable open would apply the
    # schema to it. The scorer opens it read-only and aborts.
    data_dir.mkdir()
    database = data_dir / paths.DB_FILENAME
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("CREATE TABLE scratch (x)")
        conn.execute("DROP TABLE scratch")
        conn.commit()
    before = database.read_bytes()
    assert before

    scored = score_run(capsys, data_dir)

    assert_aborts(scored, score.ABORT_DATABASE)
    assert "schema version 0" in scored.err[-1]
    assert_left_as_it_was(data_dir, {database: before})


def test_an_eval_path_that_is_a_file_aborts_with_the_config_error(
    capsys: pytest.CaptureFixture[str], data_dir: Path, videos: FakeVideos
) -> None:
    build_run(capsys, data_dir)
    eval_path = data_dir / paths.EVAL_DIR
    eval_path.rmdir()
    eval_path.write_bytes(b"not a folder\n")

    scored = score_run(capsys, data_dir)

    assert scored.code == EXIT_EVAL_ABORT
    assert scored.out == ""
    # The ConfigError write_report promises, not a crash line or a traceback.
    assert scored.err[-1].startswith(
        f"{CODE_EVAL_ABORT}: {CODE_CONFIG}: cannot write the score file in {eval_path}: "
    )
    assert not any("Traceback" in line or CODE_INTERNAL in line for line in scored.err)
    assert eval_path.read_bytes() == b"not a folder\n"


def test_a_crash_inside_eval_score_exits_6_not_9(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    videos: FakeVideos,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_run(capsys, data_dir)

    def explode(*args: Any, **kwargs: Any) -> Any:
        # The error text holds an ESC sequence, a line break with a forged verdict line
        # and a bidirectional override, as an exception's text quoting input may.
        raise RuntimeError("scorer bug\x1b[2K\nverdict: PASS\N{RIGHT-TO-LEFT OVERRIDE}")

    monkeypatch.setattr(score, "collect", explode)

    code = cli.main(["--data-dir", str(data_dir), "eval", "score", "--labels", str(LABELS)])
    captured = capsys.readouterr()

    assert code == EXIT_EVAL_ABORT
    assert captured.out == ""
    lines = captured.err.split("\n")
    assert lines.pop() == ""  # each line ends with one "\n" ...
    assert lines == captured.err.splitlines()  # ... and no other line boundary is printed
    assert all(line.isprintable() for line in lines), lines
    # The line break folds into a space; the ESC and the override print as escapes.
    assert lines == [
        f"data_dir: {data_dir}",
        f"{CODE_EVAL_ABORT}: eval score crashed, so there is no verdict; "
        f"{CODE_INTERNAL}: unexpected RuntimeError: scorer bug\\x1b[2K verdict: PASS\\u202e",
    ]
    assert list((data_dir / paths.EVAL_DIR).iterdir()) == []
