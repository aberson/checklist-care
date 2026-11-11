"""Wikipedia extract → packet text and sections (plan §4.3 text shape, §4.4).

All text here is self-written. ``tests/fixtures/packet_wiki_synthetic.json`` is checked
against the same functions, so the fixture the later steps build on stays in the shape
``fetch`` writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from checklist_care.sources import packet, wiki
from checklist_care.sources.packet import Section

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "packet_wiki_synthetic.json"

LEAD = "A synthetic lead paragraph about a made-up book."


def article(*sections: tuple[str, str]) -> str:
    """A plain-text extract: the lead, then ``\\n\\n\\n<heading line>\\n<body>`` each."""

    return LEAD + "".join(f"\n\n\n{heading}\n{body}" for heading, body in sections)


def headings(sections: tuple[Section, ...]) -> list[tuple[str, int]]:
    return [(section.heading, section.level) for section in sections]


def assert_offsets_point_at_headings(text: str, sections: tuple[Section, ...]) -> None:
    for section in sections:
        marks = "=" * section.level
        assert text[section.offset :].startswith(f"{marks} {section.heading} {marks}")
        assert section.offset == 0 or text[section.offset - 1] == "\n"


# --- sections --------------------------------------------------------------------------


def test_sections_have_heading_level_and_offset() -> None:
    text = article(
        ("== Ch.1: First idea ==", "Body one."),
        ("=== A subsection ===", "Body two."),
        ("==== A deeper one ====", "Body three."),
        ("== Reception ==", "Body four."),
    )

    sections = wiki.parse_sections(text)

    assert headings(sections) == [
        ("Ch.1: First idea", 2),
        ("A subsection", 3),
        ("A deeper one", 4),
        ("Reception", 2),
    ]
    assert sections[0].offset == len(LEAD) + 3
    assert_offsets_point_at_headings(text, sections)


@pytest.mark.parametrize(
    "line",
    [
        "=== Unbalanced ==",
        "== Unbalanced ===",
        "= Level one is not a section heading =",
        "==  ==",
        "== Heading == with trailing words",
        "Text with == markers == inside a line",
        "======= Seven marks =======",
    ],
)
def test_lines_that_are_not_headings(line: str) -> None:
    assert wiki.parse_sections(f"{LEAD}\n\n\n{line}\nBody.") == ()


def test_heading_white_space_is_trimmed() -> None:
    sections = wiki.parse_sections(f"{LEAD}\n\n\n==Tight==\nBody.\n\n\n==  Loose  == \nBody.")
    assert headings(sections) == [("Tight", 2), ("Loose", 2)]


# --- the tail ----------------------------------------------------------------------------


def test_the_reference_tail_is_cut() -> None:
    kept = article(("== Ch.1: First idea ==", "Body one."), ("== Reception ==", "Body two."))
    extract = kept + "".join(
        f"\n\n\n== {name} ==\nList line for {name}." for name in wiki.TAIL_HEADINGS
    )

    text, sections = wiki.shape_text(extract)

    assert text == kept
    assert headings(sections) == [("Ch.1: First idea", 2), ("Reception", 2)]


def test_tail_subsections_go_with_their_section() -> None:
    kept = article(("== Ch.1: First idea ==", "Body one."))
    extract = kept + (
        "\n\n\n== References ==\n"
        "\n\n\n=== Cited works ===\nA list line.\n"
        "\n\n\n== External links ==\nA link line.\n"
    )

    assert wiki.shape_text(extract) == (kept, wiki.parse_sections(kept))


def test_tail_headings_match_without_regard_to_case() -> None:
    kept = article(("== Ch.1: First idea ==", "Body one."))
    text, _ = wiki.shape_text(kept + "\n\n\n== SEE ALSO ==\nx\n\n\n== external LINKS ==\ny")
    assert text == kept


def test_only_a_trailing_run_is_cut() -> None:
    # A "Notes" section followed by content is content, not the tail.
    kept = article(
        ("== Notes ==", "A section the article calls notes."),
        ("== Ch.1: First idea ==", "Body one."),
    )
    text, sections = wiki.shape_text(kept + "\n\n\n== References ==\nA list line.")
    assert text == kept
    assert headings(sections) == [("Notes", 2), ("Ch.1: First idea", 2)]


def test_a_tail_named_subsection_of_a_content_section_is_kept() -> None:
    extract = article(("== Reception ==", "Body."), ("=== Notes ===", "Body of a subsection."))
    text, sections = wiki.shape_text(extract)
    assert text == extract
    assert headings(sections) == [("Reception", 2), ("Notes", 3)]


def test_similar_but_unlisted_headings_are_kept() -> None:
    extract = article(("== Ch.1: First idea ==", "Body."), ("== Works cited ==", "A line."))
    assert wiki.shape_text(extract)[0] == extract


def test_an_article_of_only_a_lead_and_a_tail_keeps_the_lead() -> None:
    assert wiki.shape_text(article(("== See also ==", "x"), ("== References ==", ""))) == (
        LEAD,
        (),
    )


def test_text_is_normalized_and_stripped() -> None:
    extract = "\n Café lead.\r\n\r\n\r\n== Ch.1: Naïve ==\r\nBody.\r\n\n"

    text, sections = wiki.shape_text(extract)

    assert text == "Café lead.\n\n\n== Ch.1: Naïve ==\nBody."
    assert "\r" not in text
    assert headings(sections) == [("Ch.1: Naïve", 2)]
    assert_offsets_point_at_headings(text, sections)


# --- the fixture -------------------------------------------------------------------------


def test_the_synthetic_fixture_is_a_wiki_packet_in_fetch_shape() -> None:
    fixture = packet.read_packet(FIXTURE)

    assert fixture.kind == "wiki"
    assert fixture.source_ref == "wiki:en:The_Happiness_Hypothesis"
    assert fixture.url == "https://en.wikipedia.org/wiki/The_Happiness_Hypothesis"
    assert (fixture.transcript, fixture.duration_s, fixture.chapters) == (None, None, ())
    assert fixture.text is not None
    assert wiki.shape_text(fixture.text) == (fixture.text, fixture.sections)
    chapters = [s.heading for s in fixture.sections if s.heading.startswith("Ch.")]
    assert [heading.split(":", 1)[0] for heading in chapters] == [f"Ch.{n}" for n in range(1, 12)]
    assert [s.heading for s in fixture.sections][-1] == "Reception"
    assert all(section.level == 2 for section in fixture.sections)
    assert_offsets_point_at_headings(fixture.text, fixture.sections)
    assert packet.encode_packet(fixture) == FIXTURE.read_bytes()
