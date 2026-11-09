"""``checklist-care fetch '<youtube-url>'`` through the production entry point ``cli.main``.

The two network seams, ``youtube.make_transcript_client`` and ``youtube.run_ytdlp``,
are replaced by fakes; the fake client returns the transcript library's own
``FetchedTranscript`` so the attribute names are the real ones.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import pytest
import youtube_transcript_api as yta
from youtube_transcript_api import (
    FetchedTranscript,
    FetchedTranscriptSnippet,
    YouTubeTranscriptApi,
)

from checklist_care import cli, paths
from checklist_care.db import connection
from checklist_care.sources import youtube
from checklist_care.store import sources as source_store

REAL_RUN_YTDLP = youtube.run_ytdlp  # captured before any fixture replaces the seam

VIDEO = "LpeDuC2sQ-w"
REF = f"youtube:{VIDEO}"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-08T12:00:00Z"

# Synthetic, self-written transcript text (never third-party text in the repo).
SNIPPETS = [
    FetchedTranscriptSnippet(text="hello and welcome", start=0.0, duration=2.1),
    FetchedTranscriptSnippet(text="first idea, café \U0001f600", start=2.1, duration=3.4),
    FetchedTranscriptSnippet(text="second idea", start=61.0, duration=4.0),
    FetchedTranscriptSnippet(text="thanks for watching", start=555.2, duration=7.3),
]
TRANSCRIPT_END = math.ceil(555.2 + 7.3)  # 563

YTDLP_INFO: dict[str, Any] = {
    "id": VIDEO,
    "title": "Tiny Changes For A 10x Better Life",
    "channel": "Chill Dude Explains",
    "duration": 563,
    "chapters": [
        {"title": "The Accountability Text", "start_time": 0.0, "end_time": 60.7},
        {"title": "Your Alarm Is Lying To You", "start_time": 60.7, "end_time": 563.0},
    ],
    "formats": [{"format_id": "18"}],  # ignored
}

TRANSCRIPT_ERRORS: list[Callable[[str], Exception]] = [
    yta.TranscriptsDisabled,
    lambda video_id: yta.NoTranscriptFound(video_id, ["en"], None),
    yta.VideoUnavailable,
    yta.InvalidVideoId,
    yta.IpBlocked,
    yta.RequestBlocked,
    yta.AgeRestricted,
    yta.PoTokenRequired,
    yta.FailedToCreateConsentCookie,
]
TRANSCRIPT_ERROR_NAMES = [
    "TranscriptsDisabled",
    "NoTranscriptFound",
    "VideoUnavailable",
    "InvalidVideoId",
    "IpBlocked",
    "RequestBlocked",
    "AgeRestricted",
    "PoTokenRequired",
    "FailedToCreateConsentCookie",
]


@dataclass
class FakeTranscriptClient:
    snippets: list[FetchedTranscriptSnippet] = field(default_factory=lambda: list(SNIPPETS))
    is_generated: bool = True
    error: Exception | None = None
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def fetch(self, video_id: str, languages: Iterable[str] = ("en",)) -> FetchedTranscript:
        self.calls.append((video_id, tuple(languages)))
        if self.error is not None:
            raise self.error
        return FetchedTranscript(
            snippets=list(self.snippets),
            video_id=video_id,
            language="English" if not self.is_generated else "English (auto-generated)",
            language_code="en",
            is_generated=self.is_generated,
        )


@dataclass
class FakeYtDlp:
    info: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(YTDLP_INFO)))
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.info


@dataclass
class Seams:
    client: FakeTranscriptClient
    ytdlp: FakeYtDlp
    clients_made: list[FakeTranscriptClient]


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(connection, "utc_now", lambda: NOW)


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    made: list[FakeTranscriptClient] = []
    state = Seams(FakeTranscriptClient(), FakeYtDlp(), made)

    def make_client() -> FakeTranscriptClient:
        made.append(state.client)
        return state.client

    monkeypatch.setattr(youtube, "make_transcript_client", make_client)
    monkeypatch.setattr(youtube, "run_ytdlp", state.ytdlp)
    return state


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    target = tmp_path / "data"
    code, _, err = run(capsys, "--data-dir", str(target), "init")
    assert code == 0, err
    return target


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


def fetch(
    capsys: pytest.CaptureFixture[str], data_dir: Path, url: str
) -> tuple[int, str, list[str]]:
    return run(capsys, "--data-dir", str(data_dir), "fetch", url)


def packet_file(data_dir: Path) -> Path:
    return data_dir / "packets" / f"{paths.source_key(REF)}.json"


def source_rows(data_dir: Path) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM source WHERE kind <> 'manual' ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def mark_reviewed(data_dir: Path, ref: str, when: str = "2026-11-08T13:00:00Z") -> None:
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        conn.execute("UPDATE source SET reviewed_at = ? WHERE ref = ?", (when, ref))
        conn.commit()


def expected_packet(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "checklist-care.packet/1",
        "source_ref": REF,
        "kind": "youtube",
        "url": WATCH,
        "title": "Tiny Changes For A 10x Better Life",
        "channel": "Chill Dude Explains",
        "duration_s": 563,
        "fetched_at": NOW_TEXT,
        "transcript": {
            "kind": "generated",
            "language_code": "en",
            "segments": [
                {"start": s.start, "duration": s.duration, "text": s.text} for s in SNIPPETS
            ],
        },
        "chapters": [
            {"title": "The Accountability Text", "start_s": 0, "end_s": 60},
            {"title": "Your Alarm Is Lying To You", "start_s": 60, "end_s": 563},
        ],
        "text": None,
        "sections": [],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


# --- a valid URL ---------------------------------------------------------------------


def test_fetch_writes_the_packet_and_prints_its_sha256(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    code, out, err = fetch(capsys, data_dir, WATCH)

    assert code == 0, err
    target = packet_file(data_dir)
    raw = target.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert out.splitlines() == [
        f"packet: {target}",
        f"source_ref: {REF}",
        f"packet_sha256: {digest}",
        "counts: 4/2",
    ]
    assert err == [f"data_dir: {data_dir}"]
    assert json.loads(raw.decode("utf-8")) == expected_packet()
    assert list(json.loads(raw)) == list(expected_packet())  # §4.4 key order
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert "café \U0001f600" in raw.decode("utf-8")  # UTF-8, not \u-escaped

    assert seams.client.calls == [(VIDEO, ("en",))]
    assert seams.ytdlp.calls == [WATCH]  # canonical URL: no &list= playlist expansion
    assert source_rows(data_dir) == [
        {
            "id": 2,
            "kind": "youtube",
            "ref": REF,
            "title": "Tiny Changes For A 10x Better Life",
            "url": WATCH,
            "revision": "generated:en",
            "mode": "extract",
            "fetched_at": NOW_TEXT,
            "reviewed_at": None,
        }
    ]


def test_packet_name_is_the_source_key(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    code, out, _ = fetch(capsys, data_dir, f"https://youtu.be/{VIDEO}?t=5")

    assert code == 0
    assert sorted(p.name for p in (data_dir / "packets").iterdir()) == [
        f"{paths.source_key(REF)}.json"
    ]
    assert out.splitlines()[0] == f"packet: {packet_file(data_dir)}"


@pytest.mark.parametrize(
    "url",
    [
        f"{WATCH}&t=10s",
        f"https://www.youtube.com/watch?list=PLabcdefghijk&v={VIDEO}",
        f"https://m.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/shorts/{VIDEO}",
        VIDEO,
    ],
)
def test_every_url_form_fetches_the_canonical_video(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams, url: str
) -> None:
    code, out, _ = fetch(capsys, data_dir, url)

    assert code == 0
    assert f"source_ref: {REF}" in out.splitlines()
    assert seams.ytdlp.calls == [WATCH]
    assert json.loads(packet_file(data_dir).read_bytes())["url"] == WATCH


def test_manual_track_is_recorded_as_manual(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    seams.client.is_generated = False

    code, _, _ = fetch(capsys, data_dir, WATCH)

    assert code == 0
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert payload["transcript"]["kind"] == "manual"
    assert source_rows(data_dir)[0]["revision"] == "manual:en"


def test_fractional_ytdlp_times_floor_chapters_and_ceil_duration(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    seams.ytdlp.info["duration"] = 562.4

    code, _, _ = fetch(capsys, data_dir, WATCH)

    assert code == 0
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert payload["duration_s"] == 563
    assert [(c["start_s"], c["end_s"]) for c in payload["chapters"]] == [(0, 60), (60, 563)]


def test_refetch_of_an_unreviewed_source_updates_its_one_row(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fetch(capsys, data_dir, WATCH)[0] == 0
    later = datetime(2026, 11, 9, 8, 30, 0, tzinfo=UTC)
    monkeypatch.setattr(connection, "utc_now", lambda: later)
    seams.ytdlp.info["title"] = "Renamed"

    code, out, _ = fetch(capsys, data_dir, f"{WATCH}&t=10s")

    assert code == 0
    rows = source_rows(data_dir)
    assert len(rows) == 1
    assert (rows[0]["title"], rows[0]["fetched_at"], rows[0]["reviewed_at"]) == (
        "Renamed",
        "2026-11-09T08:30:00Z",
        None,
    )
    raw = packet_file(data_dir).read_bytes()
    assert json.loads(raw)["title"] == "Renamed"
    assert f"packet_sha256: {hashlib.sha256(raw).hexdigest()}" in out.splitlines()


# --- transcript failures ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("make_error", "name"), list(zip(TRANSCRIPT_ERRORS, TRANSCRIPT_ERROR_NAMES, strict=True))
)
def test_each_transcript_exception_exits_4_naming_the_class(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    make_error: Callable[[str], Exception],
    name: str,
) -> None:
    error = make_error(VIDEO)
    assert type(error).__name__ == name
    assert isinstance(error, yta.CouldNotRetrieveTranscript)
    seams.client.error = error

    code, out, err = fetch(capsys, data_dir, WATCH)

    assert code == 4
    assert out == ""
    assert err[0] == f"data_dir: {data_dir}"
    assert err[1] == (
        f"CC_E_FETCH: could not retrieve the transcript of {VIDEO}: {name}; "
        "paste the transcript text instead"
    )
    assert len(err) == 2
    assert not packet_file(data_dir).exists()
    assert source_rows(data_dir) == []
    assert seams.ytdlp.calls == []  # a fatal transcript error stops before yt-dlp


def test_a_network_error_from_the_transcript_client_exits_4(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    seams.client.error = ConnectionError("connection refused")

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 4
    assert err[1].startswith(f"CC_E_FETCH: could not retrieve the transcript of {VIDEO}: ")
    assert "ConnectionError" in err[1]
    assert not packet_file(data_dir).exists()


def assert_malformed_exit(
    code: int, out: str, err: list[str], data_dir: Path, seams: Seams, name: str
) -> str:
    """Exit 4 with ``CC_E_FETCH`` naming ``name``, and nothing written; returns the line."""

    assert code == 4, err
    assert out == ""
    assert len(err) == 2
    line = err[1]
    assert line.startswith(
        f"CC_E_FETCH: could not retrieve the transcript of {VIDEO}: {name} (malformed response"
    ), line
    assert line.endswith("; paste the transcript text instead"), line
    assert not packet_file(data_dir).exists()
    assert source_rows(data_dir) == []
    assert seams.ytdlp.calls == []
    return line


@pytest.mark.parametrize(
    "error",
    [
        ParseError("no element found: line 1, column 0"),
        KeyError("start"),
        IndexError("list index out of range"),
        ValueError("could not convert string to float: 'soon'"),
        TypeError("'NoneType' object is not iterable"),
        AttributeError("'NoneType' object has no attribute 'get'"),
        RecursionError("maximum recursion depth exceeded while decoding a JSON object"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_a_malformed_response_from_the_client_exits_4_naming_the_class(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams, error: Exception
) -> None:
    seams.client.error = error

    code, out, err = fetch(capsys, data_dir, WATCH)

    line = assert_malformed_exit(code, out, err, data_dir, seams, type(error).__name__)
    assert one_detail(str(error)) in line


def one_detail(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    ("bad", "name", "detail"),
    [
        (
            FetchedTranscriptSnippet(text="bad \ud800 text", start=2.1, duration=1.0),
            "UnicodeEncodeError",
            "surrogates not allowed",
        ),
        (
            FetchedTranscriptSnippet(text=None, start=2.1, duration=1.0),
            "TypeError",
            "snippet 1 text is NoneType, not text",
        ),
        (
            FetchedTranscriptSnippet(text="x", start="soon", duration=1.0),
            "ValueError",
            "could not convert string to float",
        ),
        (
            FetchedTranscriptSnippet(text="x", start=None, duration=1.0),
            "TypeError",
            "float() argument must be",
        ),
        (
            FetchedTranscriptSnippet(text="x", start=float("nan"), duration=1.0),
            "ValueError",
            "snippet 1 start nan is not a finite, non-negative number of seconds",
        ),
        (
            FetchedTranscriptSnippet(text="x", start=2.1, duration=float("inf")),
            "ValueError",
            "snippet 1 duration inf is not a finite",
        ),
        (
            FetchedTranscriptSnippet(text="x", start=-1.0, duration=1.0),
            "ValueError",
            "snippet 1 start -1.0 is not a finite",
        ),
    ],
    ids=["lone-surrogate", "text-none", "start-text", "start-none", "nan", "inf", "negative"],
)
def test_a_malformed_snippet_exits_4_naming_the_class(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    bad: FetchedTranscriptSnippet,
    name: str,
    detail: str,
) -> None:
    seams.client.snippets = [SNIPPETS[0], bad]

    code, out, err = fetch(capsys, data_dir, WATCH)

    line = assert_malformed_exit(code, out, err, data_dir, seams, name)
    assert detail in line


def test_a_bug_outside_the_response_is_not_reported_as_a_fetch_error(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    # Only the listed classes are mapped; anything else is still CC_E_INTERNAL.
    seams.client.error = RuntimeError("a bug")

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 9
    assert err[1] == "CC_E_INTERNAL: unexpected RuntimeError: a bug"
    assert not packet_file(data_dir).exists()


# The real transcript library over a fake HTTP session: what YouTube's responses do
# to the library's own parser, not what a fake client chooses to raise.

CAPTION_URL = f"https://www.youtube.com/api/timedtext?v={VIDEO}&lang=en"
WATCH_HTML = '<html><script>var c = {"INNERTUBE_API_KEY": "fake-key"};</script></html>'
PLAYER_OK: dict[str, Any] = {
    "playabilityStatus": {"status": "OK"},
    "captions": {
        "playerCaptionsTracklistRenderer": {
            "captionTracks": [
                {
                    "baseUrl": CAPTION_URL,
                    "name": {"runs": [{"text": "English (auto-generated)"}]},
                    "languageCode": "en",
                    "kind": "asr",
                }
            ]
        }
    },
}
CAPTIONS_OK = (
    '<transcript><text start="0.0" dur="2.1">hello &amp;amp; welcome</text>'
    '<text start="2.1" dur="3.4">second idea</text></transcript>'
)


@dataclass
class FakeResponse:
    text: str = ""
    payload: Any = None
    status_code: int = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


@dataclass
class FakeYouTubeSite:
    """Stands in for ``requests.Session``: the watch page, the player API, the captions."""

    caption_body: str = CAPTIONS_OK
    player: Any = field(default_factory=lambda: copy.deepcopy(PLAYER_OK))
    headers: dict[str, str] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)

    def get(self, url: str, **_: Any) -> FakeResponse:
        self.requested.append(url)
        if url == WATCH:
            return FakeResponse(text=WATCH_HTML)
        assert url == CAPTION_URL, url
        return FakeResponse(text=self.caption_body)

    def post(self, url: str, **_: Any) -> FakeResponse:
        self.requested.append(url)
        return FakeResponse(payload=self.player)


@pytest.fixture
def site(seams: Seams, monkeypatch: pytest.MonkeyPatch) -> FakeYouTubeSite:
    fake = FakeYouTubeSite()
    monkeypatch.setattr(
        youtube, "make_transcript_client", lambda: YouTubeTranscriptApi(http_client=fake)
    )
    return fake


def test_the_real_library_over_the_fake_site_writes_the_packet(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeYouTubeSite
) -> None:
    # Anchor: the fake site is good enough for the real library to succeed.
    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 0, err
    assert site.requested[-1] == CAPTION_URL
    transcript = json.loads(packet_file(data_dir).read_bytes())["transcript"]
    assert transcript == {
        "kind": "generated",
        "language_code": "en",
        "segments": [
            {"start": 0.0, "duration": 2.1, "text": "hello & welcome"},
            {"start": 2.1, "duration": 3.4, "text": "second idea"},
        ],
    }


def _player_with(**track: Any) -> dict[str, Any]:
    player = copy.deepcopy(PLAYER_OK)
    caption = player["captions"]["playerCaptionsTracklistRenderer"]["captionTracks"][0]
    caption.update(track)
    for key in [key for key, value in track.items() if value is None]:
        del caption[key]
    return player


@pytest.mark.parametrize(
    ("site_change", "name"),
    [
        pytest.param({"caption_body": ""}, "ParseError", id="empty-caption-body"),
        pytest.param({"caption_body": '<transcript><text start="0'}, "ParseError", id="truncated"),
        pytest.param(
            {"caption_body": "<html><body>Too many requests</body></html>"},
            "KeyError",
            id="rate-limit-page",
        ),
        pytest.param(
            {"caption_body": '<transcript><text start="soon">x</text></transcript>'},
            "ValueError",
            id="start-not-a-number",
        ),
        pytest.param(
            {"caption_body": '<transcript><text start="nan">x</text></transcript>'},
            "ValueError",
            id="start-nan",
        ),
        pytest.param(
            {
                "caption_body": '<!DOCTYPE t [<!ENTITY e "boom">]>'
                '<transcript><text start="0">&e;</text></transcript>'
            },
            "EntitiesForbidden",
            id="xml-entity",
        ),
        pytest.param({"player": {}}, "AttributeError", id="player-without-playability"),
        pytest.param(
            {"player": _player_with(name={"runs": []})}, "IndexError", id="track-name-empty"
        ),
        pytest.param(
            {"player": _player_with(languageCode=None)}, "KeyError", id="track-without-language"
        ),
    ],
)
def test_a_malformed_youtube_response_through_the_real_library_exits_4(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    site: FakeYouTubeSite,
    site_change: dict[str, Any],
    name: str,
) -> None:
    for attribute, value in site_change.items():
        setattr(site, attribute, value)

    code, out, err = fetch(capsys, data_dir, WATCH)

    assert_malformed_exit(code, out, err, data_dir, seams, name)


# --- yt-dlp failures ---------------------------------------------------------------------


def test_ytdlp_failure_still_writes_the_packet_without_chapters(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    seams.ytdlp.error = youtube.YtDlpError("exit 1: ERROR: [youtube] Sign in to confirm")

    code, out, err = fetch(capsys, data_dir, WATCH)

    assert code == 0, err
    raw = packet_file(data_dir).read_bytes()
    payload = json.loads(raw)
    assert payload["chapters"] == []
    assert payload["duration_s"] == TRANSCRIPT_END == 563
    assert payload["title"] is None
    assert payload["channel"] is None
    assert len(payload["warnings"]) == 1
    warning = payload["warnings"][0]
    assert warning.startswith("yt-dlp failed (exit 1: ERROR: [youtube] Sign in to confirm)")
    assert "duration_s comes from the transcript" in warning
    assert payload["transcript"] == expected_packet()["transcript"]
    assert out.splitlines() == [
        f"packet: {packet_file(data_dir)}",
        f"source_ref: {REF}",
        f"packet_sha256: {hashlib.sha256(raw).hexdigest()}",
        "counts: 4/0",
    ]
    assert err == [f"data_dir: {data_dir}", f"warning: {warning}"]
    assert source_rows(data_dir)[0]["title"] is None


def test_real_run_ytdlp_failure_reaches_the_packet_warning(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production run_ytdlp and runner, with a child that fails the way yt-dlp does.
    failing = "import sys; sys.stderr.write('ERROR: Video unavailable\\n'); sys.exit(1)"
    monkeypatch.setattr(youtube, "run_ytdlp", REAL_RUN_YTDLP)
    monkeypatch.setattr(youtube, "ytdlp_command", lambda url: [sys.executable, "-c", failing])

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 0, err
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert payload["chapters"] == []
    assert payload["duration_s"] == 563
    assert payload["warnings"][0].startswith("yt-dlp failed (exit 1: ERROR: Video unavailable)")


def test_ytdlp_without_duration_falls_back_to_the_transcript(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    del seams.ytdlp.info["duration"]
    seams.ytdlp.info["chapters"] = None

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 0
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert payload["duration_s"] == 563
    assert payload["chapters"] == []
    assert payload["warnings"] == [
        "the video has no chapters; split the transcript by content",
        "yt-dlp gave no duration; duration_s comes from the transcript",
    ]
    assert err[1:] == [f"warning: {w}" for w in payload["warnings"]]


def test_empty_transcript_writes_a_packet_with_a_warning(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    seams.client.snippets = []

    code, out, _ = fetch(capsys, data_dir, WATCH)

    assert code == 0
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert payload["transcript"]["segments"] == []
    assert payload["duration_s"] == 563  # yt-dlp still gave it
    assert payload["warnings"] == [
        "the transcript has no segments; paste the transcript text instead"
    ]
    assert "counts: 0/2" in out.splitlines()


def test_ytdlp_json_nested_too_deeply_still_writes_the_packet(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # json.loads raises RecursionError here, not JSONDecodeError.
    deep = "import sys; sys.stdout.write('[' * 100000 + ']' * 100000)"
    monkeypatch.setattr(youtube, "run_ytdlp", REAL_RUN_YTDLP)
    monkeypatch.setattr(youtube, "ytdlp_command", lambda url: [sys.executable, "-c", deep])

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 0, err
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert payload["chapters"] == []
    assert payload["duration_s"] == TRANSCRIPT_END
    assert payload["transcript"] == expected_packet()["transcript"]
    assert payload["warnings"][0].startswith("yt-dlp failed (output is not JSON: RecursionError: ")


def test_lone_surrogates_in_ytdlp_text_become_replacement_characters(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    # A "\ud800" JSON escape decodes to a lone surrogate, which UTF-8 cannot encode.
    seams.ytdlp.info = json.loads(
        '{"title": "Tiny \\ud800 Changes", "channel": "Chill \\udfff", "duration": 563,'
        ' "chapters": [{"title": "The \\ud83d Text", "start_time": 0, "end_time": 563}]}'
    )

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 0, err
    payload = json.loads(packet_file(data_dir).read_bytes().decode("utf-8"))
    assert payload["title"] == "Tiny � Changes"
    assert payload["channel"] == "Chill �"
    assert payload["chapters"] == [{"title": "The � Text", "start_s": 0, "end_s": 563}]
    assert payload["warnings"] == []
    assert source_rows(data_dir)[0]["title"] == "Tiny � Changes"


def test_ytdlp_numbers_beyond_float_range_degrade(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams
) -> None:
    huge = 10**400  # json.loads gives a Python int; float() of it overflows
    seams.ytdlp.info["duration"] = huge
    seams.ytdlp.info["chapters"][1]["end_time"] = huge

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 0, err
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert payload["duration_s"] == TRANSCRIPT_END
    assert payload["chapters"] == []
    assert len(payload["warnings"]) == 2
    assert payload["warnings"][0].startswith("yt-dlp gave malformed chapters")
    assert payload["warnings"][1] == "yt-dlp gave no duration; duration_s comes from the transcript"


# --- the packet and the source row change together -------------------------------------

LATER = datetime(2026, 11, 9, 8, 30, 0, tzinfo=UTC)


def packet_dir_names(data_dir: Path) -> list[str]:
    return sorted(p.name for p in (data_dir / "packets").iterdir())


def first_fetch_then_change(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Fetch once, then make the next fetch differ; returns the packet bytes and rows."""

    assert fetch(capsys, data_dir, WATCH)[0] == 0
    before = (packet_file(data_dir).read_bytes(), source_rows(data_dir))
    monkeypatch.setattr(connection, "utc_now", lambda: LATER)
    seams.ytdlp.info["title"] = "Renamed"
    return before


