"""init, status and backup through the production entry point ``cli.main``.

Every in-process test calls ``cli.main(argv)``. The subprocess tests run the
real ``python -m checklist_care.cli`` and the ``checklist-care`` console script.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

import checklist_care
from checklist_care import cli, paths
from checklist_care.db import connection, seed

EMOJI = "\U0001f600"

EXPECTED_LISTS = [
    (1, "Adult Bathroom", "adults", "Morning and bedtime routines: teeth, face, getting ready"),
    (2, "My Office", "adults", "Working at the desk or computer"),
    (3, "Kids Bathroom", "kids", "Kids' morning and bedtime routines"),
    (4, "Kids Bathroom", "adults", "Helping kids get ready; adults using the kids' bathroom"),
    (5, "Front Door", "kids", "Leaving for school or coming home"),
    (6, "Front Door", "adults", "Leaving the house or coming home"),
    (7, "Coffee Machine", "adults", "First coffee or hot drink of the day"),
]

EXPECTED_BUCKET_TITLES = [
    (1, "Daily checklist"),
    (2, "Occasional activity"),
    (3, "Optional / when time allows"),
    (4, "Left out"),
]


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real data directory: the defaults point into tmp_path."""

    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


def init_db(capsys: pytest.CaptureFixture[str], data_dir: Path) -> Path:
    code, _, err = run(capsys, "--data-dir", str(data_dir), "init")
    assert code == 0, err
    return data_dir / "checklist-care.db"


def dump(db_path: Path) -> list[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return list(conn.iterdump())


@pytest.fixture
def db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[sqlite3.Connection]:
    """A database created by ``init``, opened through the production ``connect``."""

    db_path = init_db(capsys, tmp_path / "data")
    with closing(connection.connect(db_path)) as conn:
        yield conn


# --- init ----------------------------------------------------------------------------


def test_init_creates_the_seeded_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    code, out, err = run(capsys, "--data-dir", str(data_dir), "init")

    assert code == 0
    db_path = data_dir / "checklist-care.db"
    assert err[0] == f"data_dir: {data_dir}"
    assert out.splitlines() == [
        f"db_path: {db_path}",
        "schema_version: 1",
        "result: created",
    ]
    for name in ("packets", "inbox", "proposals", "backups", "eval"):
        assert (data_dir / name).is_dir()

    with closing(connection.connect(db_path)) as conn:
        assert connection.schema_version(conn) == 1
        buckets = conn.execute("SELECT id, title, rubric FROM bucket ORDER BY id").fetchall()
        lists = conn.execute("SELECT id, place, audience, cue FROM list ORDER BY id").fetchall()
        sources = conn.execute("SELECT kind, ref, mode, title, reviewed_at FROM source").fetchall()

    assert [(row["id"], row["title"]) for row in buckets] == EXPECTED_BUCKET_TITLES
    assert [row["rubric"] for row in buckets] == [bucket.rubric for bucket in seed.BUCKETS]
    assert buckets[0]["rubric"].startswith("Worth doing every day, or every time a cue happens.")
    assert buckets[3]["rubric"].endswith("Duplicates record which tip they repeat. Never printed.")
    assert [tuple(row) for row in lists] == EXPECTED_LISTS
    assert [tuple(row) for row in sources] == [("manual", "manual", "manual", "My own tips", None)]


def test_init_twice_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(connection, "utc_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    db_path = init_db(capsys, data_dir)
    before = dump(db_path)

    # A later clock proves the second run does not rewrite updated_at either.
    monkeypatch.setattr(connection, "utc_now", lambda: datetime(2027, 6, 1, tzinfo=UTC))
    code, out, _ = run(capsys, "--data-dir", str(data_dir), "init")

    assert code == 0
    assert "result: already initialized" in out.splitlines()
    assert dump(db_path) == before
    assert any("2026-01-01T00:00:00Z" in line for line in before)


def test_init_keeps_operator_edits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], db: sqlite3.Connection
) -> None:
    db.execute("UPDATE list SET cue = 'Edited cue' WHERE id = 7")
    db.execute("UPDATE bucket SET rubric = 'Edited rubric' WHERE id = 2")

    code, _, _ = run(capsys, "--data-dir", str(tmp_path / "data"), "init")

    assert code == 0
    assert db.execute("SELECT cue FROM list WHERE id = 7").fetchone()[0] == "Edited cue"
    assert db.execute("SELECT rubric FROM bucket WHERE id = 2").fetchone()[0] == "Edited rubric"


