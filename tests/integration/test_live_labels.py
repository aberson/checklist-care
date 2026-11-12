"""The committed labels against the live YouTube and Wikipedia metadata (manual step M1).

Marked ``live``: the default gate (``addopts = -m 'not live'``) never runs it. Run it
with ``uv run pytest -m live``. A network or block error here is recorded as
``LIVE: BLOCKED-EXTERNAL (<class>)`` and retried later, not as a code failure.

The gate already aborts when a label title is not byte-equal to a fixture packet's
chapter title (``tests/integration/test_cli_eval.py``). These tests close the loop for
every committed labels file: every chapter title a labels file names appears byte for
byte among the live chapters of its video, and each fixture packet the gate scores
carries the live chapter titles, timings and duration, all read through the production
``run_ytdlp`` and ``read_metadata``. Each Wikipedia source a labels file names is the
live article's canonical ref, and the live article holds the 11 "Ch.N" headings INV-2
counts, read through the production ``wiki.fetch_article`` and ``score.chapter_headings``.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from checklist_care.limits import SOURCE_KIND_WIKI
from checklist_care.scoring import score
from checklist_care.sources import packet as packet_module
from checklist_care.sources import wiki, youtube

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
LABEL_FILES = sorted((ROOT / "eval" / "labels").glob("*.json"))
FIXTURES = ROOT / "tests" / "fixtures"
FIXTURE_PACKETS = {
    youtube.source_ref("RPK-xw3SrVw"): FIXTURES / "packet_RPK-xw3SrVw_synthetic.json",
    youtube.source_ref("LpeDuC2sQ-w"): FIXTURES / "packet_youtube_synthetic.json",
    youtube.source_ref("rgntlzYeaRM"): FIXTURES / "packet_rgntlzYeaRM_synthetic.json",
    youtube.source_ref("VbGMWtr0AJI"): FIXTURES / "packet_VbGMWtr0AJI_synthetic.json",
}
YOUTUBE_PREFIX = youtube.source_ref("")
"""What ``youtube.source_ref`` puts before every video id."""


@functools.cache
def live_metadata(ref: str) -> youtube.VideoMetadata:
    """The video's live title, duration and chapters (one yt-dlp call per video)."""

    video = ref.removeprefix(YOUTUBE_PREFIX)
    metadata = youtube.read_metadata(youtube.run_ytdlp(youtube.watch_url(video)))
    assert metadata.warnings == (), metadata.warnings
    return metadata


WIKI_PREFIX = f"{SOURCE_KIND_WIKI}:"
FETCHED_AT = "2026-11-12T00:00:00Z"


def test_there_is_a_labels_file_to_check() -> None:
    names = ("in_sample.json", "out_of_sample.json", "investigate_happiness_hypothesis.json")

    assert {ROOT / "eval" / "labels" / name for name in names} <= set(LABEL_FILES)


def wiki_refs(labels_file: Path) -> list[str]:
    return [
        source.ref
        for source in score.load_labels(labels_file).sources
        if source.ref.startswith(WIKI_PREFIX)
    ]


@functools.cache
def live_article(ref: str) -> packet_module.Packet:
    """The live article of a ``wiki:<lang>:<Title>`` ref (one request per article)."""

    _, lang, title = ref.split(":", 2)
    target = wiki.WikiTitle(lang=lang, title=title.replace("_", " "))
    return wiki.fetch_article(target, fetched_at=FETCHED_AT).packet


@pytest.mark.parametrize("labels_file", LABEL_FILES, ids=lambda path: path.name)
def test_every_labelled_article_is_live_with_the_chapter_headings_inv2_counts(
    labels_file: Path,
) -> None:
    refs = wiki_refs(labels_file)
    if not refs:
        pytest.skip(f"{labels_file.name} names no Wikipedia source")
    for ref in refs:
        article = live_article(ref)
        assert article.source_ref == ref  # the label holds the canonical ref
        assert len(score.chapter_headings(article)) == score.CHAPTER_HEADINGS


@pytest.mark.parametrize("labels_file", LABEL_FILES, ids=lambda path: path.name)
def test_every_label_title_appears_byte_exact_in_the_live_chapters(labels_file: Path) -> None:
    labels = score.load_labels(labels_file)
    titles: dict[str, list[str]] = {}
    for chapter_ref in labels.chapter_refs():
        titles.setdefault(chapter_ref.ref, []).append(chapter_ref.title)

    missing = {
        ref: [title for title in wanted if title not in live_titles]
        for ref, wanted in titles.items()
        if ref.startswith(YOUTUBE_PREFIX)
        for live_titles in [[chapter.title for chapter in live_metadata(ref).chapters]]
    }

    assert {ref: found for ref, found in missing.items() if found} == {}


@pytest.mark.parametrize("ref", sorted(FIXTURE_PACKETS))
def test_the_fixture_packet_carries_the_live_chapters_and_duration(ref: str) -> None:
    fixture = packet_module.read_packet(FIXTURE_PACKETS[ref])
    live = live_metadata(ref)

    assert fixture.source_ref == ref
    assert fixture.chapters == live.chapters
    assert fixture.duration_s == live.duration_s
    assert fixture.title == live.title
