"""The bucket rubrics (plan §6, Step 10): ``GET``/``PUT /api/buckets`` through
``TestClient``, and the API → skill-context seam.

Every test runs on one data directory made by ``cli.main(["init"])``, with
``CHECKLIST_CARE_DATA_DIR`` pointing at it, so ``cli.main(["context"])`` (what the
skill runs before it proposes) reads the very database the running server
(``create_app``) writes: an edited rubric is what the skill reads on its next ingest.
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

from checklist_care import cli, limits, paths
from checklist_care.db import connection, seed
from checklist_care.server import models
from checklist_care.server.app import create_app

NOW = datetime(2026, 11, 11, 9, 30, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-11T09:30:00Z"
SEEDED_AT = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
SEEDED_AT_TEXT = "2026-11-01T08:00:00Z"
LOCAL_URL = "http://127.0.0.1:8450"
"""TestClient's default ``testserver`` Host is not one the app accepts."""

NEW_RUBRIC = (
    "Worth doing every day, or each time its cue comes round — two minutes or less.\n"
    "\tOne clear physical action; no equipment beyond what is already at hand (café included)."
)
"""A rubric with a line break, a tab and non-ASCII characters, all of which it may hold."""

INVALID = "CC_E_VALIDATION"


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real data directory; control the clock."""

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(connection, "utc_now", lambda: SEEDED_AT)


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
def client(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The server on ``data_dir``'s database; edits from here on happen at :data:`NOW`."""

    monkeypatch.setattr(connection, "utc_now", lambda: NOW)
    dist = tmp_path / "dist"
    dist.mkdir()
    app = create_app(data_dir / paths.DB_FILENAME, dist_dir=dist)
    with TestClient(app, base_url=LOCAL_URL) as test_client:
        yield test_client


