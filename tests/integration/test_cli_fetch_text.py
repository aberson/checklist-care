"""``checklist-care fetch --text-file '<path>' [--title TITLE]`` through ``cli.main``.

The text source of plan §4.3 end to end: the file as the skill leaves it in
``<data-dir>/inbox/`` (plan §3.1, §4.6), the text packet (plan §4.4), the ``source``
row, ``context`` for the text ref, and ``propose`` on that packet, where a text
tip's ``start_s`` must be null (plan §4.5). No network seam is involved. All text
here is self-written.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from checklist_care import cli, context, errors, limits, paths, proposal
from checklist_care.db import connection
from checklist_care.sources import text, youtube

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-08T12:00:00Z"

# Every character exists in Windows-1252, so the same text has a cp1252 form.
SAMPLE = (
    "Notes from a talk on calmer mornings — “small steps” only.\n"
    "Fill a glass of water while the kettle boils, before the café-style coffee.\n"
    "Put tomorrow's bag by the door at night: 2 minutes, naïve but it works. 5 € saved.\n"
)
REF = "text:sha256:" + hashlib.sha256(SAMPLE.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(connection, "utc_now", lambda: NOW)

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a text fetch must not touch the YouTube seams")

    monkeypatch.setattr(youtube, "make_transcript_client", no_network)
    monkeypatch.setattr(youtube, "run_ytdlp", no_network)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    target = tmp_path / "data"
    code, _, err = run(capsys, "--data-dir", str(target), "init")
    assert code == 0, err
    return target


def inbox_file(data_dir: Path, data: bytes, n: int = 1) -> Path:
    """A file where the skill puts it: ``<data-dir>/inbox/<YYYYMMDD-HHMMSS>-<n>.txt``."""

    target = data_dir / paths.INBOX_DIR / f"20261108-120000-{n}.txt"
    target.write_bytes(data)
    return target


def fetch_text(
    capsys: pytest.CaptureFixture[str], data_dir: Path, file: Path, *extra: str
) -> tuple[int, str, list[str]]:
    return run(capsys, "--data-dir", str(data_dir), "fetch", "--text-file", str(file), *extra)


def packet_file(data_dir: Path, ref: str = REF) -> Path:
    return data_dir / paths.PACKETS_DIR / f"{paths.source_key(ref)}.json"


def query(data_dir: Path, sql: str, *params: Any) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def source_rows(data_dir: Path) -> list[dict[str, Any]]:
    return query(data_dir, "SELECT * FROM source WHERE kind <> 'manual' ORDER BY id")


def dump(data_dir: Path) -> list[str]:
    with closing(sqlite3.connect(data_dir / paths.DB_FILENAME)) as conn:
        return list(conn.iterdump())


def listing(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


# --- a valid text file -----------------------------------------------------------------


def test_fetch_text_file_writes_a_text_packet_and_prints_its_sha256(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    source_file = inbox_file(data_dir, SAMPLE.encode("utf-8"))

    code, out, err = fetch_text(capsys, data_dir, source_file, "--title", "Calmer mornings")

    assert code == 0, err
    target = packet_file(data_dir)
    raw = target.read_bytes()
    assert out.splitlines() == [
        f"packet: {target}",
        f"source_ref: {REF}",
        f"packet_sha256: {hashlib.sha256(raw).hexdigest()}",
        f"counts: {len(SAMPLE)}/0",
    ]
    assert err == [f"data_dir: {data_dir}"]
    expected = {
        "schema": "checklist-care.packet/1",
        "source_ref": REF,
        "kind": "text",
        "url": None,
        "title": "Calmer mornings",
        "channel": None,
        "duration_s": None,
        "fetched_at": NOW_TEXT,
        "transcript": None,
        "chapters": [],
        "text": SAMPLE,
        "sections": [],
        "warnings": [],
    }
    assert json.loads(raw.decode("utf-8")) == expected
    assert list(json.loads(raw)) == list(expected)  # §4.4 key order
    assert not raw.startswith(codecs.BOM_UTF8) and b"\r" not in raw
    assert "café" in raw.decode("utf-8")  # UTF-8, not \u-escaped
    assert source_rows(data_dir) == [
        {
            "id": 2,
            "kind": "text",
            "ref": REF,
            "title": "Calmer mornings",
            "url": None,
            "revision": None,
            "mode": "extract",
            "fetched_at": NOW_TEXT,
            "reviewed_at": None,
        }
    ]
    assert source_file.read_bytes() == SAMPLE.encode("utf-8")  # the input is left as it was


def test_without_a_title_the_packet_and_row_have_none(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code, _, err = fetch_text(capsys, data_dir, inbox_file(data_dir, SAMPLE.encode("utf-8")))

    assert code == 0, err
    assert json.loads(packet_file(data_dir).read_bytes())["title"] is None
    assert source_rows(data_dir)[0]["title"] is None


def test_a_cp1252_file_and_a_utf8_bom_file_give_the_same_ref(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    cp1252 = inbox_file(data_dir, SAMPLE.replace("\n", "\r\n").encode("cp1252"), n=1)
    utf8_bom = inbox_file(data_dir, codecs.BOM_UTF8 + SAMPLE.encode("utf-8"), n=2)
    assert cp1252.read_bytes() != utf8_bom.read_bytes()

    first = fetch_text(capsys, data_dir, cp1252)
    second = fetch_text(capsys, data_dir, utf8_bom)

    assert first[0] == 0 and second[0] == 0, (first[2], second[2])
    first_ref = first[1].splitlines()[1]
    second_ref = second[1].splitlines()[1]
    assert first_ref == second_ref == f"source_ref: {REF}"
    assert first[2][1] == f"warning: {text.CP1252_WARNING}"  # only the cp1252 file warns
    assert second[2] == [f"data_dir: {data_dir}"]
    assert [row["ref"] for row in source_rows(data_dir)] == [REF]  # one source, refreshed
    assert listing(data_dir / paths.PACKETS_DIR) == [packet_file(data_dir).name]
    stored = json.loads(packet_file(data_dir).read_bytes())
    assert stored["text"] == SAMPLE and stored["warnings"] == []


# --- refused files write nothing --------------------------------------------------------


def test_a_file_over_200000_characters_exits_4_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    too_long = inbox_file(data_dir, b"a" * (text.TEXT_CHARS_MAX + 1))
    before = dump(data_dir)

    code, out, err = fetch_text(capsys, data_dir, too_long)

    assert code == errors.EXIT_FETCH == 4
    assert out == ""
    assert err[1].startswith("CC_E_FETCH: the text is 200,001 characters, over the ")
    assert "too long; split it" in err[1]
    assert len(err) == 2
    assert listing(data_dir / paths.PACKETS_DIR) == []
    assert dump(data_dir) == before


def test_a_file_of_exactly_200000_characters_is_fetched(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    code, out, err = fetch_text(capsys, data_dir, inbox_file(data_dir, b"a" * text.TEXT_CHARS_MAX))

    assert code == 0, err
    assert out.splitlines()[-1] == f"counts: {text.TEXT_CHARS_MAX}/0"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (codecs.BOM_UTF16_LE + SAMPLE.encode("utf-16-le"), "the text file is UTF-16"),
        (b"%PDF-1.7\n\x00\x00 binary", "the text file holds NUL bytes"),
        (b"abc \x81 def", "the text file is neither UTF-8 nor Windows-1252 text"),
        (b" \r\n \n", "the text file holds no text"),
    ],
)
def test_an_unusable_file_exits_4_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path, data: bytes, message: str
) -> None:
    before = dump(data_dir)

    code, out, err = fetch_text(capsys, data_dir, inbox_file(data_dir, data))

    assert code == 4
    assert out == ""
    assert err[1].startswith(f"CC_E_FETCH: {message}")
    assert listing(data_dir / paths.PACKETS_DIR) == []
    assert dump(data_dir) == before


def test_a_missing_file_exits_4(capsys: pytest.CaptureFixture[str], data_dir: Path) -> None:
    missing = data_dir / paths.INBOX_DIR / "20261108-120000-9.txt"

    code, out, err = fetch_text(capsys, data_dir, missing)

    assert code == 4
    assert out == ""
    assert err[1].startswith(f"CC_E_FETCH: cannot read the text file '{missing}': ")
    assert source_rows(data_dir) == []


@pytest.mark.parametrize("make", ["directory", "device"])
def test_a_folder_or_a_device_exits_4_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path, make: str
) -> None:
    if make == "directory":
        target = data_dir / paths.INBOX_DIR / "20261108-120000-1.txt"
        target.mkdir()
    else:
        target = Path(os.devnull)  # NUL on Windows, /dev/null elsewhere
    before = dump(data_dir)

    code, out, err = fetch_text(capsys, data_dir, target)

    assert code == 4
    assert out == ""
    assert err[1] == (
        f"CC_E_FETCH: cannot read the text file '{target}': it is not a regular file "
        "(a folder, a device or a pipe); give the path of a text file"
    )
    assert listing(data_dir / paths.PACKETS_DIR) == []
    assert dump(data_dir) == before


def test_fetch_text_without_a_database_exits_2_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_file = tmp_path / "notes.txt"
    source_file.write_bytes(SAMPLE.encode("utf-8"))
    target = tmp_path / "data"

    code, out, err = fetch_text(capsys, target, source_file)

    assert code == 2
    assert out == ""
    assert err[1].startswith("CC_E_CONFIG: no database at ")
    assert not target.exists()


# --- usage -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["fetch"], "one of the arguments URL --text-file is required"),
        (["fetch", "LpeDuC2sQ-w", "--text-file", "x.txt"], "not allowed with"),
        (["fetch", "LpeDuC2sQ-w", "--title", "Notes"], "--title: only allowed with --text-file"),
        (["fetch", "--text-file", "x.txt", "--title", " \n "], "--title: the title is empty"),
        # A repeated option (a title split by a quoting slip) is refused, not "last wins".
        (
            ["fetch", "--text-file", "x.txt", "--title", "a", "--text-file", "y.txt"],
            "argument --text-file: given more than once",
        ),
        (
            ["fetch", "--text-file", "x.txt", "--title", "a", "--title", "b"],
            "argument --title: given more than once",
        ),
    ],
)
def test_usage_errors_exit_2_before_anything_runs(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, argv: list[str], message: str
) -> None:
    target = tmp_path / "data"

    code, out, err = run(capsys, "--data-dir", str(target), *argv)

    assert code == 2
    assert out == ""
    assert message in " ".join(err)
    assert not target.exists()


def test_fetch_help_names_text_file_and_title(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "fetch", "--help")

    assert code == 0
    assert "--text-file PATH" in out and "--title TITLE" in out
    assert "Put the URL in single quotes" in " ".join(out.split())


# --- context and propose on a text packet ----------------------------------------------


def text_proposal(ctx: dict[str, Any], **tip_values: Any) -> dict[str, Any]:
    """A valid extract proposal for the fetched text, fingerprints from ``context``."""

    tip: dict[str, Any] = {
        "name": "Glass By The Kettle",
        "action": "Fill a glass of water while the kettle boils",
        "reason": "Pairing water with a drink you already make turns it into a habit.",
        "bucket": limits.BUCKET_DAILY,
        "audience": limits.AUDIENCE_ADULTS,
        "list_ids": [7],
        "start_s": None,
        "locator": None,
        "evidence_url": None,
        "duplicate_of": None,
        "possible_duplicate_of": None,
        "note": "daily, under a minute, at the first hot drink",
    }
    tip.update(tip_values)
    return {
        "schema": proposal.PROPOSAL_SCHEMA,
        "source_ref": ctx["source"]["ref"],
        "packet_sha256": ctx["source"]["packet_sha256"],
        "skill_sha256": ctx["skill_sha256"],
        "host": limits.HOST_CLAUDE_CODE,
        "model": "synthetic-test",
        "mode": limits.SOURCE_MODE_EXTRACT,
        "degraded": False,
        "tips": [tip],
    }


def write_draft(data_dir: Path, document: dict[str, Any]) -> Path:
    target = data_dir / paths.PROPOSALS_DIR / f"draft-{paths.source_key(REF)}.json"
    target.write_bytes(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
    return target


@pytest.fixture
def fetched(capsys: pytest.CaptureFixture[str], data_dir: Path) -> dict[str, Any]:
    """The text fetched, then ``context --source-ref`` for it."""

    code, _, err = fetch_text(capsys, data_dir, inbox_file(data_dir, SAMPLE.encode("utf-8")))
    assert code == 0, err
    code, out, err = run(capsys, "--data-dir", str(data_dir), "context", "--source-ref", REF)
    assert code == 0, err
    ctx: dict[str, Any] = json.loads(out)
    return ctx


def test_context_describes_the_text_source(fetched: dict[str, Any], data_dir: Path) -> None:
    assert fetched["skill_sha256"] == context.skill_sha256()
    assert fetched["source"] == {
        "ref": REF,
        "kind": "text",
        "packet_path": str(packet_file(data_dir)),
        "packet_sha256": hashlib.sha256(packet_file(data_dir).read_bytes()).hexdigest(),
        "duration_s": None,
        "chapters": [],
        "section_headings": [],
    }


@pytest.mark.parametrize("start_s", [0, 12, 12.0, "12"])
def test_a_text_tip_with_a_start_s_exits_5_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path, fetched: dict[str, Any], start_s: Any
) -> None:
    draft = write_draft(data_dir, text_proposal(fetched, start_s=start_s))
    before = dump(data_dir)
    proposals_before = listing(data_dir / paths.PROPOSALS_DIR)

    code, out, err = run(capsys, "--data-dir", str(data_dir), "propose", str(draft))

    assert code == errors.EXIT_PROPOSAL == 5
    assert err[1].startswith("CC_E_PROPOSAL: ")
    assert json.loads(out) == [
        {
            "index": 0,
            "field": "start_s",
            "problem": "must be null: a text source has no time positions",
        }
    ]
    assert dump(data_dir) == before
    assert listing(data_dir / paths.PROPOSALS_DIR) == proposals_before


def test_a_text_proposal_with_null_start_s_is_imported_and_the_text_is_then_reviewed(
    capsys: pytest.CaptureFixture[str], data_dir: Path, fetched: dict[str, Any]
) -> None:
    draft = write_draft(data_dir, text_proposal(fetched))

    code, out, err = run(capsys, "--data-dir", str(data_dir), "propose", str(draft))

    assert code == 0, (out, err)
    archive = data_dir / paths.PROPOSALS_DIR / f"{paths.source_key(REF)}.1.json"
    assert out.splitlines()[:2] == [f"proposal: {archive}", f"source_ref: {REF}"]
    tips = query(data_dir, "SELECT name, start_s, status, source_id FROM tip")
    source = source_rows(data_dir)[0]
    assert tips == [
        {
            "name": "Glass By The Kettle",
            "start_s": None,
            "status": limits.TIP_STATUS_PENDING,
            "source_id": source["id"],
        }
    ]
    assert source["reviewed_at"] == NOW_TEXT
    assert query(data_dir, "SELECT list_id, position FROM list_item") == [
        {"list_id": 7, "position": 1}
    ]

    # The same text again, now in Windows-1252 with CRLF, is the same reviewed source.
    again = inbox_file(data_dir, SAMPLE.replace("\n", "\r\n").encode("cp1252"), n=2)
    before_packet = packet_file(data_dir).read_bytes()

    code, out, err = fetch_text(capsys, data_dir, again)

    assert code == errors.EXIT_ALREADY_REVIEWED == 3
    assert out == ""
    assert err[1].startswith(f"CC_ALREADY_REVIEWED: {REF} was already reviewed at ")
    assert packet_file(data_dir).read_bytes() == before_packet