class CommitFails:
    """A real connection whose ``COMMIT`` fails, as on a full disk or an I/O error."""

    def __init__(self, conn: sqlite3.Connection, on_commit: Callable[[], None]) -> None:
        self._conn = conn
        self._on_commit = on_commit

    def execute(self, sql: str, *params: Any) -> sqlite3.Cursor:
        if sql.strip().upper() == "COMMIT":
            self._on_commit()
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def fail_every_commit(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> list[bytes | None]:
    """Make every later COMMIT fail; returns the packet file's bytes seen at each one."""

    seen: list[bytes | None] = []
    real_connect = connection.connect

    def at_commit() -> None:
        target = packet_file(data_dir)
        seen.append(target.read_bytes() if target.exists() else None)

    monkeypatch.setattr(
        connection, "connect", lambda path: CommitFails(real_connect(path), at_commit)
    )
    return seen


def test_a_failed_upsert_keeps_the_previous_packet_and_row(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_packet, before_rows = first_fetch_then_change(capsys, data_dir, seams, monkeypatch)

    def full_disk(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(source_store, "upsert_fetched", full_disk)

    code, out, err = fetch(capsys, data_dir, WATCH)

    assert code == 9
    assert out == ""
    assert err[1] == "CC_E_INTERNAL: unexpected OperationalError: database or disk is full"
    assert packet_file(data_dir).read_bytes() == before_packet
    assert source_rows(data_dir) == before_rows
    assert packet_dir_names(data_dir) == [packet_file(data_dir).name]


def test_a_failed_commit_puts_the_previous_packet_back(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_packet, before_rows = first_fetch_then_change(capsys, data_dir, seams, monkeypatch)
    seen_at_commit = fail_every_commit(data_dir, monkeypatch)

    code, out, err = fetch(capsys, data_dir, WATCH)

    assert code == 9
    assert out == ""
    assert err[1] == "CC_E_INTERNAL: unexpected OperationalError: disk I/O error"
    # The new packet was in place when COMMIT ran (so this is not vacuous) ...
    assert len(seen_at_commit) == 1 and seen_at_commit[0] is not None
    assert json.loads(seen_at_commit[0])["title"] == "Renamed"
    # ... and the failed COMMIT put the old one back, matching the unchanged row.
    assert packet_file(data_dir).read_bytes() == before_packet
    assert source_rows(data_dir) == before_rows
    assert packet_dir_names(data_dir) == [packet_file(data_dir).name]


def test_a_failed_commit_on_the_first_fetch_leaves_no_packet(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_at_commit = fail_every_commit(data_dir, monkeypatch)

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 9
    assert err[1] == "CC_E_INTERNAL: unexpected OperationalError: disk I/O error"
    assert len(seen_at_commit) == 1 and seen_at_commit[0] is not None  # it was installed
    assert not packet_file(data_dir).exists()
    assert source_rows(data_dir) == []
    assert packet_dir_names(data_dir) == []


def test_a_packet_that_cannot_be_replaced_leaves_the_row_unchanged(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On Windows a reader holding the old packet open makes the rename over it fail.
    before_packet, before_rows = first_fetch_then_change(capsys, data_dir, seams, monkeypatch)
    target = packet_file(data_dir)
    real_replace = os.replace
    refused: list[str] = []

    def replace(source: Any, destination: Any) -> None:
        if Path(destination) == target:
            refused.append(Path(source).name)
            raise PermissionError(13, "the file is in use", str(destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)

    code, _, err = fetch(capsys, data_dir, WATCH)

    assert code == 9
    assert err[1].startswith("CC_E_INTERNAL: unexpected PermissionError: ")
    assert len(refused) == 1 and refused[0].endswith(".tmp")  # the install was attempted
    assert target.read_bytes() == before_packet
    assert source_rows(data_dir) == before_rows
    assert packet_dir_names(data_dir) == [target.name]


# --- reviewed sources ----------------------------------------------------------------


@pytest.mark.parametrize("url", [WATCH, f"{WATCH}&t=10s", f"https://youtu.be/{VIDEO}"])
def test_a_reviewed_source_exits_3_for_every_url_form(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams, url: str
) -> None:
    assert fetch(capsys, data_dir, WATCH)[0] == 0
    mark_reviewed(data_dir, REF)
    before_rows = source_rows(data_dir)
    before_packet = packet_file(data_dir).read_bytes()
    seams.client.calls.clear()
    seams.ytdlp.calls.clear()
    seams.clients_made.clear()

    code, out, err = fetch(capsys, data_dir, url)

    assert code == 3
    assert out == ""
    assert err[1].startswith(f"CC_ALREADY_REVIEWED: {REF} was already reviewed at ")
    assert len(err) == 2
    assert source_rows(data_dir) == before_rows
    assert packet_file(data_dir).read_bytes() == before_packet
    assert seams.clients_made == [] and seams.ytdlp.calls == []  # no network at all


def test_a_source_reviewed_during_the_fetch_is_refused_and_not_overwritten(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fetch(capsys, data_dir, WATCH)[0] == 0
    before_packet = packet_file(data_dir).read_bytes()

    def review_while_running(url: str) -> dict[str, Any]:
        mark_reviewed(data_dir, REF)  # `propose` lands while yt-dlp runs
        return {"title": "Changed meanwhile", "duration": 563, "chapters": []}

    monkeypatch.setattr(youtube, "run_ytdlp", review_while_running)

    code, out, err = fetch(capsys, data_dir, f"{WATCH}&t=10s")

    assert code == 3
    assert out == ""
    assert err[1].startswith("CC_ALREADY_REVIEWED: ")
    assert packet_file(data_dir).read_bytes() == before_packet
    assert source_rows(data_dir)[0]["title"] == "Tiny Changes For A 10x Better Life"


# --- input and setup errors ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@ChillDudeExplains",
        "https://vimeo.com/123456789",
        "https://en.wikipedia.org/wiki/The_Happiness_Hypothesis",
    ],
)
def test_an_unsupported_url_exits_4_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path, seams: Seams, url: str
) -> None:
    code, out, err = fetch(capsys, data_dir, url)

    assert code == 4
    assert out == ""
    assert err[1].startswith("CC_E_FETCH: not a supported YouTube video URL")
    assert list((data_dir / "packets").iterdir()) == []
    assert source_rows(data_dir) == []
    assert seams.clients_made == []


def test_fetch_without_a_database_exits_2_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], seams: Seams
) -> None:
    target = tmp_path / "data"

    code, out, err = fetch(capsys, target, WATCH)

    assert code == 2
    assert out == ""
    assert err[1].startswith("CC_E_CONFIG: no database at ")
    assert not target.exists()
    assert seams.clients_made == []


def test_fetch_help_says_to_single_quote_the_url(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["fetch", "--help"])
    assert code == 0
    assert "Put the URL in single quotes" in " ".join(capsys.readouterr().out.split())
