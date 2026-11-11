"""Live Wikipedia fetch through ``cli.main`` with the real ``urlopen`` (manual step M1).

Marked ``live``: the default gate (``addopts = -m 'not live'``) never runs it.
Run it with ``uv run pytest -m live``. It sends one request (two if Wikipedia answers
429/503 once). A network or block error here (HTTP 429/503, DNS) is recorded as
``LIVE: BLOCKED-EXTERNAL (<class>)`` and retried later, not as a code failure.

Planning fact (plan §4.3, verified 2026-11-08): the article is organized as 11
"Ch.N: …" sections plus Reception, about 10k characters.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from checklist_care import cli, paths
from checklist_care.sources import wiki

pytestmark = pytest.mark.live

URL = "https://en.m.wikipedia.org/wiki/The_Happiness_Hypothesis#Reception"
REF = "wiki:en:The_Happiness_Hypothesis"
CANONICAL = "https://en.wikipedia.org/wiki/The_Happiness_Hypothesis"


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def test_live_fetch_of_the_book_article(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    assert cli.main(["--data-dir", str(data_dir), "init"]) == 0
    capsys.readouterr()

    code = cli.main(["--data-dir", str(data_dir), "fetch", URL])
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
    assert payload["kind"] == "wiki"
    assert payload["url"] == CANONICAL
    assert payload["title"] == "The Happiness Hypothesis"
    assert (payload["transcript"], payload["duration_s"], payload["chapters"]) == (None, None, [])
    assert payload["warnings"] == []

    text = payload["text"]
    sections = payload["sections"]
    chapters = [section for section in sections if section["heading"].startswith("Ch.")]
    assert len(chapters) == 11, [section["heading"] for section in sections]
    tail = {heading.casefold() for heading in wiki.TAIL_HEADINGS}
    assert not [s["heading"] for s in sections if s["heading"].casefold() in tail]
    assert 3_000 < len(text) < 30_000
    for section in sections:
        assert text[section["offset"] :].startswith("=" * section["level"] + " ")
    assert lines[3] == f"counts: {len(text)}/{len(sections)}"

    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        row = conn.execute(
            "SELECT kind, title, url, revision, reviewed_at FROM source WHERE ref = ?", (REF,)
        ).fetchone()
    assert row[:3] == ("wiki", "The Happiness Hypothesis", CANONICAL)
    assert row[3].isdigit() and int(row[3]) > 0  # lastrevid
    assert row[4] is None
