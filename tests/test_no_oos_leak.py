"""No out-of-sample video id or chapter title reaches the skill (plan §4.8, D17, Step 16).

The out-of-sample results are the real signal only while the skill has never seen those
videos. So no out-of-sample video id and no chapter title that the committed
``eval/labels/out_of_sample.json`` names (read by the production labels reader,
``score.load_labels``) may appear in any file under ``skills/``, ``.claude/skills/`` or
``.agents/skills/``, or in ``AGENTS.md``, which both hosts load. Those ids and titles are
the only needles (the plan's scope): a chapter title the labels file does not name is not
checked, so a short generic title cannot trip the check on ordinary skill prose.

A title matches loosely (casefolded, typographic apostrophes made ASCII, white space
collapsed), so a retyped title still counts; a video id matches as written. Every file is
read as bytes and decoded leniently, so a binary or non-UTF-8 file is still scanned and
never stops the check. The checker also runs on a planted tree, so a check that silently
matches nothing fails instead of passing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from checklist_care import context
from checklist_care.scoring import score
from checklist_care.sources import youtube

ROOT = Path(__file__).resolve().parents[1]
OOS_LABELS = ROOT / "eval" / "labels" / "out_of_sample.json"
SKILL_ROOTS = ("skills", ".claude/skills", ".agents/skills")
INSTRUCTION_FILES = ("AGENTS.md",)
YOUTUBE_PREFIX = youtube.source_ref("")
"""What ``youtube.source_ref`` puts before every video id."""


def out_of_sample_videos() -> list[str]:
    """The ids of the labelled out-of-sample videos: the graded YouTube sources (the
    labels' reference video is in-sample, and the anchor is no video)."""

    labels = score.load_labels(OOS_LABELS)
    return [
        source.ref.removeprefix(YOUTUBE_PREFIX)
        for source in labels.sources
        if source.graded and source.ref.startswith(YOUTUBE_PREFIX)
    ]


def out_of_sample_titles() -> list[str]:
    """Every chapter title the labels name, once each, in file order."""

    titles = [chapter.title for chapter in score.load_labels(OOS_LABELS).chapter_refs()]
    return list(dict.fromkeys(titles))


def normalized(text: str) -> str:
    """Casefolded, typographic apostrophes made ASCII, white space collapsed."""

    ascii_quotes = text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").replace(
        "\N{LEFT SINGLE QUOTATION MARK}", "'"
    )
    return " ".join(ascii_quotes.casefold().split())


def leaked(text: str, videos: Iterable[str], titles: Iterable[str]) -> list[str]:
    haystack = normalized(text)
    found = [video for video in videos if video in text]
    return found + [title for title in titles if normalized(title) in haystack]


def scanned_files(root: Path) -> list[Path]:
    """Every file under the skill roots of ``root``, and its instruction files."""

    files = [
        path
        for skill_root in SKILL_ROOTS
        if (root / skill_root).is_dir()
        for path in sorted((root / skill_root).rglob("*"))
        if path.is_file()
    ]
    return files + [root / name for name in INSTRUCTION_FILES if (root / name).is_file()]


def file_text(path: Path) -> str:
    """The file's bytes as UTF-8, each byte that is not UTF-8 replaced: a binary or
    non-UTF-8 file is still scanned (its ids and ASCII titles still match), never refused."""

    return path.read_bytes().decode("utf-8", errors="replace")


def leaks(root: Path, videos: list[str], titles: list[str]) -> dict[str, list[str]]:
    """Each scanned file of ``root`` that holds an id or a title, with what it holds."""

    found = {
        path.relative_to(root).as_posix(): leaked(file_text(path), videos, titles)
        for path in scanned_files(root)
    }
    return {path: terms for path, terms in found.items() if terms}


def raw_labels() -> tuple[set[str], set[str]]:
    """The labels file's video ids and chapter titles, walked from its raw JSON without the
    production reader: the graded YouTube sources' ids; the titles of the sources'
    chapters, of both sides of each chapter pair and of the soft groups."""

    document = json.loads(OOS_LABELS.read_bytes().decode("utf-8"))
    assert set(document) <= {
        "schema",
        "claim",
        "sources",
        "duplicate_pairs",
        "not_duplicate_pairs",
        "soft",
        "criteria",
    }, "a key this walk does not read could hold a title"
    videos = {
        source["ref"].removeprefix(YOUTUBE_PREFIX)
        for source in document["sources"]
        if source.get("ref", "").startswith(YOUTUBE_PREFIX) and not source.get("reference")
    }
    titles = {
        chapter["title"] for source in document["sources"] for chapter in source.get("chapters", [])
    }
    for key in ("duplicate_pairs", "not_duplicate_pairs"):
        for pair in document.get(key, []):
            titles |= {pair["a"][1], pair["b"][1]}
    for group in document.get("soft", []):
        titles |= set(group["titles"])
    return videos, titles