@pytest.mark.parametrize("command", [["init"], ["status"], ["status", "--json"], ["backup"]])
@pytest.mark.parametrize("position", ["before", "after"])
def test_data_dir_works_before_or_after_the_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str], position: str
) -> None:
    data_dir = tmp_path / "data"
    if command != ["init"]:
        init_db(capsys, data_dir)
    flag = ["--data-dir", str(data_dir)]
    argv = flag + command if position == "before" else command + flag

    code, _, err = run(capsys, *argv)

    assert code == 0, err
    assert err[0] == f"data_dir: {data_dir}"
    assert (data_dir / "checklist-care.db").is_file()
    assert not (tmp_path / "localappdata").exists()


def test_env_var_is_used_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(env_dir))

    code, _, err = run(capsys, "init")

    assert code == 0
    assert err[0] == f"data_dir: {env_dir}"
    assert (env_dir / "checklist-care.db").is_file()


def test_flag_wins_over_env_var(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "from-env"))

    code, _, err = run(capsys, "init", "--data-dir", str(tmp_path / "from-flag"))

    assert code == 0
    assert err[0] == f"data_dir: {tmp_path / 'from-flag'}"
    assert not (tmp_path / "from-env").exists()


def test_default_data_dir_is_used_without_flag_or_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / ("localappdata" if os.name == "nt" else "xdg")
    expected = base / "checklist-care"

    code, _, err = run(capsys, "init")

    assert code == 0
    assert err[0] == f"data_dir: {expected}"
    assert (expected / "checklist-care.db").is_file()


def test_empty_env_var_exits_2_with_config_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, "")

    code, out, err = run(capsys, "status")

    assert code == 2
    assert out == ""
    assert err[0].startswith("CC_E_CONFIG: ")


# --- git-checkout refusal ------------------------------------------------------------


@pytest.mark.parametrize("command", [["init"], ["status", "--json"], ["backup"]])
@pytest.mark.parametrize("marker_kind", ["dir", "file"])
def test_data_dir_inside_a_git_checkout_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str], marker_kind: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    if marker_kind == "dir":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    data_dir = repo / "private" / "data"

    code, out, err = run(capsys, "--data-dir", str(data_dir), *command)

    assert code == 2
    assert out == ""
    assert err[0] == f"data_dir: {data_dir}"
    assert err[1].startswith("CC_E_DATA_IN_REPO: ")
    assert len(err) == 2
    assert not (repo / "private").exists()  # nothing was created inside the checkout


def test_data_dir_inside_this_checkout_via_env_exits_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = Path(__file__).resolve().parents[2]
    target = checkout / "tests" / "integration" / "never-created-data-dir"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(target))

    code, _, err = run(capsys, "init")

    assert code == 2
    assert err[1].startswith("CC_E_DATA_IN_REPO: ")
    assert not target.exists()


# --- status --------------------------------------------------------------------------


