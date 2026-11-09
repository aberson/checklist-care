"""The print sheets and the read-only Lists API (plan §4.7, §6, Step 7) through ``TestClient``:
``/api/print/lists/{id}``, ``/api/print/activities``, ``/api/lists/{id}/items`` and
``/api/tips?unplaced=true``.

The database is filled by the production producer, never by raw inserts: ``cli.main``
runs ``init``, then ``fetch`` (the two network seams faked from
``tests/fixtures/packet_youtube_synthetic.json``), ``context`` and ``propose``. The first
proposal is ``tests/fixtures/proposal_valid.json``; the second, built here, adds a pending
daily tip on Coffee Machine — Adults, activities for kids+, an optional tip, a left-out
tip and an unplaced daily tip. Tips are approved and left out through the review API.

The demo's print data (``server/demo.py``) is checked through ``serve --demo``.
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

from checklist_care import cli, limits, paths, printing, proposal
from checklist_care.db import connection
from checklist_care.server import demo, run
from checklist_care.server.app import create_app
from checklist_care.sources import youtube
from checklist_care.store import lists as list_store

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
PACKET_FIXTURE = FIXTURES / "packet_youtube_synthetic.json"
PROPOSAL_FIXTURE = FIXTURES / "proposal_valid.json"

FIRST_VIDEO = "LpeDuC2sQ-w"
SECOND_VIDEO = "EXAMPLE0003"
CAPACITY_VIDEO = "EXAMPLE0004"
FIRST_REF = youtube.source_ref(FIRST_VIDEO)
SECOND_REF = youtube.source_ref(SECOND_VIDEO)
CAPACITY_REF = youtube.source_ref(CAPACITY_VIDEO)
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-08T12:00:00Z"
LOCAL_URL = "http://127.0.0.1:8450"
"""TestClient's default ``testserver`` Host is not one the app accepts."""

BATHROOM_LIST = 1  # Adult Bathroom / adults
OFFICE_LIST = 2  # My Office / adults
FRONT_DOOR_ADULTS = 6  # Front Door / adults
COFFEE_LIST = 7  # Coffee Machine / adults

PENDING_COFFEE = "Coffee Cup Calf Raises"
PENDING_UNPLACED = "Wrist Circles Before Typing"
PENDING_ACTIVITY = "Weekend Bike Ride"


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
    assert code == 0, (err, out)
    return out


