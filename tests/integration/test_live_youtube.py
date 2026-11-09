"""Live YouTube fetch through ``cli.main`` with the real seams (manual step M1).

Marked ``live``: the default gate (``addopts = -m 'not live'``) never runs it.
Run it with ``uv run pytest -m live``. A network or block error here (``IpBlocked``,
``RequestBlocked``, HTTP 429/503, DNS) is recorded as
``LIVE: BLOCKED-EXTERNAL (<class>)`` and retried later, not as a code failure.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from checklist_care import cli, paths

pytestmark = pytest.mark.live

VIDEO = "LpeDuC2sQ-w"
REF = f"youtube:{VIDEO}"

# Public chapter metadata (plan Appendix F), not transcript text.
EXPECTED_CHAPTERS = [
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


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def test_live_fetch_of_the_in_sample_video(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    assert cli.main(["--data-dir", str(data_dir), "init"]) == 0
    capsys.readouterr()

    code = cli.main(
        ["--data-dir", str(data_dir), "fetch", f"https://www.youtube.com/watch?v={VIDEO}&t=10s"]
    )
    captured = capsys.readouterr()

    assert code == 0, captured.err
    target = data_dir / "packets" / f"{paths.source_key(REF)}.json"
    raw = target.read_bytes()
    lines = captured.out.splitlines()
    assert lines[:3] == [
        f"packet: {target}",
        f"source_ref: {REF}",
        f"packet_sha256: {hashlib.sha256(raw).hexdigest()}",
    ]
    payload = json.loads(raw)
    assert payload["warnings"] == []
    assert payload["transcript"]["kind"] == "generated"
    assert payload["transcript"]["language_code"] == "en"
    segments = payload["transcript"]["segments"]
    assert len(segments) > 50
    assert 9_000 < sum(len(segment["text"]) for segment in segments) < 15_000
    assert [chapter["title"] for chapter in payload["chapters"]] == EXPECTED_CHAPTERS
    assert payload["chapters"][0]["start_s"] == 0
    assert 500 < payload["duration_s"] < 650
    assert all(c["end_s"] <= payload["duration_s"] for c in payload["chapters"])
    assert lines[3] == f"counts: {len(segments)}/10"

    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        row = conn.execute(
            "SELECT title, url, revision, reviewed_at FROM source WHERE ref = ?", (REF,)
        ).fetchone()
    assert row == (
        payload["title"],
        f"https://www.youtube.com/watch?v={VIDEO}",
        "generated:en",
        None,
    )
