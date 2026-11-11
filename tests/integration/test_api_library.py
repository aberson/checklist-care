"""The Library API (plan §6, Step 9) through ``TestClient``: "Add my own tip"
(``POST /api/tips``), moving a tip between buckets and deleting one, read back
through the print sheets the tips feed.

Each test starts from a data directory made by ``cli.main(["init"])`` (the seeded
buckets, the seven lists and the manual source). The duplicate that a deleted tip
leaves behind is made by the production producer: ``fetch`` (the two network seams
faked from ``tests/fixtures/packet_youtube_synthetic.json``), ``context`` and
``propose``, with a proposal that names the manual tip as its original, the way the
skill finds your own tips through ``context`` (plan §3.5). The Library page loads
its data from ``serve --demo``, which is checked here too.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from youtube_transcript_api import FetchedTranscript, FetchedTranscriptSnippet

from checklist_care import cli, limits, paths, proposal
from checklist_care.db import connection, seed
from checklist_care.server import run
from checklist_care.server.app import create_app
from checklist_care.sources import youtube
from checklist_care.store import tips as tip_store

ROOT = Path(__file__).resolve().parents[2]
PACKET_FIXTURE = ROOT / "tests" / "fixtures" / "packet_youtube_synthetic.json"

VIDEO = "EXAMPLE0009"
VIDEO_REF = youtube.source_ref(VIDEO)
NOW = datetime(2026, 11, 11, 9, 0, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-11T09:00:00Z"
LOCAL_URL = "http://127.0.0.1:8450"
"""TestClient's default ``testserver`` Host is not one the app accepts."""

ADULT_BATHROOM = 1  # Adult Bathroom / adults
OFFICE = 2  # My Office / adults
KIDS_BATHROOM = 3  # Kids Bathroom / kids

BRUSH: dict[str, Any] = {
    "name": "Brush your teeth",
    "action": "Brush for two full minutes, morning and night",
    "bucket": limits.BUCKET_DAILY,
    "audience": limits.AUDIENCE_KIDS_PLUS,
    "list_ids": [KIDS_BATHROOM, ADULT_BATHROOM],
}
"""The basic of Step 9's Done when: bucket 1, kids+, on lists 3 and 1, no reason."""

FACE: dict[str, Any] = {
    "name": "Wash your face",
    "action": "Splash cold water on your face",
    "reason": "A cold splash is an easy wake-up cue.",
    "bucket": limits.BUCKET_DAILY,
    "audience": limits.AUDIENCE_ADULTS,
    "list_ids": [ADULT_BATHROOM],
}


# --- the production producer: init, fetch, context, propose ---------------------------------


def fixture_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_bytes().decode("utf-8"))
    return payload


@dataclass
class FakeTranscripts:
    """The fixture's segments as the transcript library's own objects, for any video."""

    calls: list[str] = field(default_factory=list)

    def fetch(self, video_id: str, languages: Iterable[str] = ("en",)) -> FetchedTranscript:
        self.calls.append(video_id)
        return FetchedTranscript(
            snippets=[
                FetchedTranscriptSnippet(
                    text=seg["text"], start=seg["start"], duration=seg["duration"]
                )
                for seg in fixture_json(PACKET_FIXTURE)["transcript"]["segments"]
            ],
            video_id=video_id,
            language="English (auto-generated)",
            language_code="en",
            is_generated=True,
        )