def context_buckets(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """The ``buckets`` block that ``checklist-care context`` prints for the skill."""

    payload = json.loads(run_cli(capsys, "context"))
    buckets: list[dict[str, Any]] = payload["buckets"]
    return buckets


def seeded_buckets() -> list[dict[str, Any]]:
    return [
        {"id": bucket.id, "title": bucket.title, "rubric": bucket.rubric} for bucket in seed.BUCKETS
    ]


def stored_buckets(data_dir: Path) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, title, rubric, updated_at FROM bucket ORDER BY id")
        return [dict(row) for row in rows]


def assert_error(response: Any, status: int, code: str, fields: list[str]) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["fields"] == fields
    assert error["message"]


# --- the API -> skill-context seam ------------------------------------------------------------


def test_a_rubric_put_through_the_api_is_exactly_what_context_prints(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert context_buckets(capsys) == seeded_buckets()

    response = client.put(f"/api/buckets/{limits.BUCKET_DAILY}", json={"rubric": NEW_RUBRIC})

    assert response.status_code == 200, response.text
    daily = seed.BUCKETS[0]
    assert response.json() == {
        "id": limits.BUCKET_DAILY,
        "title": daily.title,
        "rubric": NEW_RUBRIC,
        "updated_at": NOW_TEXT,
    }
    # The skill's next `context` call, on the same data directory while the server runs.
    out = run_cli(capsys, "context")
    buckets = json.loads(out)["buckets"]
    assert buckets[0] == {"id": limits.BUCKET_DAILY, "title": daily.title, "rubric": NEW_RUBRIC}
    assert buckets[0]["rubric"] == NEW_RUBRIC
    assert json.dumps(NEW_RUBRIC, ensure_ascii=False) in out
    assert buckets[1:] == seeded_buckets()[1:]


def test_a_title_and_rubric_put_together_reach_context_meta_and_get(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = {"title": "Every day", "rubric": "Due daily; two minutes."}

    response = client.put(f"/api/buckets/{limits.BUCKET_OCCASIONAL}", json=body)

    assert response.status_code == 200, response.text
    expected = {"id": limits.BUCKET_OCCASIONAL, **body}
    assert context_buckets(capsys)[1] == expected
    listed = client.get("/api/buckets").json()
    assert listed[1] == {**expected, "updated_at": NOW_TEXT}
    titles = [bucket["title"] for bucket in client.get("/api/meta").json()["buckets"]]
    assert titles[1] == "Every day"
    # A later rubric-only edit keeps the new title.
    client.put(f"/api/buckets/{limits.BUCKET_OCCASIONAL}", json={"rubric": "Weekly or monthly."})
    assert stored_buckets(data_dir)[1]["title"] == "Every day"
    assert context_buckets(capsys)[1]["rubric"] == "Weekly or monthly."


# --- GET /buckets ------------------------------------------------------------------------------


def test_get_lists_the_four_seeded_buckets_by_id(client: TestClient) -> None:
    response = client.get("/api/buckets")

    assert response.status_code == 200
    assert response.json() == [
        {**bucket, "updated_at": SEEDED_AT_TEXT} for bucket in seeded_buckets()
    ]
    assert [bucket["id"] for bucket in response.json()] == list(limits.BUCKET_IDS)


# --- PUT /buckets/{id}: what is refused ---------------------------------------------------------


def test_an_empty_rubric_returns_400_and_stores_nothing(
    client: TestClient, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = stored_buckets(data_dir)

    response = client.put(f"/api/buckets/{limits.BUCKET_DAILY}", json={"rubric": ""})

    assert_error(response, 400, INVALID, ["rubric"])
    assert "must not be empty" in response.json()["error"]["message"]
    assert stored_buckets(data_dir) == before
    assert context_buckets(capsys) == seeded_buckets()


def test_a_put_that_breaks_a_rule_stores_nothing(client: TestClient, data_dir: Path) -> None:
    before = stored_buckets(data_dir)
    daily = f"/api/buckets/{limits.BUCKET_DAILY}"
    for body, fields in (
        ({"rubric": "   \n\t "}, ["rubric"]),
        ({"rubric": None}, ["rubric"]),
        ({"rubric": "r" * (limits.RUBRIC_MAX + 1)}, ["rubric"]),
        ({"rubric": "line one\r\nline two"}, ["rubric"]),
        ({"rubric": "a\x00b"}, ["rubric"]),
        ({"rubric": "a b"}, ["rubric"]),
        ({"rubric": 7}, ["rubric"]),
        ({"title": ""}, ["title"]),
        ({"title": None}, ["title"]),
        ({"title": "t" * (limits.BUCKET_TITLE_MAX + 1)}, ["title"]),
        ({"title": "two\nlines"}, ["title"]),
        ({"title": "Fine", "rubric": ""}, ["rubric"]),
        ({}, ["title", "rubric"]),
        ({"rubric": "Fine", "updated_at": "2020-01-01T00:00:00Z"}, ["updated_at"]),
        ({"rubric": "Fine", "id": 2}, ["id"]),
    ):
        assert_error(client.put(daily, json=body), 400, INVALID, fields)
    # A "\ud800" JSON escape decodes to a lone surrogate, which cannot be stored.
    lone = client.put(
        daily,
        content=b'{"rubric": "a\\ud800b"}',
        headers={"Content-Type": "application/json"},
    )
    assert_error(lone, 400, INVALID, ["rubric"])
    assert stored_buckets(data_dir) == before


def test_the_longest_rubric_and_title_are_stored(client: TestClient) -> None:
    rubric = "r" * limits.RUBRIC_MAX
    title = "t" * limits.BUCKET_TITLE_MAX

    response = client.put(
        f"/api/buckets/{limits.BUCKET_LEFT_OUT}", json={"title": title, "rubric": rubric}
    )

    assert response.status_code == 200, response.text
    assert (response.json()["title"], response.json()["rubric"]) == (title, rubric)


def test_an_unknown_bucket_is_404_and_a_bad_id_400(client: TestClient, data_dir: Path) -> None:
    before = stored_buckets(data_dir)

    body = {"rubric": "Anything"}
    assert_error(client.put("/api/buckets/5", json=body), 404, "CC_E_NOT_FOUND", [])
    assert_error(client.put("/api/buckets/0", json=body), 400, INVALID, ["bucket_id"])
    assert_error(client.put("/api/buckets/x", json=body), 400, INVALID, ["bucket_id"])
    assert stored_buckets(data_dir) == before


# --- the rules come from limits.py ---------------------------------------------------------------


def test_the_rubric_rule_reads_rubric_max_from_limits() -> None:
    assert models.rubric_problem("r" * limits.RUBRIC_MAX) is None
    assert models.rubric_problem("r" * (limits.RUBRIC_MAX + 1)) == (
        f"is {limits.RUBRIC_MAX + 1} characters; RUBRIC_MAX is {limits.RUBRIC_MAX}; shorten it"
    )
    assert models.rubric_problem(NEW_RUBRIC) is None
    # RUBRIC_MAX is above CPython's small-int cache, so `is` fails for a restated value.
    assert limits.RUBRIC_MAX > 256
    assert models.RUBRIC_MAX is limits.RUBRIC_MAX


def test_every_seeded_bucket_text_passes_the_put_rules() -> None:
    for bucket in seed.BUCKETS:
        models.BucketPut.model_validate({"title": bucket.title, "rubric": bucket.rubric})
