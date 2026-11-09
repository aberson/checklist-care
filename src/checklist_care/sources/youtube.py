"""YouTube sources (plan §4.3): URL → video id, transcript, chapters, packet.

Two seams reach the network, and tests replace them:

* :func:`make_transcript_client` returns a ``YouTubeTranscriptApi()``;
* :func:`run_ytdlp` runs ``python -m yt_dlp --dump-single-json`` on one URL and
  returns its JSON object, or raises :class:`YtDlpError`.

:func:`fetch_packet` looks both up as module globals at call time, so
``monkeypatch.setattr(youtube, "run_ytdlp", fake)`` reaches it. A transcript
failure is fatal (exit 4, ``CC_E_FETCH``); a yt-dlp failure is not: the packet
keeps the transcript, gets ``chapters: []`` and a warning, and ``duration_s``
comes from the transcript.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import ParseError

from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException

from checklist_care import USER_AGENT
from checklist_care.errors import FetchError, one_line
from checklist_care.limits import (
    SOURCE_KIND_YOUTUBE,
    TRANSCRIPT_KIND_GENERATED,
    TRANSCRIPT_KIND_MANUAL,
)
from checklist_care.processes import NEW_SESSION, NO_WINDOW, kill_tree
from checklist_care.sources.packet import Chapter, Packet, Segment, Transcript

VIDEO_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{11}")
"""A video id (plan §3.2), matched with ``fullmatch``."""

WATCH_URL: Final = "https://www.youtube.com/watch?v={video_id}"

WATCH_HOSTS: Final = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
SHORT_LINK_HOSTS: Final = frozenset({"youtu.be", "www.youtu.be"})
ID_PATH_PREFIXES: Final = frozenset({"shorts", "embed", "live"})
"""``/<prefix>/<id>`` paths on a watch host."""

TRANSCRIPT_LANGUAGES: Final = ("en",)
PASTE_HINT: Final = "paste the transcript text instead"

YTDLP_TIMEOUT_S: Final = 60.0

_STDERR_TAIL_MAX: Final = 200
_INPUT_ECHO_MAX: Final = 200
_DETAIL_MAX: Final = 200


class YtDlpError(Exception):
    """yt-dlp could not give the video's metadata; the fetch continues without it."""


# --- URL → id ------------------------------------------------------------------------


def _unsupported(raw: str) -> FetchError:
    shown = one_line(raw)[:_INPUT_ECHO_MAX]
    return FetchError(
        f"not a supported YouTube video URL: '{shown}'. Give a youtube.com/watch?v=, youtu.be/, "
        "/shorts/, /embed/ or /live/ link, or the 11-character video id; channel and playlist "
        "links name no video"
    )


def parse_video_id(raw: str) -> str:
    """The 11-character video id of a YouTube URL or bare id.

    Accepts ``watch?v=ID`` with other parameters in any order, ``youtu.be/ID``,
    ``/shorts/ID``, ``/embed/ID`` and ``/live/ID`` on the www, ``m.``, ``music.``
    and ``youtube-nocookie.com`` hosts, with or without a scheme. Anything else,
    such as a channel URL or another host, raises :class:`FetchError` (exit 4).
    """

    text = raw.strip()
    if VIDEO_ID_PATTERN.fullmatch(text):
        return text
    url = text if "://" in text else f"https://{text}"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
    except ValueError as error:
        raise _unsupported(raw) from error
    if parts.scheme.lower() not in ("http", "https"):
        raise _unsupported(raw)
    segments = [segment for segment in parts.path.split("/") if segment]
    candidate: str | None = None
    if host in SHORT_LINK_HOSTS:
        if len(segments) == 1:
            candidate = segments[0]
    elif host in WATCH_HOSTS:
        if segments == ["watch"]:
            values = parse_qs(parts.query).get("v")
            candidate = values[0] if values else None
        elif len(segments) == 2 and segments[0] in ID_PATH_PREFIXES:
            candidate = segments[1]
    if candidate is None or not VIDEO_ID_PATTERN.fullmatch(candidate):
        raise _unsupported(raw)
    return candidate


def source_ref(video_id: str) -> str:
    """``youtube:<id>``, the source's dedupe key (plan §3.2)."""

    return f"{SOURCE_KIND_YOUTUBE}:{video_id}"


def watch_url(video_id: str) -> str:
    """The canonical ``https://www.youtube.com/watch?v=<id>`` of a video."""

    return WATCH_URL.format(video_id=video_id)


# --- transcript seam -----------------------------------------------------------------


TRANSCRIPT_ERRORS: Final = (YouTubeTranscriptApiException, OSError)
"""The library's own failures, and network errors (``requests`` errors subclass ``OSError``)."""

