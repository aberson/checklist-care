"""The review inbox API (plan §6, Step 6): ``/api/tips``, ``/api/review/*``,
``/api/lists`` and ``/api/sources`` through ``TestClient``.

The database is filled by the production producer, never by raw inserts:
``cli.main`` runs ``init``, then ``fetch`` (the two network seams faked from
``tests/fixtures/packet_youtube_synthetic.json``), ``context`` and ``propose`` for
two videos. The first proposal is ``tests/fixtures/proposal_valid.json``; the
second one, built here, holds a duplicate of a first-video tip and a possible
duplicate of another.

The demo seed (``server/demo.py``) is checked through ``serve --demo``, and the
web page test's demo fixture (``web/src/fixtures/demoReview.json``) must equal what
the demo server returns. To regenerate that file after a deliberate demo change,
run ``CHECKLIST_CARE_WRITE_DEMO_FIXTURE=1 uv run pytest -k demo_fixture``.
"""

from __future__ import annotations

import json
import os
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
from checklist_care.db import connection
from checklist_care.server import demo, run
from checklist_care.server.app import create_app
from checklist_care.sources import youtube
from checklist_care.store import lists as list_store
from checklist_care.store import tips as tip_store

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
PACKET_FIXTURE = FIXTURES / "packet_youtube_synthetic.json"
PROPOSAL_FIXTURE = FIXTURES / "proposal_valid.json"
WEB_DEMO_FIXTURE = ROOT / "web" / "src" / "fixtures" / "demoReview.json"
WRITE_DEMO_FIXTURE_ENV = "CHECKLIST_CARE_WRITE_DEMO_FIXTURE"

FIRST_VIDEO = "LpeDuC2sQ-w"
SECOND_VIDEO = "EXAMPLE0002"
FIRST_REF = youtube.source_ref(FIRST_VIDEO)
SECOND_REF = youtube.source_ref(SECOND_VIDEO)
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 8, 13, 30, 0, tzinfo=UTC)
LATER_TEXT = "2026-11-08T13:30:00Z"
LOCAL_URL = "http://127.0.0.1:8450"
"""TestClient's default ``testserver`` Host is not one the app accepts."""

KIDS_LIST = 3  # Kids Bathroom / kids
OFFICE_LIST = 2  # My Office / adults
BATHROOM_LIST = 1  # Adult Bathroom / adults


