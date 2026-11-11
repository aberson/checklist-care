"""List management (plan §6, Step 11) through ``TestClient``: ``POST``/``PATCH``/``DELETE
/api/lists`` and ``PUT /api/lists/{id}/items``, read back through the print sheets
(``GET /api/print/lists/{id}``), the "Not on any list yet" panel
(``GET /api/tips?unplaced=true``) and the skill's ``checklist-care context``.

Every test runs on one data directory made by ``cli.main(["init"])`` (the seeded
buckets, the seven lists and the manual source), with ``CHECKLIST_CARE_DATA_DIR``
pointing at it, so ``cli.main(["context"])`` reads the very database the running
server (``create_app``) writes: an edited cue is what the skill reads on its next
ingest. Tips are added with the Library's ``POST /api/tips`` (approved, manual source).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from checklist_care import cli, limits, paths, printing
from checklist_care.db import connection, seed
from checklist_care.server import models
from checklist_care.server.app import create_app

NOW = datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC)
LOCAL_URL = "http://127.0.0.1:8450"
"""TestClient's default ``testserver`` Host is not one the app accepts."""

ADULT_BATHROOM = 1  # Adult Bathroom / adults
OFFICE = 2  # My Office / adults
KIDS_BATHROOM = 3  # Kids Bathroom / kids
KIDS_BATHROOM_ADULTS = 4  # Kids Bathroom / adults
COFFEE = 7  # Coffee Machine / adults

BEDROOM_CUE = "Getting ready for bed or getting up"

INVALID = "CC_E_VALIDATION"
NOT_FOUND = "CC_E_NOT_FOUND"
CONFLICT = "CC_E_CONFLICT"


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real data directory; control the clock."""

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(connection, "utc_now", lambda: NOW)


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """``cli.main(argv)`` on the data directory of ``CHECKLIST_CARE_DATA_DIR``; its stdout."""

    code = cli.main(list(argv))
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return captured.out


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """The data directory ``init`` made: the seeded buckets, lists and manual source."""

    run_cli(capsys, "init")
    target = tmp_path / "data"
    assert (target / paths.DB_FILENAME).is_file()
    return target


@pytest.fixture
def client(data_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    dist = tmp_path / "dist"
    dist.mkdir()
    app = create_app(data_dir / paths.DB_FILENAME, dist_dir=dist)
    with TestClient(app, base_url=LOCAL_URL) as test_client:
        yield test_client


def context_lists(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """The ``lists`` block that ``checklist-care context`` prints for the skill."""

    lists: list[dict[str, Any]] = json.loads(run_cli(capsys, "context"))["lists"]
    return lists


def context_tip_lists(capsys: pytest.CaptureFixture[str]) -> dict[str, list[int]]:
    """Each tip's ``list_ids`` as ``context`` prints them, by tip name."""

    tips = json.loads(run_cli(capsys, "context"))["tips"]
    return {tip["name"]: tip["list_ids"] for tip in tips}


def query(data_dir: Path, sql: str, *params: Any) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def dump(data_dir: Path) -> list[str]:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        return list(conn.iterdump())