MALFORMED_RESPONSE_ERRORS: Final = (
    ParseError,
    LookupError,
    ValueError,
    TypeError,
    AttributeError,
    RecursionError,
)
"""What the library raises on a response it cannot read, rather than its own exceptions.

An empty or truncated caption body (a rate-limit or consent page) fails
``ElementTree.fromstring`` with ``ParseError``; a cue without ``start`` or with a
non-number gives ``KeyError`` or ``ValueError``; player JSON of another shape
gives ``KeyError``, ``IndexError``, ``TypeError`` or ``AttributeError`` (it calls
``.get`` on whatever it found); deeply nested JSON gives ``RecursionError``.
These are caught only around the library call and the conversion of what it
returned, so a bug elsewhere still surfaces as ``CC_E_INTERNAL``.
"""


def make_transcript_client() -> YouTubeTranscriptApi:
    """Seam: the transcript client (``YouTubeTranscriptApi``, 1.x instance API)."""

    return YouTubeTranscriptApi()


def _shown(text: str) -> str:
    """Untrusted error text as one bounded line without control characters."""

    printable = "".join(char for char in one_line(text) if char.isprintable())
    return printable[:_DETAIL_MAX]


def _utf8_text(value: Any, what: str) -> str:
    """``value`` if it is a string that UTF-8 can encode, else ``TypeError`` or
    ``UnicodeEncodeError``. A lone surrogate (only a ``\\ud800`` JSON escape makes
    one) would otherwise fail the packet write and the database bind."""

    if not isinstance(value, str):
        raise TypeError(f"{what} is {type(value).__name__}, not text")
    value.encode("utf-8")
    return value


def _seconds_value(value: Any, what: str) -> float:
    """A snippet time as a finite, non-negative float, else ``ValueError``."""

    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{what} {seconds!r} is not a finite, non-negative number of seconds")
    return seconds


def _segment(snippet: Any, index: int) -> Segment:
    return Segment(
        start=_seconds_value(snippet.start, f"snippet {index} start"),
        duration=_seconds_value(snippet.duration, f"snippet {index} duration"),
        text=_utf8_text(snippet.text, f"snippet {index} text"),
    )


def fetch_transcript(video_id: str) -> Transcript:
    """The video's English transcript; a manual track wins over a generated one.

    Every transcript-library exception and network ``OSError`` raises
    :class:`FetchError` naming the class. So does a response that the library or
    the snippet conversion cannot read (:data:`MALFORMED_RESPONSE_ERRORS`), with a
    bounded detail; a snippet time that is not a finite, non-negative number, or
    text that UTF-8 cannot encode, counts as malformed too.
    """

    client = make_transcript_client()
    try:
        fetched = client.fetch(video_id, languages=TRANSCRIPT_LANGUAGES)
        kind = TRANSCRIPT_KIND_GENERATED if fetched.is_generated else TRANSCRIPT_KIND_MANUAL
        language_code = _utf8_text(fetched.language_code, "language_code")
        segments = tuple(_segment(snippet, i) for i, snippet in enumerate(fetched.snippets))
    except TRANSCRIPT_ERRORS as error:
        raise FetchError(
            f"could not retrieve the transcript of {video_id}: {type(error).__name__}; {PASTE_HINT}"
        ) from error
    except MALFORMED_RESPONSE_ERRORS as error:
        detail = _shown(str(error))
        raise FetchError(
            f"could not retrieve the transcript of {video_id}: {type(error).__name__} "
            f"(malformed response{': ' + detail if detail else ''}); {PASTE_HINT}"
        ) from error
    return Transcript(kind=kind, language_code=language_code, segments=segments)


def revision(packet: Packet) -> str | None:
    """``source.revision`` of a YouTube packet: ``<generated|manual>:<language_code>``."""

    if packet.transcript is None:
        return None
    return f"{packet.transcript.kind}:{packet.transcript.language_code}"


def transcript_duration(segments: Sequence[Segment]) -> int | None:
    """``ceil(last.start + last.duration)``, or None without segments."""

    if not segments:
        return None
    last = segments[-1]
    return math.ceil(last.start + last.duration)


# --- yt-dlp seam ---------------------------------------------------------------------


def ytdlp_command(url: str) -> list[str]:
    """The exact yt-dlp command line of plan §4.3."""

    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--user-agent",
        USER_AGENT,
        url,
    ]


def run_ytdlp(url: str) -> dict[str, Any]:
    """Seam: yt-dlp's metadata JSON for ``url`` (60 s limit, tree killed on timeout)."""

    return run_json_command(ytdlp_command(url), timeout=YTDLP_TIMEOUT_S)


def _stderr_tail(stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if line.strip()]
    return one_line(lines[-1])[:_STDERR_TAIL_MAX] if lines else "no error output"