def test_status_json_prints_the_documented_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], db: sqlite3.Connection
) -> None:
    data_dir = tmp_path / "data"
    insert_tip(db, bucket=2, audience="adults", status="pending")
    insert_tip(db, bucket=1, audience="kids+", status="approved")
    insert_tip(db, bucket=1, audience="adults", status="pending")

    code, out, err = run(capsys, "status", "--json", "--data-dir", str(data_dir))

    assert code == 0
    assert err == [f"data_dir: {data_dir}"]
    payload = json.loads(out)
    assert payload == {
        "data_dir": str(data_dir),
        "db_path": str(data_dir / "checklist-care.db"),
        "schema_version": 1,
        "counts": {
            "sources": 1,
            "tips_by_bucket": {"1": 2, "2": 1, "3": 0, "4": 0},
            "pending": 2,
            "lists": 7,
        },
    }
    assert len(out.splitlines()) == 1


def test_status_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    init_db(capsys, data_dir)

    code, out, _ = run(capsys, "--data-dir", str(data_dir), "status")

    assert code == 0
    assert out.splitlines() == [
        f"data_dir: {data_dir}",
        f"db_path: {data_dir / 'checklist-care.db'}",
        "schema_version: 1",
        "sources: 1",
        "tips: 0 (bucket 1: 0, bucket 2: 0, bucket 3: 0, bucket 4: 0)",
        "pending: 0",
        "lists: 7",
    ]


@pytest.mark.parametrize("command", [["status"], ["status", "--json"], ["backup"]])
def test_commands_without_a_database_exit_2_and_create_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    data_dir = tmp_path / "data"

    code, out, err = run(capsys, "--data-dir", str(data_dir), *command)

    assert code == 2
    assert out == ""
    assert err[1].startswith("CC_E_CONFIG: no database at ")
    assert "checklist-care init" in err[1]
    assert not data_dir.exists()


@pytest.mark.parametrize("command", [["status"], ["status", "--json"], ["backup"]])
def test_commands_on_a_zero_byte_database_exit_2_and_leave_it_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "checklist-care.db"
    db_path.touch()

    code, out, err = run(capsys, "--data-dir", str(data_dir), *command)

    assert code == 2
    assert out == ""
    assert err[1].startswith("CC_E_CONFIG: no database at ")
    assert db_path.stat().st_size == 0
    assert sorted(p.name for p in data_dir.iterdir()) == ["checklist-care.db"]


def test_init_seeds_a_zero_byte_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "checklist-care.db").touch()

    code, out, _ = run(capsys, "--data-dir", str(data_dir), "init")

    assert code == 0
    assert "result: created" in out.splitlines()
    status_code, status_out, _ = run(capsys, "--data-dir", str(data_dir), "status", "--json")
    assert status_code == 0
    assert json.loads(status_out)["counts"]["lists"] == 7


def test_unknown_schema_version_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = init_db(capsys, tmp_path / "data")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA user_version = 2")

    code, _, err = run(capsys, "--data-dir", str(tmp_path / "data"), "status")

    assert code == 2
    assert err[1].startswith("CC_E_CONFIG: ")
    assert "schema version 2" in err[1]


def test_a_file_that_is_not_a_database_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "checklist-care.db").write_bytes(b"this is not sqlite " * 100)

    code, _, err = run(capsys, "--data-dir", str(data_dir), "status", "--json")

    assert code == 2
    assert err[1].startswith("CC_E_CONFIG: ")


def test_a_foreign_database_is_not_adopted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        conn.execute("CREATE TABLE something_else (x)")

    code, _, err = run(capsys, "--data-dir", str(data_dir), "init")

    assert code == 2
    assert "not a checklist-care database" in err[1]


# --- backup --------------------------------------------------------------------------


def test_backup_writes_a_db_with_the_same_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], db: sqlite3.Connection
) -> None:
    data_dir = tmp_path / "data"
    insert_tip(db, bucket=1, audience="kids+", status="approved", name=f"Smile {EMOJI}")
    db.execute("INSERT INTO list_item (list_id, tip_id, position) VALUES (3, 1, 1)")

    code, out, err = run(capsys, "--data-dir", str(data_dir), "backup")

    assert code == 0
    assert err == [f"data_dir: {data_dir}"]
    match = re.fullmatch(r"backup: (.+)", out.strip())
    assert match is not None
    backup = Path(match.group(1))
    assert backup.parent == data_dir / "backups"
    assert re.fullmatch(r"checklist-care-\d{8}-\d{6}\.db", backup.name)
    assert dump(backup) == dump(data_dir / "checklist-care.db")
    with closing(connection.connect(backup)) as copy:
        assert connection.schema_version(copy) == 1
        assert copy.execute("SELECT name FROM tip").fetchone()[0] == f"Smile {EMOJI}"
        assert copy.execute("SELECT count(*) FROM list").fetchone()[0] == 7