def fake_ytdlp(url: str) -> dict[str, Any]:
    """yt-dlp's JSON for the fixture's title, channel, duration and chapters."""

    packet = fixture_json(PACKET_FIXTURE)
    return {
        "id": url.rsplit("=", 1)[-1],
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
    """Never touch the real data directory or the real demo folder; control the clock."""

    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    monkeypatch.setattr(connection, "utc_now", lambda: NOW)
    monkeypatch.setattr(youtube, "make_transcript_client", FakeTranscripts)
    monkeypatch.setattr(youtube, "run_ytdlp", fake_ytdlp)


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


def cli_ok(capsys: pytest.CaptureFixture[str], data_dir: Path, *argv: str) -> str:
    code, out, err = run_cli(capsys, "--data-dir", str(data_dir), *argv)
    assert code == 0, err
    return out


def submit(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    build: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    """``fetch`` and ``context`` for :data:`VIDEO`, a proposal of ``build(context)``'s
    tips, then ``propose``."""

    cli_ok(capsys, data_dir, "fetch", f"https://www.youtube.com/watch?v={VIDEO}")
    ctx = json.loads(cli_ok(capsys, data_dir, "context", "--source-ref", VIDEO_REF))
    document = {
        "schema": proposal.PROPOSAL_SCHEMA,
        "source_ref": VIDEO_REF,
        "packet_sha256": ctx["source"]["packet_sha256"],
        "skill_sha256": ctx["skill_sha256"],
        "host": limits.HOST_CLAUDE_CODE,
        "model": "synthetic-fixture",
        "mode": limits.SOURCE_MODE_EXTRACT,
        "degraded": False,
        "tips": build(ctx),
    }
    draft = data_dir / "proposals" / f"draft-{paths.source_key(VIDEO_REF)}.json"
    draft.write_bytes(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
    out = cli_ok(capsys, data_dir, "propose", str(draft))
    assert out.splitlines()[-1] == f"Review: {proposal.REVIEW_URL}"


def proposed_tip(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reason": "A small, repeatable step that is easy to attach to a cue.",
        "list_ids": [],
        "start_s": 12,
        "locator": None,
        "evidence_url": None,
        "duplicate_of": None,
        "possible_duplicate_of": None,
        "note": None,
    }
    return {**base, **fields}


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A data directory made by ``init``: seeded buckets, lists 1-7 and the manual source."""

    target = tmp_path / "data"
    cli_ok(capsys, target, "init")
    return target


@pytest.fixture
def client(data_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    dist = tmp_path / "dist"
    dist.mkdir()
    app = create_app(data_dir / paths.DB_FILENAME, dist_dir=dist)
    with TestClient(app, base_url=LOCAL_URL) as test_client:
        yield test_client


def query(data_dir: Path, sql: str, *params: Any) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def dump(data_dir: Path) -> list[str]:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        return list(conn.iterdump())


def ok_json(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def add(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/tips`` that must succeed; returns the stored tip."""

    response = client.post("/api/tips", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def sheet(client: TestClient, list_id: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = ok_json(client.get(f"/api/print/lists/{list_id}"))["items"]
    return items


def printed(tip: dict[str, Any]) -> dict[str, str]:
    return {"name": tip["name"], "action": tip["action"]}


def manual_source_id(data_dir: Path) -> int:
    rows = query(data_dir, "SELECT id FROM source WHERE ref = ?", seed.MANUAL_SOURCE_REF)
    source_id: int = rows[0]["id"]
    return source_id


def assert_error(response: Any, status: int, code: str, fields: list[str]) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["fields"] == fields
    assert error["message"]


# --- POST /tips: Add my own tip ---------------------------------------------------------------


def test_brush_your_teeth_prints_on_lists_3_and_1_at_once(
    client: TestClient, data_dir: Path
) -> None:
    assert sheet(client, KIDS_BATHROOM) == []
    assert sheet(client, ADULT_BATHROOM) == []

    tip = add(client, BRUSH)

    assert sheet(client, KIDS_BATHROOM) == [printed(BRUSH)]
    assert sheet(client, ADULT_BATHROOM) == [printed(BRUSH)]
    manual = manual_source_id(data_dir)
    assert tip == {
        **tip,
        "name": BRUSH["name"],
        "action": BRUSH["action"],
        "reason": None,
        "bucket": limits.BUCKET_DAILY,
        "audience": limits.AUDIENCE_KIDS_PLUS,
        "status": limits.TIP_STATUS_APPROVED,
        "source_id": manual,
        "source_ref": seed.MANUAL_SOURCE_REF,
        "start_s": None,
        "start_url": None,
        "duplicate_of": None,
        "possible_duplicate_of": None,
        "note": None,
        "list_ids": [ADULT_BATHROOM, KIDS_BATHROOM],
        "created_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
    }
    row = query(
        data_dir,
        "SELECT tip.status, tip.reason, source.kind FROM tip "
        "JOIN source ON source.id = tip.source_id WHERE tip.id = ?",
        tip["id"],
    )
    assert row == [
        {"status": "approved", "reason": None, "kind": limits.SOURCE_KIND_MANUAL},
    ]
    # The Library's source filter finds it among your own tips.
    mine = ok_json(client.get("/api/tips", params={"source_id": manual}))
    assert [item["id"] for item in mine] == [tip["id"]]
    sources = {source["ref"]: source for source in ok_json(client.get("/api/sources"))}
    assert sources[seed.MANUAL_SOURCE_REF]["tip_counts"] == {"pending": 0, "approved": 1}
    # Nothing waits for review: an own tip is approved when it is added.
    assert ok_json(client.get("/api/tips", params={"status": "pending"})) == []


def test_an_added_tip_goes_to_the_end_of_each_list_and_keeps_its_reason(
    client: TestClient, data_dir: Path
) -> None:
    face = add(client, FACE)
    brush = add(client, BRUSH)

    assert sheet(client, ADULT_BATHROOM) == [printed(FACE), printed(BRUSH)]
    assert sheet(client, KIDS_BATHROOM) == [printed(BRUSH)]
    assert face["reason"] == FACE["reason"]
    positions = query(
        data_dir,
        "SELECT tip_id, position FROM list_item WHERE list_id = ? ORDER BY position",
        ADULT_BATHROOM,
    )
    assert positions == [
        {"tip_id": face["id"], "position": 1},
        {"tip_id": brush["id"], "position": 2},
    ]


def test_an_added_tip_outside_bucket_1_goes_on_no_list(client: TestClient) -> None:
    optional = add(
        client,
        {
            "name": "Label the fuse box",
            "action": "Write each fuse's room on the box door",
            "bucket": limits.BUCKET_OPTIONAL,
            "audience": limits.AUDIENCE_ADULTS,
        },
    )

    assert optional["list_ids"] == []
    assert optional["status"] == limits.TIP_STATUS_APPROVED
    for item in ok_json(client.get("/api/lists")):
        assert sheet(client, item["id"]) == []


def test_an_added_tip_that_breaks_a_rule_stores_nothing(client: TestClient, data_dir: Path) -> None:
    before = dump(data_dir)
    longest = {"name": "n" * limits.NAME_MAX, "action": "a" * limits.ACTION_MAX}
    no_action = {key: value for key, value in BRUSH.items() if key != "action"}
    conflict, invalid = "CC_E_CONFLICT", "CC_E_VALIDATION"
    for body, status, code, fields in (
        ({**BRUSH, "audience": limits.AUDIENCE_ADULTS}, 409, conflict, ["list_ids"]),
        ({**BRUSH, "bucket": limits.BUCKET_OCCASIONAL}, 409, conflict, ["list_ids"]),
        ({**BRUSH, "list_ids": [99]}, 400, invalid, ["list_ids"]),
        ({**BRUSH, "list_ids": [1, 1]}, 400, invalid, ["list_ids"]),
        ({**BRUSH, "list_ids": [0]}, 400, invalid, ["list_ids"]),
        ({**BRUSH, **longest}, 400, invalid, ["name", "action"]),
        ({**BRUSH, "name": ""}, 400, invalid, ["name"]),
        ({**BRUSH, "name": "x" * (limits.NAME_MAX + 1)}, 400, invalid, ["name"]),
        ({**BRUSH, "reason": ""}, 400, invalid, ["reason"]),
        ({**BRUSH, "reason": "two\nlines"}, 400, invalid, ["reason"]),
        ({**BRUSH, "bucket": 5}, 400, invalid, ["bucket"]),
        ({**BRUSH, "bucket": "1"}, 400, invalid, ["bucket"]),
        ({**BRUSH, "audience": limits.AUDIENCE_KIDS}, 400, invalid, ["audience"]),
        (no_action, 400, invalid, ["action"]),
        # A tip of your own is always approved and manual; the body cannot say otherwise.
        ({**BRUSH, "status": "pending"}, 400, invalid, ["status"]),
        ({**BRUSH, "source_id": 2}, 400, invalid, ["source_id"]),
        ({**BRUSH, "duplicate_of": 1}, 400, invalid, ["duplicate_of"]),
    ):
        assert_error(client.post("/api/tips", json=body), status, code, fields)
    assert dump(data_dir) == before


def test_adding_without_the_seeded_manual_source_fails_loudly(
    client: TestClient, data_dir: Path
) -> None:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        conn.execute("DELETE FROM source WHERE ref = ?", (seed.MANUAL_SOURCE_REF,))
        conn.commit()

    with pytest.raises(RuntimeError, match="checklist-care init"):
        client.post("/api/tips", json=BRUSH)
    assert query(data_dir, "SELECT count(*) AS n FROM tip") == [{"n": 0}]


# --- moving between buckets -----------------------------------------------------------------


def test_moving_it_to_bucket_3_takes_it_off_both_sheets(client: TestClient, data_dir: Path) -> None:
    face = add(client, FACE)
    brush = add(client, BRUSH)

    response = client.patch(f"/api/tips/{brush['id']}", json={"bucket": limits.BUCKET_OPTIONAL})

    result = ok_json(response)
    assert result["removed_placements"] == [ADULT_BATHROOM, KIDS_BATHROOM]
    assert result["tip"]["bucket"] == limits.BUCKET_OPTIONAL
    assert result["tip"]["list_ids"] == []
    assert sheet(client, KIDS_BATHROOM) == []
    assert sheet(client, ADULT_BATHROOM) == [printed(FACE)]
    assert query(data_dir, "SELECT list_id FROM list_item WHERE tip_id = ?", brush["id"]) == []
    # Still stored and approved: bucket 3 is browsed in the Library, never printed.
    optional = ok_json(client.get("/api/tips", params={"bucket": limits.BUCKET_OPTIONAL}))
    assert [(item["id"], item["status"]) for item in optional] == [
        (brush["id"], limits.TIP_STATUS_APPROVED)
    ]
    assert face["id"] in [item["id"] for item in ok_json(client.get("/api/tips?unplaced=false"))]


def test_moving_it_back_to_bucket_1_puts_it_on_the_lists_named(client: TestClient) -> None:
    brush = add(client, BRUSH)
    ok_json(client.patch(f"/api/tips/{brush['id']}", json={"bucket": limits.BUCKET_OPTIONAL}))

    result = ok_json(
        client.patch(
            f"/api/tips/{brush['id']}",
            json={"bucket": limits.BUCKET_DAILY, "list_ids": [KIDS_BATHROOM]},
        )
    )

    assert result["removed_placements"] == []
    assert sheet(client, KIDS_BATHROOM) == [printed(BRUSH)]
    assert sheet(client, ADULT_BATHROOM) == []


# --- DELETE /tips/{id}: the original of a duplicate -------------------------------------------


def test_deleting_a_tip_another_duplicates_clears_duplicate_of_and_appends_the_note(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brush = add(client, BRUSH)
    note = "duplicate of my own tip: the same action at bedtime"

    def build(ctx: dict[str, Any]) -> list[dict[str, Any]]:
        # `context` shows the skill your own tip, so a proposal can name it.
        known = {tip["name"]: tip for tip in ctx["tips"]}
        assert known[BRUSH["name"]]["source_ref"] == seed.MANUAL_SOURCE_REF
        assert known[BRUSH["name"]]["id"] == brush["id"]
        return [
            proposed_tip(
                name="Brush Before Bed",
                action="Brush your teeth for two minutes before bed",
                bucket=limits.BUCKET_LEFT_OUT,
                audience=limits.AUDIENCE_KIDS_PLUS,
                duplicate_of=brush["id"],
                note=note,
            ),
            proposed_tip(
                name="Teeth Twice A Day",
                action="Brush morning and night",
                bucket=limits.BUCKET_LEFT_OUT,
                audience=limits.AUDIENCE_KIDS_PLUS,
                start_s=75,
                duplicate_of=brush["id"],
            ),
            proposed_tip(
                name="Floss After Brushing",
                action="Floss once your teeth are brushed",
                bucket=limits.BUCKET_DAILY,
                audience=limits.AUDIENCE_KIDS_PLUS,
                list_ids=[KIDS_BATHROOM],
                start_s=140,
                possible_duplicate_of=brush["id"],
                note="near match: same moment, another action",
            ),
        ]

    submit(capsys, data_dir, build)
    left_out = ok_json(client.get("/api/tips", params={"bucket": limits.BUCKET_LEFT_OUT}))
    assert {item["name"]: item["duplicate_of"] for item in left_out} == {
        "Brush Before Bed": brush["id"],
        "Teeth Twice A Day": brush["id"],
    }

    response = client.delete(f"/api/tips/{brush['id']}")

    assert response.status_code == 204
    assert query(data_dir, "SELECT id FROM tip WHERE id = ?", brush["id"]) == []
    marker = f'(original #{brush["id"]} "{BRUSH["name"]}" deleted)'
    assert tip_store.deleted_original_note(brush["id"], BRUSH["name"]) == marker
    rows = query(
        data_dir,
        "SELECT name, duplicate_of, possible_duplicate_of, note FROM tip ORDER BY id",
    )
    assert rows == [
        {
            "name": "Brush Before Bed",
            "duplicate_of": None,
            "possible_duplicate_of": None,
            "note": f"{note} {marker}",
        },
        {
            "name": "Teeth Twice A Day",
            "duplicate_of": None,
            "possible_duplicate_of": None,
            "note": marker,
        },
        # A near match keeps its own note; only duplicates name their deleted original.
        {
            "name": "Floss After Brushing",
            "duplicate_of": None,
            "possible_duplicate_of": None,
            "note": "near match: same moment, another action",
        },
    ]
    # The Library shows the survivors without a dangling badge.
    for item in ok_json(client.get("/api/tips")):
        assert item["duplicate_of_tip"] is None
        assert item["possible_duplicate_of_tip"] is None
    assert sheet(client, ADULT_BATHROOM) == []
    assert [item["name"] for item in ok_json(client.get(f"/api/lists/{KIDS_BATHROOM}/items"))] == [
        "Floss After Brushing"
    ]


# --- the demo the Library page is reviewed against ---------------------------------------------


def serve_demo(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe: Callable[[TestClient], None],
) -> None:
    """Run ``checklist-care serve --demo`` with a ``run_server`` stand-in that calls
    ``probe`` with a client of the app it was given."""

    def fake_run_server(config: uvicorn.Config) -> None:
        assert isinstance(config.app, FastAPI)
        with TestClient(config.app, base_url=LOCAL_URL) as test_client:
            probe(test_client)

    monkeypatch.setattr(run, "run_server", fake_run_server)
    code, _, err = run_cli(capsys, "serve", "--demo", "--port", "8451")
    assert code == 0, err


def test_the_demo_library_has_tips_to_filter_and_takes_an_own_tip(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def probe(test_client: TestClient) -> None:
        seen["tips"] = ok_json(test_client.get("/api/tips"))
        seen["sources"] = ok_json(test_client.get("/api/sources"))
        response = test_client.post(
            "/api/tips", json={**BRUSH, "name": "Hang up your towel", "list_ids": [3]}
        )
        assert response.status_code == 201, response.text
        seen["kids"] = ok_json(test_client.get("/api/print/lists/3"))["items"]

    serve_demo(capsys, monkeypatch, probe)

    tips = seen["tips"]
    assert {tip["bucket"] for tip in tips} == set(limits.BUCKET_IDS)
    assert {tip["status"] for tip in tips} == set(limits.TIP_STATUSES)
    assert {tip["audience"] for tip in tips} == set(limits.AUDIENCES_TIP)
    assert len({tip["source_id"] for tip in tips}) >= 3
    assert any(tip["source_ref"] == seed.MANUAL_SOURCE_REF for tip in tips)
    assert {source["ref"] for source in seen["sources"]} >= {seed.MANUAL_SOURCE_REF}
    assert seen["kids"][-1] == {"name": "Hang up your towel", "action": BRUSH["action"]}