# --- the production producer: init, fetch, context, propose -------------------------------


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
    capsys: pytest.CaptureFixture[str], data_dir: Path, ref: str, build: Callable[..., Any]
) -> None:
    """``context`` for ``ref``, a proposal from ``build(context)``, then ``propose``."""

    ctx = json.loads(cli_ok(capsys, data_dir, "context", "--source-ref", ref))
    document = build(ctx)
    draft = data_dir / "proposals" / f"draft-{paths.source_key(ref)}.json"
    draft.write_bytes(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
    out = cli_ok(capsys, data_dir, "propose", str(draft))
    assert out.splitlines()[-1] == f"Review: {proposal.REVIEW_URL}"


def first_proposal(ctx: dict[str, Any]) -> dict[str, Any]:
    document = fixture_json(PROPOSAL_FIXTURE)
    assert document["packet_sha256"] == ctx["source"]["packet_sha256"]
    document["skill_sha256"] = ctx["skill_sha256"]
    return document


def tip_id_in(ctx: dict[str, Any], name: str) -> int:
    matches = [tip["id"] for tip in ctx["tips"] if tip["name"] == name]
    assert len(matches) == 1, name
    tip_id: int = matches[0]
    return tip_id


def second_proposal(ctx: dict[str, Any]) -> dict[str, Any]:
    """Self-written tips for the second video: a duplicate of "Your Brain Is A Raisin",
    a possible duplicate of "Sit Up Straight, You're Not Dead Yet", and a kids tip."""

    def tip(**fields: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "reason": "A small, repeatable step that is easy to attach to a cue.",
            "list_ids": [],
            "locator": None,
            "evidence_url": None,
            "duplicate_of": None,
            "possible_duplicate_of": None,
            "note": None,
        }
        return {**base, **fields}

    return {
        "schema": proposal.PROPOSAL_SCHEMA,
        "source_ref": SECOND_REF,
        "packet_sha256": ctx["source"]["packet_sha256"],
        "skill_sha256": ctx["skill_sha256"],
        "host": limits.HOST_CODEX,
        "model": "synthetic-fixture",
        "mode": limits.SOURCE_MODE_EXTRACT,
        "degraded": False,
        "tips": [
            tip(
                name="Water Before Anything",
                action="Drink a glass of water first thing",
                bucket=limits.BUCKET_LEFT_OUT,
                audience=limits.AUDIENCE_KIDS_PLUS,
                start_s=12,
                duplicate_of=tip_id_in(ctx, "Your Brain Is A Raisin"),
                note="duplicate: the same action at the same cue",
            ),
            tip(
                name="Shoulder Rolls At The Desk",
                action="Roll your shoulders back five times at the desk",
                bucket=limits.BUCKET_DAILY,
                audience=limits.AUDIENCE_ADULTS,
                list_ids=[OFFICE_LIST],
                start_s=75,
                possible_duplicate_of=tip_id_in(ctx, "Sit Up Straight, You're Not Dead Yet"),
                note="near match: same goal, another cue",
            ),
            tip(
                name="Hang Up The Towel",
                action="Hang your towel on the hook after washing",
                bucket=limits.BUCKET_DAILY,
                audience=limits.AUDIENCE_KIDS_PLUS,
                list_ids=[KIDS_LIST],
                start_s=140,
            ),
        ],
    }


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A data directory filled by init, fetch, context and propose for two videos."""

    target = tmp_path / "data"
    cli_ok(capsys, target, "init")
    cli_ok(capsys, target, "fetch", f"https://www.youtube.com/watch?v={FIRST_VIDEO}")
    submit(capsys, target, FIRST_REF, first_proposal)
    cli_ok(capsys, target, "fetch", f"https://youtu.be/{SECOND_VIDEO}")
    submit(capsys, target, SECOND_REF, second_proposal)
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


def placements_of(data_dir: Path, tip_id: int) -> list[int]:
    rows = query(data_dir, "SELECT list_id FROM list_item WHERE tip_id = ? ORDER BY 1", tip_id)
    return [row["list_id"] for row in rows]


def tips_by_name(client: TestClient, **filters: Any) -> dict[str, dict[str, Any]]:
    response = client.get("/api/tips", params=filters)
    assert response.status_code == 200, response.text
    return {tip["name"]: tip for tip in response.json()}


def source_ids(client: TestClient) -> dict[str, int]:
    return {source["ref"]: source["id"] for source in client.get("/api/sources").json()}


def assert_error(response: Any, status: int, code: str, fields: list[str]) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["fields"] == fields
    assert error["message"]


FIRST_NAMES = [tip["name"] for tip in fixture_json(PROPOSAL_FIXTURE)["tips"]]
SECOND_NAMES = ["Water Before Anything", "Shoulder Rolls At The Desk", "Hang Up The Towel"]


# --- GET /tips and GET /sources: pending tips by source --------------------------------------


def test_pending_tips_list_by_source(client: TestClient) -> None:
    sources = {source["ref"]: source for source in client.get("/api/sources").json()}

    assert set(sources) == {"manual", FIRST_REF, SECOND_REF}
    first, second = sources[FIRST_REF], sources[SECOND_REF]
    assert first["tip_counts"] == {"pending": 10, "approved": 0}
    assert second["tip_counts"] == {"pending": 3, "approved": 0}
    assert sources["manual"]["tip_counts"] == {"pending": 0, "approved": 0}
    assert first["url"] == youtube.watch_url(FIRST_VIDEO)
    assert first["kind"] == limits.SOURCE_KIND_YOUTUBE
    assert first["mode"] == limits.SOURCE_MODE_EXTRACT
    assert first["reviewed_at"] == "2026-11-08T12:00:00Z"
    assert first["title"] == fixture_json(PACKET_FIXTURE)["title"]

    pending = client.get("/api/tips", params={"status": "pending"}).json()
    by_source: dict[int, list[str]] = {}
    for tip in pending:
        assert tip["status"] == limits.TIP_STATUS_PENDING
        by_source.setdefault(tip["source_id"], []).append(tip["name"])
    assert by_source == {first["id"]: FIRST_NAMES, second["id"]: SECOND_NAMES}

    for source, names in ((first, FIRST_NAMES), (second, SECOND_NAMES)):
        response = client.get("/api/tips", params={"status": "pending", "source_id": source["id"]})
        assert response.status_code == 200
        assert [tip["name"] for tip in response.json()] == names
        assert {tip["source_ref"] for tip in response.json()} == {source["ref"]}


def test_a_listed_tip_carries_its_lists_link_and_duplicate_views(client: TestClient) -> None:
    tips = tips_by_name(client, status="pending")
    raisin = tips["Your Brain Is A Raisin"]
    duplicate = tips["Water Before Anything"]
    near = tips["Shoulder Rolls At The Desk"]

    assert raisin["list_ids"] == [KIDS_LIST, 7]
    assert raisin["start_url"] == f"https://www.youtube.com/watch?v={FIRST_VIDEO}&t=503s"
    assert duplicate["start_url"] == f"https://www.youtube.com/watch?v={SECOND_VIDEO}&t=12s"
    assert duplicate["bucket"] == limits.BUCKET_LEFT_OUT
    assert duplicate["duplicate_of"] == raisin["id"]
    assert duplicate["duplicate_of_tip"] == {
        "id": raisin["id"],
        "name": raisin["name"],
        "action": raisin["action"],
        "bucket": raisin["bucket"],
        "status": limits.TIP_STATUS_PENDING,
        "source_id": raisin["source_id"],
    }
    assert duplicate["possible_duplicate_of_tip"] is None
    sit_up = tips["Sit Up Straight, You're Not Dead Yet"]
    assert near["possible_duplicate_of"] == sit_up["id"]
    assert near["possible_duplicate_of_tip"]["name"] == sit_up["name"]
    assert near["duplicate_of_tip"] is None
    assert set(raisin) == {
        "id",
        "name",
        "action",
        "reason",
        "bucket",
        "audience",
        "status",
        "source_id",
        "source_ref",
        "start_s",
        "start_url",
        "locator",
        "evidence_url",
        "duplicate_of",
        "duplicate_of_tip",
        "possible_duplicate_of",
        "possible_duplicate_of_tip",
        "note",
        "list_ids",
        "created_at",
        "updated_at",
    }


def test_tip_filters_combine_and_reject_values_outside_the_enums(client: TestClient) -> None:
    daily_adults = tips_by_name(client, bucket=limits.BUCKET_DAILY, audience="adults")
    assert sorted(daily_adults) == sorted(
        ["Fire Your Morning Brain", "The Two-Minute Brain Trick", "Shoulder Rolls At The Desk"]
    )
    assert tips_by_name(client, status="approved") == {}

    for params, field_name in (
        ({"status": "done"}, "status"),
        ({"bucket": 5}, "bucket"),
        ({"audience": "kids"}, "audience"),
        ({"bucket": "one"}, "bucket"),
        ({"source_id": 0}, "source_id"),
    ):
        assert_error(client.get("/api/tips", params=params), 400, "CC_E_VALIDATION", [field_name])


def test_lists_carry_title_counts_and_capacity(client: TestClient) -> None:
    lists = client.get("/api/lists").json()

    assert [item["id"] for item in lists] == [1, 2, 3, 4, 5, 6, 7]
    coffee = lists[6]
    assert coffee == {
        "id": 7,
        "place": "Coffee Machine",
        "audience": "adults",
        "title": "Coffee Machine — Adults",
        "cue": "First coffee or hot drink of the day",
        "sort_order": 7,
        "item_count": 1,
        "over_capacity": False,
    }
    counts = {item["id"]: item["item_count"] for item in lists}
    assert counts == {1: 1, 2: 2, 3: 2, 4: 0, 5: 1, 6: 1, 7: 1}


# --- PATCH /tips/{id} --------------------------------------------------------------------------


def test_patch_from_bucket_1_to_2_returns_its_placements(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = tips_by_name(client)["Step Into Morning Daylight"]
    assert tip["bucket"] == limits.BUCKET_DAILY
    assert placements_of(data_dir, tip["id"]) == [5, 6]
    monkeypatch.setattr(connection, "utc_now", lambda: LATER)

    response = client.patch(f"/api/tips/{tip['id']}", json={"bucket": limits.BUCKET_OCCASIONAL})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed_placements"] == [5, 6]
    assert body["cleared_duplicate_of"] is None
    assert body["tip"]["bucket"] == limits.BUCKET_OCCASIONAL
    assert body["tip"]["list_ids"] == []
    assert body["tip"]["updated_at"] == LATER_TEXT
    assert placements_of(data_dir, tip["id"]) == []
    row = query(data_dir, "SELECT bucket, updated_at FROM tip WHERE id = ?", tip["id"])[0]
    assert row == {"bucket": limits.BUCKET_OCCASIONAL, "updated_at": LATER_TEXT}


def test_becoming_adults_drops_only_the_kids_lists(client: TestClient, data_dir: Path) -> None:
    tip = tips_by_name(client)["Your Brain Is A Raisin"]

    response = client.patch(f"/api/tips/{tip['id']}", json={"audience": "adults"})

    assert response.status_code == 200, response.text
    assert response.json()["removed_placements"] == [KIDS_LIST]
    assert response.json()["tip"]["list_ids"] == [7]
    assert placements_of(data_dir, tip["id"]) == [7]


def test_an_explicit_list_ids_replaces_the_placements_and_reports_none(
    client: TestClient, data_dir: Path
) -> None:
    tip = tips_by_name(client)["The Two-Minute Brain Trick"]  # adults, daily, unplaced

    response = client.patch(f"/api/tips/{tip['id']}", json={"list_ids": [OFFICE_LIST, 6]})
    assert response.status_code == 200, response.text
    assert response.json()["removed_placements"] == []
    assert placements_of(data_dir, tip["id"]) == [OFFICE_LIST, 6]
    # Appended at the end of each list.
    office = query(
        data_dir, "SELECT tip_id FROM list_item WHERE list_id = ? ORDER BY position", OFFICE_LIST
    )
    assert office[-1]["tip_id"] == tip["id"]

    response = client.patch(f"/api/tips/{tip['id']}", json={"list_ids": [6]})
    assert response.status_code == 200
    assert response.json()["removed_placements"] == []
    assert placements_of(data_dir, tip["id"]) == [6]


def test_an_explicit_adults_tip_on_a_kids_list_is_a_409(client: TestClient, data_dir: Path) -> None:
    tips = tips_by_name(client)
    adults = tips["Fire Your Morning Brain"]
    kids = tips["Hang Up The Towel"]
    before = dump(data_dir)

    on_kids_list = client.patch(
        f"/api/tips/{adults['id']}", json={"list_ids": [BATHROOM_LIST, KIDS_LIST]}
    )
    becoming_adults = client.patch(
        f"/api/tips/{kids['id']}", json={"audience": "adults", "list_ids": [KIDS_LIST]}
    )
    not_daily = client.patch(
        f"/api/tips/{kids['id']}", json={"bucket": limits.BUCKET_OPTIONAL, "list_ids": [6]}
    )

    for response in (on_kids_list, becoming_adults, not_daily):
        assert_error(response, 409, "CC_E_CONFLICT", ["list_ids"])
    assert "kids" in on_kids_list.json()["error"]["message"]
    assert dump(data_dir) == before


def test_unknown_or_repeated_lists_are_a_400(client: TestClient, data_dir: Path) -> None:
    tip = tips_by_name(client)["Fire Your Morning Brain"]
    before = dump(data_dir)

    for list_ids in ([99], [BATHROOM_LIST, BATHROOM_LIST], [0], ["1"]):
        response = client.patch(f"/api/tips/{tip['id']}", json={"list_ids": list_ids})
        assert_error(response, 400, "CC_E_VALIDATION", ["list_ids"])
    assert dump(data_dir) == before


def test_an_explicit_duplicate_of_with_bucket_2_is_a_400(
    client: TestClient, data_dir: Path
) -> None:
    tips = tips_by_name(client)
    original = tips["The Accountability Text"]
    activity = tips["Unfollow Your Ghosts"]  # bucket 2
    daily = tips["Fire Your Morning Brain"]
    before = dump(data_dir)

    with_bucket = client.patch(
        f"/api/tips/{daily['id']}",
        json={"bucket": limits.BUCKET_OCCASIONAL, "duplicate_of": original["id"]},
    )
    stored_bucket_2 = client.patch(
        f"/api/tips/{activity['id']}", json={"duplicate_of": original["id"]}
    )

    for response in (with_bucket, stored_bucket_2):
        assert_error(response, 400, "CC_E_VALIDATION", ["duplicate_of"])
    assert dump(data_dir) == before


def test_moving_a_duplicate_to_bucket_1_clears_duplicate_of(
    client: TestClient, data_dir: Path
) -> None:
    tips = tips_by_name(client)
    duplicate = tips["Water Before Anything"]
    original_id = tips["Your Brain Is A Raisin"]["id"]
    assert duplicate["duplicate_of"] == original_id

    response = client.patch(f"/api/tips/{duplicate['id']}", json={"bucket": limits.BUCKET_DAILY})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cleared_duplicate_of"] == original_id
    assert body["tip"]["duplicate_of"] is None
    assert body["tip"]["duplicate_of_tip"] is None
    assert body["tip"]["bucket"] == limits.BUCKET_DAILY
    assert body["removed_placements"] == []
    row = query(data_dir, "SELECT bucket, duplicate_of FROM tip WHERE id = ?", duplicate["id"])
    assert row == [{"bucket": limits.BUCKET_DAILY, "duplicate_of": None}]


def test_naming_duplicate_of_null_clears_it_without_a_report(client: TestClient) -> None:
    tips = tips_by_name(client)
    duplicate = tips["Water Before Anything"]

    response = client.patch(
        f"/api/tips/{duplicate['id']}",
        json={"bucket": limits.BUCKET_OPTIONAL, "duplicate_of": None},
    )

    assert response.status_code == 200, response.text
    assert response.json()["cleared_duplicate_of"] is None
    assert response.json()["tip"]["duplicate_of"] is None


def test_a_bucket_4_tip_can_be_marked_a_duplicate(client: TestClient) -> None:
    tips = tips_by_name(client)
    rejected = tips["Make Laziness Your Superpower"]  # bucket 4, no duplicate_of
    original = tips["The Golden Rule of Habit-Building"]

    response = client.patch(f"/api/tips/{rejected['id']}", json={"duplicate_of": original["id"]})

    assert response.status_code == 200, response.text
    assert response.json()["tip"]["duplicate_of_tip"]["name"] == original["name"]
    assert response.json()["cleared_duplicate_of"] is None


def test_text_edits_follow_the_proposal_rules(client: TestClient, data_dir: Path) -> None:
    tip = tips_by_name(client)["Fire Your Morning Brain"]
    url = f"/api/tips/{tip['id']}"
    before = dump(data_dir)

    cases: list[tuple[dict[str, Any], list[str]]] = [
        ({"name": "x" * (limits.NAME_MAX + 1)}, ["name"]),
        ({"action": "  "}, ["action"]),
        ({"reason": "two\nlines"}, ["reason"]),
        ({"note": "y" * (limits.NOTE_MAX + 1)}, ["note"]),
        ({"name": "N" * limits.NAME_MAX, "action": "A" * limits.ACTION_MAX}, ["name", "action"]),
        ({"reason": None}, ["reason"]),  # only a manual tip may have no reason
        ({"name": None}, ["name"]),
        ({"bucket": True}, ["bucket"]),
        ({"bucket": 5}, ["bucket"]),
        ({"audience": "kids"}, ["audience"]),
        ({"status": "done"}, ["status"]),
        ({"source_id": 1}, ["source_id"]),
        ({"possible_duplicate_of": 9999}, ["possible_duplicate_of"]),
        ({"possible_duplicate_of": tip["id"]}, ["possible_duplicate_of"]),
    ]
    for body, fields in cases:
        assert_error(client.patch(url, json=body), 400, "CC_E_VALIDATION", fields)
    assert dump(data_dir) == before

    edited = client.patch(
        url,
        json={
            "name": "Cold Splash Wake-Up",
            "action": "Splash cold water on your face",
            "reason": "A quick cold splash is an easy wake-up cue.",
            "note": None,
        },
    )
    assert edited.status_code == 200, edited.text
    stored = query(data_dir, "SELECT name, action, reason, note FROM tip WHERE id = ?", tip["id"])
    assert stored == [
        {
            "name": "Cold Splash Wake-Up",
            "action": "Splash cold water on your face",
            "reason": "A quick cold splash is an easy wake-up cue.",
            "note": None,
        }
    ]


def test_patch_and_delete_of_a_missing_tip_are_404(client: TestClient) -> None:
    assert_error(client.patch("/api/tips/9999", json={"name": "Gone"}), 404, "CC_E_NOT_FOUND", [])
    assert_error(client.delete("/api/tips/9999"), 404, "CC_E_NOT_FOUND", [])
    assert_error(client.patch("/api/tips/abc", json={}), 400, "CC_E_VALIDATION", ["tip_id"])


def test_delete_notes_the_original_on_its_duplicates(client: TestClient, data_dir: Path) -> None:
    tips = tips_by_name(client)
    original = tips["Your Brain Is A Raisin"]
    duplicate = tips["Water Before Anything"]

    response = client.delete(f"/api/tips/{original['id']}")

    assert response.status_code == 204
    assert query(data_dir, "SELECT id FROM tip WHERE id = ?", original["id"]) == []
    assert placements_of(data_dir, original["id"]) == []
    row = query(data_dir, "SELECT duplicate_of, note FROM tip WHERE id = ?", duplicate["id"])[0]
    assert row["duplicate_of"] is None
    assert row["note"] == (
        "duplicate: the same action at the same cue "
        f"{tip_store.deleted_original_note(original['id'], original['name'])}"
    )
    assert tip_store.deleted_original_note(6, "Brush") == '(original #6 "Brush" deleted)'


# --- POST /review/approve and /review/left-out ---------------------------------------------------


def test_approve_flips_status(client: TestClient, data_dir: Path) -> None:
    sources = source_ids(client)
    second = client.get(
        "/api/tips", params={"status": "pending", "source_id": sources[SECOND_REF]}
    ).json()
    ids = [tip["id"] for tip in second]

    response = client.post("/api/review/approve", json={"tip_ids": ids})

    assert response.status_code == 200, response.text
    assert response.json() == {"approved": 3}
    rows = query(data_dir, "SELECT status FROM tip WHERE source_id = ?", sources[SECOND_REF])
    assert {row["status"] for row in rows} == {limits.TIP_STATUS_APPROVED}
    pending = tips_by_name(client, status="pending")
    assert sorted(pending) == sorted(FIRST_NAMES)
    assert sorted(tips_by_name(client, status="approved")) == sorted(SECOND_NAMES)
    # Placements stay; approving twice changes nothing.
    assert placements_of(data_dir, ids[2]) == [KIDS_LIST]
    assert client.post("/api/review/approve", json={"tip_ids": ids}).json() == {"approved": 0}


def test_left_out_sets_bucket_4_and_approved(client: TestClient, data_dir: Path) -> None:
    tip = tips_by_name(client)["Your Brain Is A Raisin"]
    assert placements_of(data_dir, tip["id"]) == [KIDS_LIST, 7]

    response = client.post("/api/review/left-out", json={"tip_ids": [tip["id"]]})

    assert response.status_code == 200, response.text
    assert response.json() == {"left_out": 1}
    row = query(data_dir, "SELECT bucket, status FROM tip WHERE id = ?", tip["id"])[0]
    assert row == {"bucket": limits.BUCKET_LEFT_OUT, "status": limits.TIP_STATUS_APPROVED}
    assert placements_of(data_dir, tip["id"]) == []
    assert "Your Brain Is A Raisin" not in tips_by_name(client, status="pending")
    # Nothing is deleted: the rejected idea is still stored.
    assert tips_by_name(client, bucket=limits.BUCKET_LEFT_OUT)["Your Brain Is A Raisin"]


def test_review_of_an_unknown_tip_is_a_404_and_changes_nothing(
    client: TestClient, data_dir: Path
) -> None:
    known = tips_by_name(client)["Fire Your Morning Brain"]["id"]
    before = dump(data_dir)

    for path in ("/api/review/approve", "/api/review/left-out"):
        response = client.post(path, json={"tip_ids": [known, 9999]})
        assert_error(response, 404, "CC_E_NOT_FOUND", ["tip_ids"])
        assert "#9999" in response.json()["error"]["message"]
    for body in ({"tip_ids": ["1"]}, {"ids": [known]}, {"tip_ids": [0]}):
        response = client.post("/api/review/approve", json=body)
        assert response.status_code == 400, response.text
    assert dump(data_dir) == before


# --- the demo seed, through `serve --demo` ---------------------------------------------------


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


def demo_payload(test_client: TestClient) -> dict[str, Any]:
    """What the review, lists and buckets pages load from the demo server: ``/api/meta``
    (without the version, so a release does not change the fixture), the buckets, the
    sources, the lists, the pending tips, each list's items (keyed by list id) and the
    approved daily tips on no list (``/api/tips?unplaced=true``)."""

    meta = test_client.get("/api/meta").json()
    del meta["version"]
    lists = test_client.get("/api/lists").json()
    return {
        "meta": meta,
        "buckets": test_client.get("/api/buckets").json(),
        "sources": test_client.get("/api/sources").json(),
        "lists": lists,
        "tips": test_client.get("/api/tips", params={"status": "pending"}).json(),
        "list_items": {
            str(item["id"]): test_client.get(f"/api/lists/{item['id']}/items").json()
            for item in lists
        },
        "unplaced": test_client.get("/api/tips", params={"unplaced": "true"}).json(),
    }


def test_the_demo_inbox_has_pending_tips_in_every_bucket_with_both_duplicates(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    serve_demo(capsys, monkeypatch, lambda test_client: seen.update(demo_payload(test_client)))

    pending = seen["tips"]
    assert len(pending) >= 5
    assert {tip["bucket"] for tip in pending} == set(limits.BUCKET_IDS)
    assert len({tip["source_id"] for tip in pending}) >= 2
    duplicates = [tip for tip in pending if tip["duplicate_of"] is not None]
    near = [tip for tip in pending if tip["possible_duplicate_of"] is not None]
    assert duplicates and near
    for tip in duplicates:
        assert tip["bucket"] == limits.BUCKET_LEFT_OUT
        assert tip["duplicate_of_tip"]["status"] == limits.TIP_STATUS_APPROVED
    assert all(tip["possible_duplicate_of_tip"] is not None for tip in near)
    assert any(tip["start_url"] for tip in pending)
    for tip in pending:
        assert proposal.line_problem(tip["name"], tip["action"]) is None


def test_every_demo_tip_passes_the_text_and_placement_rules(tmp_path: Path) -> None:
    db = demo.populate(tmp_path / "demo")
    with closing(connection.connect(db)) as conn:
        lists = list_store.lists_by_id(conn)
    for source in demo.DEMO_SOURCES:
        for tip in source.tips:
            assert demo.tip_problems(tip, lists) == [], tip.key


def test_the_demo_seeder_refuses_a_tip_that_breaks_a_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = demo.DemoTip(
        "bad",
        "Adults Tip",
        "Do it",
        "A reason.",
        limits.BUCKET_DAILY,
        limits.AUDIENCE_ADULTS,
        list_ids=(KIDS_LIST,),
    )
    source = demo.DemoSource(
        limits.SOURCE_KIND_YOUTUBE,
        youtube.source_ref("DEMObadtip1"),
        "Bad",
        limits.TIP_STATUS_PENDING,
        (bad,),
    )
    monkeypatch.setattr(demo, "DEMO_SOURCES", (source,))

    with pytest.raises(RuntimeError, match="kids list"):
        demo.populate(tmp_path / "demo")


def test_the_web_demo_fixture_is_the_demo_servers_payload(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    serve_demo(capsys, monkeypatch, lambda test_client: seen.update(demo_payload(test_client)))
    expected = json.dumps(seen, ensure_ascii=False, indent=2) + "\n"

    if os.environ.get(WRITE_DEMO_FIXTURE_ENV) == "1":
        WEB_DEMO_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        WEB_DEMO_FIXTURE.write_bytes(expected.encode("utf-8"))

    assert WEB_DEMO_FIXTURE.is_file(), f"run with {WRITE_DEMO_FIXTURE_ENV}=1 to create it"
    actual = WEB_DEMO_FIXTURE.read_bytes().decode("utf-8")
    assert actual == expected, (
        f"{WEB_DEMO_FIXTURE} differs from the demo server's payload; after a deliberate "
        f"demo change, regenerate it with {WRITE_DEMO_FIXTURE_ENV}=1 uv run pytest -k demo_fixture"
    )