def test_two_backups_in_the_same_second_both_survive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    init_db(capsys, data_dir)
    monkeypatch.setattr(connection, "utc_now", lambda: datetime(2026, 11, 8, 12, 0, tzinfo=UTC))

    first = run(capsys, "--data-dir", str(data_dir), "backup")
    second = run(capsys, "--data-dir", str(data_dir), "backup")

    assert first[0] == second[0] == 0
    assert sorted(p.name for p in (data_dir / "backups").iterdir()) == [
        "checklist-care-20261108-120000-2.db",
        "checklist-care-20261108-120000.db",
    ]


# --- placement triggers (plan §3.3) ----------------------------------------------------


def insert_tip(
    conn: sqlite3.Connection,
    *,
    bucket: int,
    audience: str,
    status: str = "approved",
    name: str = "A Tip",
) -> int:
    source_id = conn.execute("SELECT id FROM source WHERE ref = 'manual'").fetchone()[0]
    cursor = conn.execute(
        "INSERT INTO tip (name, action, bucket, audience, status, source_id, created_at, "
        "updated_at) VALUES (?, 'Do the thing', ?, ?, ?, ?, ?, ?)",
        (name, bucket, audience, status, source_id, "2026-11-08T00:00:00Z", "2026-11-08T00:00:00Z"),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def place(conn: sqlite3.Connection, list_id: int, tip_id: int) -> None:
    conn.execute(
        "INSERT INTO list_item (list_id, tip_id, position) VALUES (?, ?, 1)", (list_id, tip_id)
    )


ADULTS_LIST, KIDS_LIST = 1, 3


@pytest.mark.parametrize(
    "bucket",
    [
        pytest.param(2, id="bucket-2"),  # done-when 8: a bucket-2 tip on a list raises
        pytest.param(3, id="bucket-3"),
        pytest.param(4, id="bucket-4"),
    ],
)
def test_list_item_for_any_non_daily_tip_raises(db: sqlite3.Connection, bucket: int) -> None:
    tip = insert_tip(db, bucket=bucket, audience="adults")
    with pytest.raises(sqlite3.IntegrityError, match="bucket-1"):
        place(db, ADULTS_LIST, tip)
    assert db.execute("SELECT count(*) FROM list_item").fetchone()[0] == 0


def test_adults_tip_on_a_kids_list_raises(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="adults")
    with pytest.raises(sqlite3.IntegrityError, match="kids list"):
        place(db, KIDS_LIST, tip)
    assert db.execute("SELECT count(*) FROM list_item").fetchone()[0] == 0


def test_compatible_placements_are_accepted(db: sqlite3.Connection) -> None:
    # Positive controls: the triggers do not reject everything.
    kids_tip = insert_tip(db, bucket=1, audience="kids+")
    adults_tip = insert_tip(db, bucket=1, audience="adults")
    place(db, KIDS_LIST, kids_tip)
    place(db, ADULTS_LIST, kids_tip)  # an adults list accepts kids+ tips too
    place(db, ADULTS_LIST, adults_tip)
    assert db.execute("SELECT count(*) FROM list_item").fetchone()[0] == 3


def test_moving_a_list_item_onto_a_kids_list_raises(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="adults")
    place(db, ADULTS_LIST, tip)
    with pytest.raises(sqlite3.IntegrityError, match="kids list"):
        db.execute("UPDATE list_item SET list_id = ? WHERE tip_id = ?", (KIDS_LIST, tip))


def test_repointing_a_list_item_at_a_bucket_2_tip_raises(db: sqlite3.Connection) -> None:
    daily = insert_tip(db, bucket=1, audience="adults")
    activity = insert_tip(db, bucket=2, audience="adults")
    place(db, ADULTS_LIST, daily)
    with pytest.raises(sqlite3.IntegrityError, match="bucket-1"):
        db.execute("UPDATE list_item SET tip_id = ? WHERE tip_id = ?", (activity, daily))


def test_reordering_a_list_item_is_allowed(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="adults")
    place(db, ADULTS_LIST, tip)
    db.execute("UPDATE list_item SET position = 5 WHERE tip_id = ?", (tip,))
    assert db.execute("SELECT position FROM list_item").fetchone()[0] == 5


def test_placed_tip_cannot_leave_bucket_1(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="kids+")
    place(db, ADULTS_LIST, tip)
    with pytest.raises(sqlite3.IntegrityError, match="leaves bucket 1"):
        db.execute("UPDATE tip SET bucket = 2 WHERE id = ?", (tip,))
    # Deleting the placement first, in one transaction, is the supported path.
    with connection.transaction(db):
        db.execute("DELETE FROM list_item WHERE tip_id = ?", (tip,))
        db.execute("UPDATE tip SET bucket = 2 WHERE id = ?", (tip,))
    assert db.execute("SELECT bucket FROM tip WHERE id = ?", (tip,)).fetchone()[0] == 2


def test_tip_on_a_kids_list_cannot_become_adults(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="kids+")
    place(db, KIDS_LIST, tip)
    with pytest.raises(sqlite3.IntegrityError, match="becomes adults"):
        db.execute("UPDATE tip SET audience = 'adults' WHERE id = ?", (tip,))


def test_tip_on_an_adults_list_may_become_adults(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="kids+")
    place(db, ADULTS_LIST, tip)
    db.execute("UPDATE tip SET audience = 'adults' WHERE id = ?", (tip,))
    assert db.execute("SELECT audience FROM tip WHERE id = ?", (tip,)).fetchone()[0] == "adults"


def test_list_holding_an_adults_tip_cannot_become_kids(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="adults")
    place(db, ADULTS_LIST, tip)
    with pytest.raises(sqlite3.IntegrityError, match="becomes kids"):
        db.execute("UPDATE list SET audience = 'kids' WHERE id = ?", (ADULTS_LIST,))


def test_failed_transaction_writes_nothing(db: sqlite3.Connection) -> None:
    tip = insert_tip(db, bucket=1, audience="adults")
    before = list(db.iterdump())
    with pytest.raises(sqlite3.IntegrityError), connection.transaction(db):
        place(db, ADULTS_LIST, tip)
        place(db, KIDS_LIST, tip)
    assert not db.in_transaction
    assert list(db.iterdump()) == before


def test_duplicate_of_requires_bucket_4(db: sqlite3.Connection) -> None:
    original = insert_tip(db, bucket=1, audience="adults")
    duplicate = insert_tip(db, bucket=4, audience="adults")
    db.execute("UPDATE tip SET duplicate_of = ? WHERE id = ?", (original, duplicate))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE tip SET bucket = 3 WHERE id = ?", (duplicate,))


def test_connection_pragmas(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert db.row_factory is sqlite3.Row


# --- crashes -------------------------------------------------------------------------


def test_unexpected_exception_is_one_internal_line_exit_9(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: sqlite3.Connection, *, now: str | None = None) -> None:
        raise RuntimeError("seed exploded\nsecond line")

    monkeypatch.setattr(seed, "seed_database", boom)

    code, out, err = run(capsys, "--data-dir", str(tmp_path / "data"), "init")

    assert code == 9
    assert out == ""
    assert err == [
        f"data_dir: {tmp_path / 'data'}",
        "CC_E_INTERNAL: unexpected RuntimeError: seed exploded second line",
    ]


def test_debug_adds_the_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: sqlite3.Connection, *, now: str | None = None) -> None:
        raise RuntimeError("seed exploded")

    monkeypatch.setattr(seed, "seed_database", boom)

    code, _, err = run(capsys, "init", "--debug", "--data-dir", str(tmp_path / "data"))

    assert code == 9
    assert "Traceback (most recent call last):" in err
    assert err[-1] == "CC_E_INTERNAL: unexpected RuntimeError: seed exploded"


def test_usage_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "no-such-command")
    assert code == 2
    assert any("invalid choice" in line for line in err)