def submit(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    video: str,
    build: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """``fetch``, ``context``, a proposal from ``build(context)``, then ``propose``."""

    cli_ok(capsys, data_dir, "fetch", f"https://www.youtube.com/watch?v={video}")
    ref = youtube.source_ref(video)
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


def proposal_for(ref: str, ctx: dict[str, Any], tips: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": proposal.PROPOSAL_SCHEMA,
        "source_ref": ref,
        "packet_sha256": ctx["source"]["packet_sha256"],
        "skill_sha256": ctx["skill_sha256"],
        "host": limits.HOST_CODEX,
        "model": "synthetic-fixture",
        "mode": limits.SOURCE_MODE_EXTRACT,
        "degraded": False,
        "tips": tips,
    }


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


def second_proposal(ctx: dict[str, Any]) -> dict[str, Any]:
    """Self-written tips: a daily tip for Coffee Machine — Adults, two kids+ activities,
    an optional tip, a left-out tip and a daily tip on no list."""

    return proposal_for(
        SECOND_REF,
        ctx,
        [
            tip(
                name=PENDING_COFFEE,
                action="Rise onto your toes ten times as the coffee pours",
                bucket=limits.BUCKET_DAILY,
                audience=limits.AUDIENCE_ADULTS,
                list_ids=[COFFEE_LIST],
                start_s=20,
            ),
            tip(
                name="Garden Afternoon",
                action="Spend a weekend afternoon in the garden together",
                bucket=limits.BUCKET_OCCASIONAL,
                audience=limits.AUDIENCE_KIDS_PLUS,
                start_s=60,
            ),
            tip(
                name=PENDING_ACTIVITY,
                action="Plan a bike ride together for Saturday morning",
                bucket=limits.BUCKET_OCCASIONAL,
                audience=limits.AUDIENCE_KIDS_PLUS,
                start_s=100,
            ),
            tip(
                name="Tidy The Spice Rack",
                action="Sort the spice jars by name once",
                bucket=limits.BUCKET_OPTIONAL,
                audience=limits.AUDIENCE_ADULTS,
                start_s=140,
            ),
            tip(
                name="Read The Kettle Manual",
                action="Read the kettle's manual from cover to cover",
                bucket=limits.BUCKET_LEFT_OUT,
                audience=limits.AUDIENCE_ADULTS,
                start_s=180,
            ),
            tip(
                name=PENDING_UNPLACED,
                action="Circle your wrists ten times before you type",
                bucket=limits.BUCKET_DAILY,
                audience=limits.AUDIENCE_ADULTS,
                start_s=220,
            ),
        ],
    )


def capacity_proposal(ctx: dict[str, Any]) -> dict[str, Any]:
    """``PRINT_CAPACITY + 1`` daily tips, all on Front Door — Adults."""

    return proposal_for(
        CAPACITY_REF,
        ctx,
        [
            tip(
                name=f"Door Habit {number}",
                action=f"Do the small door habit number {number} on the way out",
                bucket=limits.BUCKET_DAILY,
                audience=limits.AUDIENCE_ADULTS,
                list_ids=[FRONT_DOOR_ADULTS],
                start_s=number * 10,
            )
            for number in range(1, limits.PRINT_CAPACITY + 2)
        ],
    )


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A data directory filled by init, fetch, context and propose for two videos."""

    target = tmp_path / "data"
    cli_ok(capsys, target, "init")
    submit(capsys, target, FIRST_VIDEO, first_proposal)
    submit(capsys, target, SECOND_VIDEO, second_proposal)
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


def ok_json(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def tips_by_name(client: TestClient, **filters: Any) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in ok_json(client.get("/api/tips", params=filters))}


def approve(client: TestClient, names: Iterable[str]) -> None:
    tips = tips_by_name(client)
    ids = [tips[name]["id"] for name in names]
    ok_json(client.post("/api/review/approve", json={"tip_ids": ids}))


def approve_all_except(client: TestClient, *kept_pending: str) -> None:
    approve(client, [name for name in tips_by_name(client) if name not in kept_pending])


def printed_names(sheet: dict[str, Any]) -> list[str]:
    return [item["name"] for item in sheet["items"]]


def every_print_payload(client: TestClient) -> list[dict[str, Any]]:
    """The sheet of every list, then the activities sheet's groups."""

    lists = ok_json(client.get("/api/lists"))
    sheets = [ok_json(client.get(f"/api/print/lists/{item['id']}")) for item in lists]
    activities = ok_json(client.get("/api/print/activities"))
    return [*sheets, *activities["groups"]]


def assert_error(response: Any, status: int, code: str, fields: list[str]) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["fields"] == fields
    assert error["message"]


# --- GET /print/lists/{id} and /print/activities -----------------------------------------------


def test_nothing_prints_before_approval(client: TestClient) -> None:
    items = ok_json(client.get(f"/api/lists/{COFFEE_LIST}/items"))
    assert [item["status"] for item in items] == [limits.TIP_STATUS_PENDING] * 2

    for payload in every_print_payload(client):
        assert payload["items"] == []
    activities = ok_json(client.get("/api/print/activities"))
    assert [group["audience"] for group in activities["groups"]] == ["kids+", "adults"]


def test_a_location_sheet_prints_approved_daily_tips_in_list_order(client: TestClient) -> None:
    approve_all_except(client, PENDING_COFFEE, PENDING_UNPLACED, PENDING_ACTIVITY)
    fire = tips_by_name(client)["Fire Your Morning Brain"]
    ok_json(client.patch(f"/api/tips/{fire['id']}", json={"list_ids": [BATHROOM_LIST, 7]}))

    items = ok_json(client.get(f"/api/lists/{COFFEE_LIST}/items"))
    sheet = ok_json(client.get(f"/api/print/lists/{COFFEE_LIST}"))

    raisin = tips_by_name(client)["Your Brain Is A Raisin"]
    coffee = tips_by_name(client)[PENDING_COFFEE]
    assert items == [
        {
            "tip_id": raisin["id"],
            "position": 1,
            "name": raisin["name"],
            "action": raisin["action"],
            "status": limits.TIP_STATUS_APPROVED,
            "bucket": limits.BUCKET_DAILY,
            "audience": limits.AUDIENCE_KIDS_PLUS,
        },
        {
            "tip_id": coffee["id"],
            "position": 2,
            "name": PENDING_COFFEE,
            "action": coffee["action"],
            "status": limits.TIP_STATUS_PENDING,
            "bucket": limits.BUCKET_DAILY,
            "audience": limits.AUDIENCE_ADULTS,
        },
        {
            "tip_id": fire["id"],
            "position": 3,
            "name": fire["name"],
            "action": fire["action"],
            "status": limits.TIP_STATUS_APPROVED,
            "bucket": limits.BUCKET_DAILY,
            "audience": limits.AUDIENCE_ADULTS,
        },
    ]
    assert sheet == {
        "title": "Coffee Machine — Adults",
        "audience": limits.AUDIENCE_ADULTS,
        "generated_at": NOW_TEXT,
        "items": [
            {"name": raisin["name"], "action": raisin["action"]},
            {"name": fire["name"], "action": fire["action"]},
        ],
        "over_capacity": False,
    }


def test_pending_and_bucket_3_and_4_tips_never_appear_in_print_payloads(
    client: TestClient, data_dir: Path
) -> None:
    approve_all_except(client, PENDING_COFFEE, PENDING_UNPLACED, PENDING_ACTIVITY)
    sit_up = tips_by_name(client)["Sit Up Straight, You're Not Dead Yet"]
    ok_json(client.post("/api/review/left-out", json={"tip_ids": [sit_up["id"]]}))

    tips = tips_by_name(client)
    never = {
        name
        for name, item in tips.items()
        if item["status"] == limits.TIP_STATUS_PENDING
        or item["bucket"] in (limits.BUCKET_OPTIONAL, limits.BUCKET_LEFT_OUT)
    }
    # The guard is not vacuous: pending tips and approved bucket-3 and bucket-4 tips exist.
    assert {PENDING_COFFEE, PENDING_UNPLACED, PENDING_ACTIVITY} <= never
    approved_3_4 = {
        name
        for name, item in tips.items()
        if item["status"] == limits.TIP_STATUS_APPROVED
        and item["bucket"] in (limits.BUCKET_OPTIONAL, limits.BUCKET_LEFT_OUT)
    }
    assert {
        "Your Alarm Is Lying To You",
        "Tidy The Spice Rack",
        "Make Laziness Your Superpower",
        "Read The Kettle Manual",
        sit_up["name"],
    } <= approved_3_4

    payloads = every_print_payload(client)
    printed = [name for payload in payloads for name in printed_names(payload)]
    assert printed, "something must print for the check to mean anything"
    assert never.isdisjoint(printed)

    # Exactly what should print: approved daily tips on lists, approved bucket-2 activities.
    expected_daily = {
        row["name"]
        for row in query(
            data_dir,
            "SELECT DISTINCT tip.name FROM tip JOIN list_item ON list_item.tip_id = tip.id "
            "WHERE tip.status = 'approved' AND tip.bucket = 1",
        )
    }
    expected_activities = {
        row["name"]
        for row in query(data_dir, "SELECT name FROM tip WHERE status = 'approved' AND bucket = 2")
    }
    assert set(printed) == expected_daily | expected_activities
    assert expected_activities == {
        "The Accountability Text",
        "Unfollow Your Ghosts",
        "Garden Afternoon",
    }
    # Leaving the approved daily sit_up out took it off the list it was on, so no sheet holds it.
    assert sit_up["list_ids"] != []
    assert tips[sit_up["name"]]["list_ids"] == []
    for list_id in sit_up["list_ids"]:
        items = ok_json(client.get(f"/api/lists/{list_id}/items"))
        assert sit_up["id"] not in [item["tip_id"] for item in items]


def test_the_activities_sheet_groups_approved_bucket_2_tips_by_audience(
    client: TestClient,
) -> None:
    approve_all_except(client, PENDING_ACTIVITY)

    sheet = ok_json(client.get("/api/print/activities"))

    tips = tips_by_name(client)
    assert sheet == {
        "generated_at": NOW_TEXT,
        "groups": [
            {
                "audience": limits.AUDIENCE_KIDS_PLUS,
                "items": [
                    {"name": "Garden Afternoon", "action": tips["Garden Afternoon"]["action"]}
                ],
            },
            {
                "audience": limits.AUDIENCE_ADULTS,
                "items": [
                    {"name": name, "action": tips[name]["action"]}
                    for name in ("The Accountability Text", "Unfollow Your Ghosts")
                ],
            },
        ],
    }
    assert [group["audience"] for group in sheet["groups"]] == list(limits.AUDIENCES_TIP)


def test_over_capacity_turns_on_past_print_capacity(
    client: TestClient, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    submit(capsys, data_dir, CAPACITY_VIDEO, capacity_proposal)
    door = sorted(
        (item for item in tips_by_name(client).values() if item["source_ref"] == CAPACITY_REF),
        key=lambda item: item["id"],
    )
    assert len(door) == limits.PRINT_CAPACITY + 1

    def front_door() -> tuple[dict[str, Any], dict[str, Any]]:
        lists = {item["id"]: item for item in ok_json(client.get("/api/lists"))}
        sheet = ok_json(client.get(f"/api/print/lists/{FRONT_DOOR_ADULTS}"))
        return lists[FRONT_DOOR_ADULTS], sheet

    # The pending first-video tip on the list and the pending door tips never count.
    ok_json(client.post("/api/review/approve", json={"tip_ids": [t["id"] for t in door[:-1]]}))
    listed, sheet = front_door()
    assert len(sheet["items"]) == limits.PRINT_CAPACITY
    assert listed["item_count"] == limits.PRINT_CAPACITY + 2
    assert (listed["over_capacity"], sheet["over_capacity"]) == (False, False)

    ok_json(client.post("/api/review/approve", json={"tip_ids": [door[-1]["id"]]}))
    listed, sheet = front_door()
    assert len(sheet["items"]) == limits.PRINT_CAPACITY + 1
    assert (listed["over_capacity"], sheet["over_capacity"]) == (True, True)
    assert printed_names(sheet) == [item["name"] for item in door]


def test_unknown_lists_are_404_and_bad_ids_400(client: TestClient) -> None:
    for path in ("/api/print/lists/99", "/api/lists/99/items"):
        assert_error(client.get(path), 404, "CC_E_NOT_FOUND", [])
    for path in ("/api/print/lists/0", "/api/print/lists/x", "/api/lists/0/items"):
        assert_error(client.get(path), 400, "CC_E_VALIDATION", ["list_id"])


# --- GET /tips?unplaced=true -----------------------------------------------------------------


def unplaced_names(client: TestClient, **filters: Any) -> list[str]:
    return list(tips_by_name(client, unplaced="true", **filters))


def test_unplaced_returns_exactly_the_approved_daily_tips_on_no_list(
    client: TestClient, data_dir: Path
) -> None:
    assert unplaced_names(client) == []  # nothing is approved yet

    approve_all_except(client, PENDING_UNPLACED)
    assert unplaced_names(client) == ["The Two-Minute Brain Trick"]

    daylight = tips_by_name(client)["Step Into Morning Daylight"]  # kids+, on lists 5 and 6
    ok_json(client.patch(f"/api/tips/{daylight['id']}", json={"list_ids": []}))
    unplaced = tips_by_name(client, unplaced="true")
    assert list(unplaced) == ["Step Into Morning Daylight", "The Two-Minute Brain Trick"]
    for item in unplaced.values():
        assert (item["status"], item["bucket"], item["list_ids"]) == (
            limits.TIP_STATUS_APPROVED,
            limits.BUCKET_DAILY,
            [],
        )
    expected = query(
        data_dir,
        "SELECT name FROM tip WHERE status = 'approved' AND bucket = 1 "
        "AND id NOT IN (SELECT tip_id FROM list_item) ORDER BY id",
    )
    assert [row["name"] for row in expected] == list(unplaced)

    # The other filters still apply; false or no unplaced adds no filter.
    assert unplaced_names(client, audience=limits.AUDIENCE_KIDS_PLUS) == [daylight["name"]]
    assert unplaced_names(client, status=limits.TIP_STATUS_PENDING) == []
    assert len(tips_by_name(client, unplaced="false")) == len(tips_by_name(client))
    assert_error(
        client.get("/api/tips", params={"unplaced": "maybe"}), 400, "CC_E_VALIDATION", ["unplaced"]
    )


# --- printing.py guards the print rules itself ---------------------------------------------------


def test_the_location_sheet_filter_holds_even_past_the_schema_triggers(
    client: TestClient, data_dir: Path
) -> None:
    approve_all_except(client)
    alarm = tips_by_name(client)["Your Alarm Is Lying To You"]  # approved, bucket 3
    with closing(connection.connect(data_dir / paths.DB_FILENAME)) as conn:
        conn.execute("DROP TRIGGER list_item_insert_requires_bucket_1")
        with connection.transaction(conn):
            list_store.append_item(conn, COFFEE_LIST, alarm["id"])
        assert alarm["id"] in [item.tip_id for item in list_store.list_items(conn, COFFEE_LIST)]
        sheet = printing.location_sheet(conn, COFFEE_LIST, now=NOW_TEXT)
    assert sheet is not None
    assert alarm["name"] not in [item.name for item in sheet.items]
    assert len(sheet.items) == 2  # the approved daily tips still print
    assert printing.over_capacity(limits.PRINT_CAPACITY) is False
    assert printing.over_capacity(limits.PRINT_CAPACITY + 1) is True


# --- the demo's print data, through `serve --demo` ---------------------------------------------


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
    code, _, err = run_cli(capsys, "serve", "--demo", "--port", "8452")
    assert code == 0, err


def test_the_demo_has_the_print_lists_the_e2e_suite_needs(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def probe(test_client: TestClient) -> None:
        seen["lists"] = {item["id"]: item for item in ok_json(test_client.get("/api/lists"))}
        for list_id in (demo.COFFEE_LIST, demo.CAPACITY_FITS_LIST, demo.CAPACITY_SPILLS_LIST):
            seen[list_id] = ok_json(test_client.get(f"/api/print/lists/{list_id}"))
            seen[f"items-{list_id}"] = ok_json(test_client.get(f"/api/lists/{list_id}/items"))
        seen["activities"] = ok_json(test_client.get("/api/print/activities"))
        seen["unplaced"] = ok_json(test_client.get("/api/tips", params={"unplaced": "true"}))

    serve_demo(capsys, monkeypatch, probe)

    coffee = seen[demo.COFFEE_LIST]
    assert coffee["title"] == "Coffee Machine — Adults"
    assert len(coffee["items"]) == demo.COFFEE_LIST_TIPS == 6
    assert len(seen[f"items-{demo.COFFEE_LIST}"]) == 6  # nothing pending on it

    fits, spills = seen[demo.CAPACITY_FITS_LIST], seen[demo.CAPACITY_SPILLS_LIST]
    assert fits["title"] == "Capacity Fits — Adults"
    assert spills["title"] == "Capacity Spills — Adults"
    assert seen["lists"][8]["audience"] == seen["lists"][9]["audience"] == "adults"
    assert (len(fits["items"]), fits["over_capacity"]) == (limits.PRINT_CAPACITY, False)
    assert (len(spills["items"]), spills["over_capacity"]) == (limits.PRINT_CAPACITY + 1, True)
    assert spills["items"][: limits.PRINT_CAPACITY] == fits["items"]
    assert seen["lists"][8]["over_capacity"] is False
    assert seen["lists"][9]["over_capacity"] is True
    for item in spills["items"]:
        assert limits.line_length(item["name"], item["action"]) == limits.LINE_MAX, item
        # Realistic mixed-case words, not filler: no character repeats five times running.
        line = f"{item['name']} {item['action']}"
        assert any(char.islower() for char in line) and " " in item["action"]
        assert all(line[i : i + 5] != line[i] * 5 for i in range(len(line) - 4)), line
    assert len({item["name"] for item in spills["items"]}) == limits.PRINT_CAPACITY + 1

    groups = {group["audience"]: group["items"] for group in seen["activities"]["groups"]}
    assert list(groups) == list(limits.AUDIENCES_TIP)
    assert all(len(items) >= 2 for items in groups.values())

    assert seen["unplaced"], "the demo shows the Not on any list yet panel"
    for item in seen["unplaced"]:
        assert (item["status"], item["bucket"], item["list_ids"]) == ("approved", 1, [])


def test_the_capacity_tips_need_exactly_print_capacity_plus_one_lines() -> None:
    with pytest.raises(RuntimeError, match="PRINT_CAPACITY"):
        demo.capacity_tips(demo.CAPACITY_LINES[:-1])