def ok_json(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def assert_error(response: Any, status: int, code: str, fields: list[str]) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["fields"] == fields
    assert error["message"]


def add_tip(
    client: TestClient,
    name: str,
    action: str,
    *,
    audience: str = limits.AUDIENCE_ADULTS,
    bucket: int = limits.BUCKET_DAILY,
    list_ids: list[int] | None = None,
) -> int:
    """A tip of your own (``POST /api/tips``): approved, on ``list_ids``; returns its id."""

    body = {
        "name": name,
        "action": action,
        "bucket": bucket,
        "audience": audience,
        "list_ids": list_ids or [],
    }
    tip_id: int = ok_json(client.post("/api/tips", json=body), 201)["id"]
    return tip_id


def create(client: TestClient, **body: Any) -> dict[str, Any]:
    created: dict[str, Any] = ok_json(client.post("/api/lists", json=body), 201)
    return created


def put_items(client: TestClient, list_id: int, tip_ids: list[int]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = ok_json(
        client.put(f"/api/lists/{list_id}/items", json={"tip_ids": tip_ids})
    )
    return items


def sheet_names(client: TestClient, list_id: int) -> list[str]:
    return [item["name"] for item in ok_json(client.get(f"/api/print/lists/{list_id}"))["items"]]


def item_ids(client: TestClient, list_id: int) -> list[int]:
    return [item["tip_id"] for item in ok_json(client.get(f"/api/lists/{list_id}/items"))]


def unplaced_names(client: TestClient) -> list[str]:
    return [tip["name"] for tip in ok_json(client.get("/api/tips", params={"unplaced": "true"}))]


def lists_by_id(client: TestClient) -> dict[int, dict[str, Any]]:
    return {item["id"]: item for item in ok_json(client.get("/api/lists"))}


# --- Done when 1: a new list, with a cue, prints its tips in the order placed ----------------


def test_bedroom_adults_with_a_cue_prints_its_tips_in_the_order_placed(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clothes = add_tip(client, "Lay Out Tomorrow's Clothes", "Put tomorrow's clothes on the chair")
    alarm = add_tip(client, "Set The Alarm", "Set the alarm before the light goes out")
    water = add_tip(
        client,
        "Glass Of Water By The Bed",
        "Fill a glass of water for the night",
        audience=limits.AUDIENCE_KIDS_PLUS,
    )

    bedroom = create(client, place="Bedroom", audience=limits.AUDIENCE_ADULTS, cue=BEDROOM_CUE)

    seeded = len(seed.LISTS)
    assert bedroom == {
        "id": seeded + 1,
        "place": "Bedroom",
        "audience": limits.AUDIENCE_ADULTS,
        "title": "Bedroom — Adults",
        "cue": BEDROOM_CUE,
        "sort_order": seeded + 1,
        "item_count": 0,
        "over_capacity": False,
    }
    assert sheet_names(client, bedroom["id"]) == []

    items = put_items(client, bedroom["id"], [alarm, water, clothes])

    assert [(item["tip_id"], item["position"]) for item in items] == [
        (alarm, 1),
        (water, 2),
        (clothes, 3),
    ]
    sheet = ok_json(client.get(f"/api/print/lists/{bedroom['id']}"))
    assert sheet["title"] == "Bedroom — Adults"
    assert [item["name"] for item in sheet["items"]] == [
        "Set The Alarm",
        "Glass Of Water By The Bed",
        "Lay Out Tomorrow's Clothes",
    ]
    assert sheet["over_capacity"] is False
    # Last in the Lists page order, holding the three tips.
    listed = ok_json(client.get("/api/lists"))
    assert listed[-1] == {**bedroom, "item_count": 3}
    # The skill sees the new place, its cue and the placements on its next ingest.
    assert context_lists(capsys)[-1] == {
        "id": bedroom["id"],
        "place": "Bedroom",
        "audience": limits.AUDIENCE_ADULTS,
        "cue": BEDROOM_CUE,
    }
    placed = context_tip_lists(capsys)
    assert placed["Set The Alarm"] == [bedroom["id"]]
    assert placed["Lay Out Tomorrow's Clothes"] == [bedroom["id"]]

    # Reordering is one PUT of the new order; the sheet follows it.
    put_items(client, bedroom["id"], [clothes, alarm, water])
    assert sheet_names(client, bedroom["id"]) == [
        "Lay Out Tomorrow's Clothes",
        "Set The Alarm",
        "Glass Of Water By The Bed",
    ]
    assert query(
        data_dir,
        "SELECT tip_id, position FROM list_item WHERE list_id = ? ORDER BY position",
        bedroom["id"],
    ) == [
        {"tip_id": clothes, "position": 1},
        {"tip_id": alarm, "position": 2},
        {"tip_id": water, "position": 3},
    ]


def test_a_list_created_without_a_cue_has_an_empty_one(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    hall = create(client, place="Hallway", audience=limits.AUDIENCE_KIDS)

    assert (hall["cue"], hall["title"]) == ("", "Hallway — Kids")
    assert context_lists(capsys)[-1]["cue"] == ""


TOO_LONG_CUE = "c" * (limits.CUE_MAX + 1)


def test_a_list_that_breaks_a_rule_is_not_created(client: TestClient, data_dir: Path) -> None:
    before = dump(data_dir)
    for body, fields in (
        # A place has one list per audience: Coffee Machine / adults is seeded.
        ({"place": "Coffee Machine", "audience": limits.AUDIENCE_ADULTS}, ["place", "audience"]),
        ({"place": "", "audience": limits.AUDIENCE_ADULTS}, ["place"]),
        ({"place": "  ", "audience": limits.AUDIENCE_ADULTS}, ["place"]),
        ({"place": "p" * (limits.PLACE_MAX + 1), "audience": limits.AUDIENCE_ADULTS}, ["place"]),
        ({"place": "Two\nLines", "audience": limits.AUDIENCE_ADULTS}, ["place"]),
        ({"place": "Garage", "audience": limits.AUDIENCE_KIDS_PLUS}, ["audience"]),
        ({"place": "Garage", "audience": "teens"}, ["audience"]),
        ({"place": "Garage"}, ["audience"]),
        ({"audience": limits.AUDIENCE_ADULTS}, ["place"]),
        ({"place": "Garage", "audience": limits.AUDIENCE_ADULTS, "cue": "   "}, ["cue"]),
        ({"place": "Garage", "audience": limits.AUDIENCE_ADULTS, "cue": TOO_LONG_CUE}, ["cue"]),
        ({"place": "Garage", "audience": limits.AUDIENCE_ADULTS, "cue": "a\tb"}, ["cue"]),
        ({"place": "Garage", "audience": limits.AUDIENCE_ADULTS, "cue": None}, ["cue"]),
        ({"place": 7, "audience": limits.AUDIENCE_ADULTS}, ["place"]),
        ({"place": "Garage", "audience": limits.AUDIENCE_ADULTS, "id": 99}, ["id"]),
        ({"place": "Garage", "audience": limits.AUDIENCE_ADULTS, "sort_order": 1}, ["sort_order"]),
    ):
        assert_error(client.post("/api/lists", json=body), 400, INVALID, fields)
    assert dump(data_dir) == before


def test_the_longest_place_and_cue_are_stored(client: TestClient) -> None:
    place, cue = "p" * limits.PLACE_MAX, "c" * limits.CUE_MAX

    created = create(client, place=place, audience=limits.AUDIENCE_ADULTS, cue=cue)

    assert (created["place"], created["cue"]) == (place, cue)
    # The same place for the other audience is a list of its own.
    other = create(client, place=place, audience=limits.AUDIENCE_KIDS)
    assert other["id"] == created["id"] + 1


# --- Done when 2: a PATCHed cue is what the skill reads --------------------------------------


def test_a_patched_cue_appears_in_context(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded_cue = seed.LISTS[COFFEE - 1].cue
    assert context_lists(capsys)[COFFEE - 1]["cue"] == seeded_cue
    cue = "Waiting for the kettle or the coffee machine — any hot drink"

    result = ok_json(client.patch(f"/api/lists/{COFFEE}", json={"cue": cue}))

    assert result["removed_tip_ids"] == []
    assert result["list"]["cue"] == cue
    assert result["list"]["title"] == "Coffee Machine — Adults"
    # `checklist-care context`, exactly as the skill runs it, on the same data directory.
    out = run_cli(capsys, "context")
    coffee = json.loads(out)["lists"][COFFEE - 1]
    assert coffee == {
        "id": COFFEE,
        "place": "Coffee Machine",
        "audience": limits.AUDIENCE_ADULTS,
        "cue": cue,
    }
    assert json.dumps(cue, ensure_ascii=False) in out
    assert query(data_dir, "SELECT cue FROM list WHERE id = ?", COFFEE) == [{"cue": cue}]
    # The other lists keep their seeded cues; an empty cue clears it.
    assert [item["cue"] for item in context_lists(capsys)[: COFFEE - 1]] == [
        item.cue for item in seed.LISTS[: COFFEE - 1]
    ]
    ok_json(client.patch(f"/api/lists/{COFFEE}", json={"cue": ""}))
    assert context_lists(capsys)[COFFEE - 1]["cue"] == ""


def test_renaming_and_reordering_reach_the_lists_the_sheet_and_context(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    result = ok_json(
        client.patch(f"/api/lists/{OFFICE}", json={"place": "Study Desk", "sort_order": 0})
    )

    assert result["list"]["title"] == "Study Desk — Adults"
    assert result["list"]["sort_order"] == 0
    listed = ok_json(client.get("/api/lists"))
    assert [item["id"] for item in listed][:2] == [OFFICE, ADULT_BATHROOM]
    assert ok_json(client.get(f"/api/print/lists/{OFFICE}"))["title"] == "Study Desk — Adults"
    assert context_lists(capsys)[0]["place"] == "Study Desk"
    # Equal sort orders keep id order.
    ok_json(client.patch(f"/api/lists/{COFFEE}", json={"sort_order": 0}))
    assert [item["id"] for item in ok_json(client.get("/api/lists"))][:3] == [
        OFFICE,
        COFFEE,
        ADULT_BATHROOM,
    ]


# --- Done when 3: becoming a kids list removes its adults tips, reported ---------------------


def test_changing_a_lists_audience_to_kids_removes_its_adults_tips_and_reports_them(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bedroom = create(client, place="Bedroom", audience=limits.AUDIENCE_ADULTS, cue=BEDROOM_CUE)
    phone = add_tip(client, "Phone Charges Outside", "Leave the phone charging in the hall")
    teeth = add_tip(
        client, "Brush Your Teeth", "Brush for two minutes", audience=limits.AUDIENCE_KIDS_PLUS
    )
    news = add_tip(client, "No News After Nine", "Close the news apps after nine")
    pyjamas = add_tip(
        client,
        "Pyjamas On The Pillow",
        "Fold pyjamas onto the pillow",
        audience=limits.AUDIENCE_KIDS_PLUS,
    )
    put_items(client, bedroom["id"], [phone, teeth, news, pyjamas])
    # The news tip is also on the office list, which keeps it.
    put_items(client, OFFICE, [news])

    result = ok_json(
        client.patch(f"/api/lists/{bedroom['id']}", json={"audience": limits.AUDIENCE_KIDS})
    )

    assert result["removed_tip_ids"] == [phone, news]
    assert result["list"] == {
        **bedroom,
        "audience": limits.AUDIENCE_KIDS,
        "title": "Bedroom — Kids",
        "item_count": 2,
    }
    items = ok_json(client.get(f"/api/lists/{bedroom['id']}/items"))
    assert [(item["tip_id"], item["position"]) for item in items] == [(teeth, 1), (pyjamas, 2)]
    assert sheet_names(client, bedroom["id"]) == ["Brush Your Teeth", "Pyjamas On The Pillow"]
    assert item_ids(client, OFFICE) == [news]
    # The phone tip is on no list now: it shows under "Not on any list yet".
    assert unplaced_names(client) == ["Phone Charges Outside"]
    placed = context_tip_lists(capsys)
    assert placed["Phone Charges Outside"] == []
    assert placed["No News After Nine"] == [OFFICE]
    assert context_lists(capsys)[-1]["audience"] == limits.AUDIENCE_KIDS
    assert query(data_dir, "SELECT count(*) AS n FROM tip") == [{"n": 4}]

    # Back to adults takes nothing off.
    back = ok_json(
        client.patch(f"/api/lists/{bedroom['id']}", json={"audience": limits.AUDIENCE_ADULTS})
    )
    assert back["removed_tip_ids"] == []
    assert item_ids(client, bedroom["id"]) == [teeth, pyjamas]


def test_an_audience_change_onto_an_existing_list_changes_nothing(
    client: TestClient, data_dir: Path
) -> None:
    adults = add_tip(client, "Check The Plug", "Switch the heater off at the wall")
    put_items(client, KIDS_BATHROOM_ADULTS, [adults])
    before = dump(data_dir)

    # Kids Bathroom / kids already exists (list 3), so list 4 cannot become it.
    response = client.patch(
        f"/api/lists/{KIDS_BATHROOM_ADULTS}", json={"audience": limits.AUDIENCE_KIDS}
    )

    assert_error(response, 400, INVALID, ["audience"])
    assert "Kids Bathroom — Kids (#3)" in response.json()["error"]["message"]
    assert dump(data_dir) == before
    assert_error(
        client.patch(f"/api/lists/{OFFICE}", json={"place": "Front Door"}), 400, INVALID, ["place"]
    )
    assert dump(data_dir) == before


def test_a_patch_that_breaks_a_rule_changes_nothing(client: TestClient, data_dir: Path) -> None:
    before = dump(data_dir)
    coffee = f"/api/lists/{COFFEE}"
    for body, fields in (
        ({}, list(models.LIST_FIELDS)),
        ({"place": None}, ["place"]),
        ({"audience": None}, ["audience"]),
        ({"cue": None}, ["cue"]),
        ({"sort_order": None}, ["sort_order"]),
        ({"place": ""}, ["place"]),
        ({"place": "x" * (limits.PLACE_MAX + 1)}, ["place"]),
        ({"audience": limits.AUDIENCE_KIDS_PLUS}, ["audience"]),
        ({"cue": "c" * (limits.CUE_MAX + 1)}, ["cue"]),
        ({"cue": "line\nbreak"}, ["cue"]),
        ({"sort_order": -1}, ["sort_order"]),
        ({"sort_order": "2"}, ["sort_order"]),
        ({"sort_order": limits.SQLITE_INTEGER_MAX + 1}, ["sort_order"]),
        ({"id": 3}, ["id"]),
        ({"title": "Coffee"}, ["title"]),
    ):
        assert_error(client.patch(coffee, json=body), 400, INVALID, fields)
    assert dump(data_dir) == before


# --- Done when 4: PUT /items with a tip the list refuses is a 409 and changes nothing --------


def test_put_items_with_an_adults_tip_on_a_kids_list_is_409_and_changes_nothing(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    teeth = add_tip(
        client,
        "Brush Your Teeth",
        "Brush for two minutes",
        audience=limits.AUDIENCE_KIDS_PLUS,
        list_ids=[KIDS_BATHROOM],
    )
    wash = add_tip(
        client, "Wash Your Hands", "Soap, rinse and dry", audience=limits.AUDIENCE_KIDS_PLUS
    )
    shave = add_tip(client, "Rinse The Razor", "Rinse and dry the razor after shaving")
    before = dump(data_dir)
    context_before = run_cli(capsys, "context")

    response = client.put(
        f"/api/lists/{KIDS_BATHROOM}/items", json={"tip_ids": [wash, shave, teeth]}
    )

    assert_error(response, 409, CONFLICT, ["tip_ids"])
    message = response.json()["error"]["message"]
    assert f'tip #{shave} "Rinse The Razor" is adults' in message
    assert "Kids Bathroom — Kids is a kids list" in message
    assert "nothing was changed" in message
    assert dump(data_dir) == before
    assert item_ids(client, KIDS_BATHROOM) == [teeth]
    assert sheet_names(client, KIDS_BATHROOM) == ["Brush Your Teeth"]
    assert run_cli(capsys, "context") == context_before
    # Without the adults tip the same request goes through.
    assert [item["tip_id"] for item in put_items(client, KIDS_BATHROOM, [wash, teeth])] == [
        wash,
        teeth,
    ]


def test_put_items_refuses_a_tip_outside_bucket_1_unknown_or_repeated_tips(
    client: TestClient, data_dir: Path
) -> None:
    daily = add_tip(client, "Glass Of Water First", "Drink a glass of water", list_ids=[COFFEE])
    weekly = add_tip(
        client, "Descale The Machine", "Run the descaler through", bucket=limits.BUCKET_OCCASIONAL
    )
    before = dump(data_dir)
    items = f"/api/lists/{COFFEE}/items"

    occasional = client.put(items, json={"tip_ids": [daily, weekly]})
    assert_error(occasional, 409, CONFLICT, ["tip_ids"])
    assert f"tip #{weekly}" in occasional.json()["error"]["message"]
    assert "bucket 2" in occasional.json()["error"]["message"]
    missing = client.put(items, json={"tip_ids": [daily, 9999]})
    assert_error(missing, 404, NOT_FOUND, ["tip_ids"])
    assert "#9999" in missing.json()["error"]["message"]
    assert_error(client.put(items, json={"tip_ids": [daily, daily]}), 400, INVALID, ["tip_ids"])
    for body, fields in (
        ({"tip_ids": [0]}, ["tip_ids"]),
        ({"tip_ids": ["1"]}, ["tip_ids"]),
        ({"tip_ids": None}, ["tip_ids"]),
        ({}, ["tip_ids"]),
        ({"tip_ids": [daily], "list_id": COFFEE}, ["list_id"]),
    ):
        assert_error(client.put(items, json=body), 400, INVALID, fields)
    assert_error(client.put("/api/lists/99/items", json={"tip_ids": [daily]}), 404, NOT_FOUND, [])
    assert_error(client.put("/api/lists/0/items", json={"tip_ids": []}), 400, INVALID, ["list_id"])
    assert dump(data_dir) == before


def test_put_items_holds_pending_tips_that_print_once_approved(client: TestClient) -> None:
    approved = add_tip(client, "Glass Of Water First", "Drink a glass of water")
    pending = add_tip(client, "Calf Raises While It Pours", "Do calf raises while it pours")
    ok_json(client.patch(f"/api/tips/{pending}", json={"status": limits.TIP_STATUS_PENDING}))

    items = put_items(client, COFFEE, [pending, approved])

    assert [(item["name"], item["status"]) for item in items] == [
        ("Calf Raises While It Pours", limits.TIP_STATUS_PENDING),
        ("Glass Of Water First", limits.TIP_STATUS_APPROVED),
    ]
    assert sheet_names(client, COFFEE) == ["Glass Of Water First"]
    assert lists_by_id(client)[COFFEE]["item_count"] == 2
    ok_json(client.post("/api/review/approve", json={"tip_ids": [pending]}))
    assert sheet_names(client, COFFEE) == ["Calf Raises While It Pours", "Glass Of Water First"]


def test_an_empty_put_clears_the_list(client: TestClient) -> None:
    tip = add_tip(client, "Glass Of Water First", "Drink a glass of water", list_ids=[COFFEE])

    assert put_items(client, COFFEE, []) == []

    assert item_ids(client, COFFEE) == []
    assert sheet_names(client, COFFEE) == []
    # The tip itself stays stored, now on no list.
    assert [(item["id"], item["list_ids"]) for item in ok_json(client.get("/api/tips"))] == [
        (tip, [])
    ]
    assert unplaced_names(client) == ["Glass Of Water First"]


# --- Done when 5: approved, unplaced daily tips appear under unplaced=true -------------------


def test_an_approved_unplaced_bucket_1_tip_appears_under_unplaced(
    client: TestClient, data_dir: Path
) -> None:
    water = add_tip(client, "Glass Of Water First", "Drink a glass of water", list_ids=[COFFEE])
    keys = add_tip(
        client,
        "Keys On The Hook",
        "Hang your keys on the hook",
        audience=limits.AUDIENCE_KIDS_PLUS,
        list_ids=[KIDS_BATHROOM, ADULT_BATHROOM],
    )
    pending = add_tip(client, "Stretch While It Brews", "Stretch while the coffee brews")
    ok_json(client.patch(f"/api/tips/{pending}", json={"status": limits.TIP_STATUS_PENDING}))
    add_tip(client, "Plan A Walk", "Pick an evening for a walk", bucket=limits.BUCKET_OCCASIONAL)
    assert unplaced_names(client) == []  # placed, pending or not daily

    # Taking the water tip off its only list leaves it on none.
    put_items(client, COFFEE, [])
    unplaced = ok_json(client.get("/api/tips", params={"unplaced": "true"}))
    assert [(tip["id"], tip["status"], tip["bucket"], tip["list_ids"]) for tip in unplaced] == [
        (water, limits.TIP_STATUS_APPROVED, limits.BUCKET_DAILY, []),
    ]

    # Deleting a list keeps its tips; the keys stay on their other list until that goes too.
    assert client.delete(f"/api/lists/{KIDS_BATHROOM}").status_code == 204
    assert unplaced_names(client) == ["Glass Of Water First"]
    assert client.delete(f"/api/lists/{ADULT_BATHROOM}").status_code == 204
    assert unplaced_names(client) == ["Glass Of Water First", "Keys On The Hook"]
    assert query(data_dir, "SELECT count(*) AS n FROM list_item WHERE tip_id = ?", keys) == [
        {"n": 0}
    ]
    # The pending tip on no list waits for review, not for a list.
    assert pending not in [tip["id"] for tip in unplaced]

    # Placing it again takes it off the panel.
    put_items(client, OFFICE, [water])
    assert unplaced_names(client) == ["Keys On The Hook"]


# --- DELETE /lists/{id} -------------------------------------------------------------------------


def test_deleting_a_list_keeps_its_tips_and_leaves_context(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    water = add_tip(
        client, "Glass Of Water First", "Drink a glass of water", list_ids=[COFFEE, OFFICE]
    )

    assert client.delete(f"/api/lists/{COFFEE}").status_code == 204

    assert COFFEE not in lists_by_id(client)
    assert COFFEE not in [item["id"] for item in context_lists(capsys)]
    assert context_tip_lists(capsys)["Glass Of Water First"] == [OFFICE]
    assert query(data_dir, "SELECT id FROM tip") == [{"id": water}]
    assert_error(client.get(f"/api/print/lists/{COFFEE}"), 404, NOT_FOUND, [])
    assert_error(client.get(f"/api/lists/{COFFEE}/items"), 404, NOT_FOUND, [])
    assert_error(client.delete(f"/api/lists/{COFFEE}"), 404, NOT_FOUND, [])
    assert_error(client.patch(f"/api/lists/{COFFEE}", json={"cue": "x"}), 404, NOT_FOUND, [])
    # The freed place and audience can be used again, at the end of the order.
    again = create(client, place="Coffee Machine", audience=limits.AUDIENCE_ADULTS)
    assert ok_json(client.get("/api/lists"))[-1] == again
    assert (again["item_count"], sheet_names(client, again["id"])) == (0, [])


# --- capacity: the warning the Lists page shows -----------------------------------------------


def test_over_capacity_follows_the_approved_tips_placed(client: TestClient) -> None:
    capacity = limits.PRINT_CAPACITY
    tips = [add_tip(client, f"Daily Tip {n}", f"Do daily thing {n}") for n in range(capacity + 1)]
    assert printing.over_capacity(capacity) is False
    assert printing.over_capacity(capacity + 1) is True

    put_items(client, OFFICE, tips[:capacity])
    assert lists_by_id(client)[OFFICE]["over_capacity"] is False
    assert ok_json(client.get(f"/api/print/lists/{OFFICE}"))["over_capacity"] is False

    put_items(client, OFFICE, tips)
    assert lists_by_id(client)[OFFICE]["over_capacity"] is True
    assert lists_by_id(client)[OFFICE]["item_count"] == capacity + 1
    assert ok_json(client.get(f"/api/print/lists/{OFFICE}"))["over_capacity"] is True

    # A pending tip does not print, so it does not count.
    ok_json(client.patch(f"/api/tips/{tips[0]}", json={"status": limits.TIP_STATUS_PENDING}))
    assert lists_by_id(client)[OFFICE]["over_capacity"] is False


# --- the rules come from limits.py ---------------------------------------------------------------


def test_every_seeded_list_passes_the_create_rules() -> None:
    for item in seed.LISTS:
        models.ListCreate.model_validate(
            {"place": item.place, "audience": item.audience, "cue": item.cue}
        )


def test_meta_carries_the_list_limits(client: TestClient) -> None:
    meta = ok_json(client.get("/api/meta"))

    assert meta["limits"]["PLACE_MAX"] == limits.PLACE_MAX
    assert meta["limits"]["CUE_MAX"] == limits.CUE_MAX
    assert meta["limits"]["AUDIENCES_LIST"] == list(limits.AUDIENCES_LIST)