# --- package metadata ----------------------------------------------------------------


def test_version_flag_and_metadata_agree(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--version")
    assert code == 0
    assert out.strip() == f"checklist-care {checklist_care.__version__}"
    assert importlib.metadata.version("checklist-care") == checklist_care.__version__


def test_project_url_and_user_agent() -> None:
    assert checklist_care.PROJECT_URL == "https://github.com/aberson/checklist-care"
    assert "@" not in checklist_care.USER_AGENT


# --- real processes: UTF-8 stdout whatever the console encoding ----------------------


def _child_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        not in {"PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO", paths.DATA_DIR_ENV}
    }
    env.update(overrides)
    return env


def _run_child(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, env=env, timeout=120, check=False)


def _assert_emoji_round_trip(command: list[str], data_dir: Path, env: dict[str, str]) -> None:
    init = _run_child([*command, "--data-dir", str(data_dir), "init"], env)
    assert init.returncode == 0, init.stderr.decode("utf-8", "replace")

    status = _run_child([*command, "status", "--json", "--data-dir", str(data_dir)], env)

    assert status.returncode == 0, status.stderr.decode("utf-8", "replace")
    assert EMOJI.encode("utf-8") in status.stdout  # raw UTF-8, not a \\u escape
    payload = json.loads(status.stdout.decode("utf-8"))
    assert payload["data_dir"] == str(data_dir)
    assert payload["db_path"] == str(data_dir / "checklist-care.db")
    assert payload["counts"]["lists"] == 7
    first_stderr_line = status.stderr.decode("utf-8").splitlines()[0]
    assert first_stderr_line == f"data_dir: {data_dir}"