def test_the_labels_name_the_two_out_of_sample_videos_and_their_titles() -> None:
    videos = out_of_sample_videos()
    titles = out_of_sample_titles()

    assert sorted(videos) == ["VbGMWtr0AJI", "rgntlzYeaRM"]
    assert {"Tracing Maps", "Sitting All Day", "Using Your GPS", "Poor Oral Hygiene"} <= set(titles)


def test_the_needles_are_exactly_the_ids_and_titles_of_the_labels_file() -> None:
    raw_videos, raw_titles = raw_labels()

    assert set(out_of_sample_videos()) == raw_videos
    assert set(out_of_sample_titles()) == raw_titles


def test_every_id_and_title_of_the_labels_file_is_found_when_planted(tmp_path: Path) -> None:
    raw_videos, raw_titles = raw_labels()
    planted = tmp_path / "skills" / "checklist-ingest" / "core.md"
    planted.parent.mkdir(parents=True)
    planted.write_text("\n".join(sorted(raw_videos | raw_titles)), encoding="utf-8")

    found = leaks(tmp_path, out_of_sample_videos(), out_of_sample_titles())

    assert set(found) == {"skills/checklist-ingest/core.md"}
    assert set(found["skills/checklist-ingest/core.md"]) == raw_videos | raw_titles


def test_the_check_scans_the_skill_and_the_instruction_files() -> None:
    files = set(scanned_files(ROOT))

    assert {ROOT / relative for relative in context.SKILL_FILES} <= files
    assert ROOT / "AGENTS.md" in files


def test_the_leak_check_matches_loosely_and_ids_exactly() -> None:
    sample = "Try tracing  MAPS, or sitting all\nday; see vbgmwtr0ajI and rgntlzYeaRM."

    assert leaked(sample, ["VbGMWtr0AJI", "rgntlzYeaRM"], ["Tracing Maps", "Sitting All Day"]) == [
        "rgntlzYeaRM",
        "Tracing Maps",
        "Sitting All Day",
    ]
    assert leaked("Don\N{RIGHT SINGLE QUOTATION MARK}t carry", [], ["Don't Carry"]) == [
        "Don't Carry"
    ]


def test_a_planted_id_or_title_in_any_scanned_place_is_found(tmp_path: Path) -> None:
    videos = out_of_sample_videos()
    titles = out_of_sample_titles()
    planted = {
        "skills/checklist-ingest/core.md": f"Example: {titles[0]}.",
        ".claude/skills/checklist-ingest/SKILL.md": f"See {videos[0]}.",
        ".agents/skills/checklist-ingest/SKILL.md": f"Example: {titles[-1].upper()}.",
        "AGENTS.md": f"Run it on {videos[-1]}.",
        "README.md": f"{titles[0]} is outside the scanned files.",
    }
    for relative, text in planted.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")

    found = leaks(tmp_path, videos, titles)

    assert found == {
        "skills/checklist-ingest/core.md": [titles[0]],
        ".claude/skills/checklist-ingest/SKILL.md": [videos[0]],
        ".agents/skills/checklist-ingest/SKILL.md": [titles[-1]],
        "AGENTS.md": [videos[-1]],
    }


def test_a_binary_or_non_utf8_file_is_scanned_without_crashing(tmp_path: Path) -> None:
    videos = out_of_sample_videos()
    titles = out_of_sample_titles()
    cp1252 = f"\N{LEFT DOUBLE QUOTATION MARK}{titles[0]}\N{RIGHT DOUBLE QUOTATION MARK} caf\xe9 "
    planted = {
        "skills/checklist-ingest/logo.png": b"\x89PNG\r\n\x1a\n\x00\xff\xfe\x80 binary",
        ".agents/skills/checklist-ingest/notes.txt": (cp1252 + videos[0]).encode("cp1252"),
    }
    for relative, data in planted.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(data)
        with pytest.raises(UnicodeDecodeError):  # neither file is UTF-8
            data.decode("utf-8")

    found = leaks(tmp_path, videos, titles)

    assert found == {".agents/skills/checklist-ingest/notes.txt": [videos[0], titles[0]]}


def test_no_out_of_sample_id_or_title_appears_in_the_skill_or_agents_md() -> None:
    assert leaks(ROOT, out_of_sample_videos(), out_of_sample_titles()) == {}