def run_json_command(command: Sequence[str], *, timeout: float) -> dict[str, Any]:
    """Run ``command`` and parse its stdout as one JSON object.

    Output is UTF-8 (``PYTHONIOENCODING=utf-8`` is set for the child). The pipes
    are read as bytes and decoded here, strictly for stdout: in text mode a bad
    byte would kill subprocess's reader thread on Windows instead of raising.
    A start failure, a timeout, a non-zero exit or output that is not a UTF-8
    JSON object (including JSON nested too deeply to parse, ``RecursionError``)
    raises :class:`YtDlpError`; on a timeout or interrupt the whole process tree
    is killed first.
    """

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=NO_WINDOW,
            start_new_session=NEW_SESSION,
        )
    except OSError as error:
        raise YtDlpError(f"cannot start yt-dlp: {error}") from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        raise YtDlpError(f"no result within {timeout:g} s; the process tree was killed") from None
    except BaseException:
        kill_tree(process)
        raise
    if process.returncode != 0:
        tail = _stderr_tail(stderr.decode("utf-8", errors="replace"))
        raise YtDlpError(f"exit {process.returncode}: {tail}")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise YtDlpError(f"output is not UTF-8: {error}") from error
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as error:  # JSONDecodeError is a ValueError
        detail = one_line(str(error))[:_STDERR_TAIL_MAX]
        raise YtDlpError(f"output is not JSON: {type(error).__name__}: {detail}") from error
    if not isinstance(data, dict):
        raise YtDlpError("output is not a JSON object")
    return data


# --- metadata → packet ---------------------------------------------------------------


@dataclass(frozen=True)
class VideoMetadata:
    """What the packet takes from yt-dlp's JSON."""

    title: str | None
    channel: str | None
    duration_s: int | None
    chapters: tuple[Chapter, ...]
    warnings: tuple[str, ...]


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except OverflowError:  # an integer beyond float range
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _text(value: Any) -> str | None:
    """A non-blank string, else None. Each lone surrogate (from a ``\\ud800`` JSON
    escape, which UTF-8 cannot encode) becomes U+FFFD: metadata is optional, so a
    bad character degrades instead of failing the packet write."""

    if not isinstance(value, str) or not value.strip():
        return None
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


_NO_CHAPTERS: Final = "the video has no chapters; split the transcript by content"
_BAD_CHAPTERS: Final = (
    "yt-dlp gave malformed chapters (each needs a title, start_time and end_time); "
    "ignored them all; split the transcript by content"
)


def _chapter(item: Any) -> Chapter | None:
    if not isinstance(item, Mapping):
        return None
    title = _text(item.get("title"))
    start = _seconds(item.get("start_time"))
    end = _seconds(item.get("end_time"))
    if title is None or start is None or end is None or end < start:
        return None
    return Chapter(title=title, start_s=math.floor(start), end_s=math.floor(end))


def _chapters(raw: Any) -> tuple[tuple[Chapter, ...], str | None]:
    """The chapters, or none plus a warning. One malformed entry drops them all:
    a partial chapter list would mislead the per-chapter split."""

    if raw is None or raw == []:
        return (), _NO_CHAPTERS
    if not isinstance(raw, list):
        return (), _BAD_CHAPTERS
    chapters = [_chapter(item) for item in raw]
    if any(chapter is None for chapter in chapters):
        return (), _BAD_CHAPTERS
    return tuple(chapter for chapter in chapters if chapter is not None), None


def read_metadata(info: Mapping[str, Any]) -> VideoMetadata:
    """Title, channel, duration and chapters from yt-dlp's JSON.

    Chapter times are ``floor()``ed to whole seconds. ``duration`` is rounded up,
    so every chapter and snippet start stays below it; a missing or non-positive
    duration gives None.
    """

    chapters, warning = _chapters(info.get("chapters"))
    duration = _seconds(info.get("duration"))
    return VideoMetadata(
        title=_text(info.get("title")),
        channel=_text(info.get("channel")),
        duration_s=math.ceil(duration) if duration else None,
        chapters=chapters,
        warnings=(warning,) if warning else (),
    )


def fetch_packet(video_id: str, *, fetched_at: str) -> Packet:
    """Fetch the transcript (fatal on failure) and the metadata (optional) of a video."""

    transcript = fetch_transcript(video_id)
    url = watch_url(video_id)
    warnings: list[str] = []
    try:
        info = run_ytdlp(url)
    except YtDlpError as error:
        metadata = VideoMetadata(None, None, None, (), ())
        warnings.append(
            f"yt-dlp failed ({one_line(str(error))}): no chapters, title or channel, and "
            "duration_s comes from the transcript; split the transcript by content"
        )
    else:
        metadata = read_metadata(info)
        warnings.extend(metadata.warnings)
        if metadata.duration_s is None:
            warnings.append("yt-dlp gave no duration; duration_s comes from the transcript")
    duration_s = metadata.duration_s
    if duration_s is None:
        duration_s = transcript_duration(transcript.segments)
    if not transcript.segments:
        warnings.append(f"the transcript has no segments; {PASTE_HINT}")
    return Packet(
        source_ref=source_ref(video_id),
        kind=SOURCE_KIND_YOUTUBE,
        url=url,
        title=metadata.title,
        channel=metadata.channel,
        duration_s=duration_s,
        fetched_at=fetched_at,
        transcript=transcript,
        chapters=metadata.chapters,
        text=None,
        sections=(),
        warnings=tuple(warnings),
    )