def test_status_json_round_trips_an_emoji_with_pythonioencoding_unset(tmp_path: Path) -> None:
    data_dir = tmp_path / f"data-{EMOJI}-ü"
    command = [sys.executable, "-m", "checklist_care.cli"]
    _assert_emoji_round_trip(command, data_dir, _child_env())


@pytest.mark.parametrize("forced", ["ascii", "cp1252"])
def test_status_json_round_trips_an_emoji_under_a_hostile_stdout_encoding(
    tmp_path: Path, forced: str
) -> None:
    # The red anchor below shows this environment breaks a plain print().
    data_dir = tmp_path / f"data-{EMOJI}"
    command = [sys.executable, "-m", "checklist_care.cli"]
    _assert_emoji_round_trip(command, data_dir, _child_env(PYTHONIOENCODING=forced))


@pytest.mark.parametrize("forced", ["ascii", "cp1252"])
def test_anchor_plain_print_of_an_emoji_fails_under_that_encoding(forced: str) -> None:
    result = _run_child(
        [sys.executable, "-c", f"print({EMOJI!r})"], _child_env(PYTHONIOENCODING=forced)
    )
    assert result.returncode != 0
    assert b"UnicodeEncodeError" in result.stderr


def test_console_script_round_trips_an_emoji(tmp_path: Path) -> None:
    script = shutil.which("checklist-care", path=str(Path(sys.executable).parent))
    assert script is not None, "the checklist-care console script is not installed"
    _assert_emoji_round_trip([script], tmp_path / f"script-{EMOJI}", _child_env())
