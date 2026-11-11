"""Pasted text and text files (plan §4.3): decoding, normalization, the cap and the ref.

The CLI path (``fetch --text-file`` through ``cli.main``) is covered by
``tests/integration/test_cli_fetch_text.py``; these tests pin the rules themselves.
All text here is self-written.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import unicodedata
from pathlib import Path

import pytest

from checklist_care import errors, limits, paths
from checklist_care.sources import packet, text

# Every character below exists in Windows-1252, so the same text has a cp1252 form.
SAMPLE = "Café notes — “one glass of water” before coffee, naïve or not: 5 €.\nSecond line.\n"
DECOMPOSED_E_ACUTE = "é"


def ref_of(data: bytes) -> str:
    return text.text_from_bytes(data).ref


def test_the_byte_cap_is_far_above_any_text_within_the_character_cap() -> None:
    # One NFC character comes from at most 4 code points of at most 4 UTF-8 bytes, and a
    # file may start with a 3-byte BOM: the byte cap must never refuse a text the
    # character cap accepts.
    assert text.TEXT_FILE_MAX_BYTES >= 16 * text.TEXT_CHARS_MAX + len(codecs.BOM_UTF8)


# --- decoding ------------------------------------------------------------------------


def test_cp1252_and_utf8_with_bom_give_the_same_ref() -> None:
    cp1252 = SAMPLE.encode("cp1252")
    utf8_bom = codecs.BOM_UTF8 + SAMPLE.encode("utf-8")
    assert cp1252 != utf8_bom  # really two different files

    from_cp1252 = text.text_from_bytes(cp1252)
    from_utf8 = text.text_from_bytes(utf8_bom)

    assert from_cp1252.encoding == "cp1252"
    assert from_utf8.encoding == "utf-8-sig"
    assert from_cp1252.text == from_utf8.text == SAMPLE
    assert from_cp1252.ref == from_utf8.ref
    assert from_utf8.ref == "text:sha256:" + hashlib.sha256(SAMPLE.encode("utf-8")).hexdigest()


def test_utf8_without_bom_gives_the_same_ref_too() -> None:
    assert ref_of(SAMPLE.encode("utf-8")) == ref_of(codecs.BOM_UTF8 + SAMPLE.encode("utf-8"))


def test_the_bom_is_not_part_of_the_text() -> None:
    assert text.text_from_bytes(codecs.BOM_UTF8 + b"Drink water").text == "Drink water"


def test_invalid_utf8_falls_back_to_cp1252() -> None:
    # 0xE9 alone is not UTF-8; in Windows-1252 it is "é".
    decoded, encoding = text.decode_text(b"caf\xe9")
    assert (decoded, encoding) == ("café", "cp1252")


def test_utf8_wins_when_both_decode() -> None:
    data = "café".encode()  # also valid cp1252 bytes ("cafÃ©")
    assert text.decode_text(data) == ("café", "utf-8-sig")


@pytest.mark.parametrize("bom", [codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE])
def test_utf16_is_refused_with_save_as_utf8(bom: bytes) -> None:
    encoding = "utf-16-le" if bom == codecs.BOM_UTF16_LE else "utf-16-be"
    with pytest.raises(errors.FetchError) as refused:
        text.text_from_bytes(bom + SAMPLE.encode(encoding))
    assert refused.value.exit_code == errors.EXIT_FETCH
    assert "UTF-16" in refused.value.message
    assert "save it as UTF-8" in refused.value.message


def test_a_binary_file_is_refused() -> None:
    pdf_like = b"%PDF-1.7\n\x00\x01\x02 binary"
    with pytest.raises(errors.FetchError, match="NUL bytes, so it is not plain text"):
        text.text_from_bytes(pdf_like)


def test_bytes_neither_encoding_decodes_are_refused() -> None:
    # 0x81 is undefined in Windows-1252 and a lone continuation byte in UTF-8.
    with pytest.raises(errors.FetchError, match="neither UTF-8 nor Windows-1252"):
        text.text_from_bytes(b"abc \x81 def")


# --- normalization -------------------------------------------------------------------


def test_crlf_and_lone_cr_become_lf() -> None:
    assert text.normalize_text("a\r\nb\rc\nd\r\n") == "a\nb\nc\nd\n"
    assert ref_of(b"one\r\ntwo\r\n") == ref_of(b"one\ntwo\n") == ref_of(b"one\rtwo\r")


def test_nfc_composes_decomposed_characters() -> None:
    decomposed = f"Caf{DECOMPOSED_E_ACUTE} notes"
    assert unicodedata.is_normalized("NFC", decomposed) is False
    normalized = text.normalize_text(decomposed)
    assert normalized == "Café notes"
    assert unicodedata.is_normalized("NFC", normalized)
    assert ref_of(decomposed.encode()) == ref_of(b"Caf\xe9 notes")  # NFC UTF-8 == cp1252 form


def test_source_ref_normalizes_its_argument() -> None:
    assert text.source_ref("a\r\nb") == text.source_ref("a\nb")
    assert re.fullmatch(r"text:sha256:[0-9a-f]{64}", text.source_ref("a"))
    assert text.source_ref("a") != text.source_ref("b")


def test_whitespace_inside_the_text_is_kept() -> None:
    # Only line endings and the Unicode form are normalized; nothing is trimmed.
    assert ref_of(b"Drink water\n") != ref_of(b"Drink water")
    spaced = "  indented\n\n\nparagraph  "
    assert text.text_from_bytes(spaced.encode()).text == spaced


# --- the cap and other refusals ------------------------------------------------------


def test_exactly_the_cap_is_accepted() -> None:
    accepted = text.text_from_bytes(b"x" * text.TEXT_CHARS_MAX)
    assert len(accepted.text) == text.TEXT_CHARS_MAX


def test_one_character_over_the_cap_is_too_long() -> None:
    with pytest.raises(errors.FetchError) as refused:
        text.text_from_bytes(b"x" * (text.TEXT_CHARS_MAX + 1))
    assert refused.value.exit_code == errors.EXIT_FETCH
    assert refused.value.code == errors.CODE_FETCH
    assert "200,001 characters" in refused.value.message
    assert "too long; split it" in refused.value.message


def test_the_cap_counts_normalized_characters() -> None:
    # CRLF counts as one character and a decomposed é as one: this file has more code
    # points than the cap but exactly the cap's worth of normalized characters.
    data = (b"\r\n" + DECOMPOSED_E_ACUTE.encode()) * (text.TEXT_CHARS_MAX // 2)
    assert len(data.decode()) > text.TEXT_CHARS_MAX
    assert len(text.text_from_bytes(data).text) == text.TEXT_CHARS_MAX


def test_a_file_over_the_byte_cap_is_too_long_without_being_decoded() -> None:
    # NUL bytes would be refused as binary if they were decoded first.
    with pytest.raises(errors.FetchError) as refused:
        text.text_from_bytes(b"\x00" * (text.TEXT_FILE_MAX_BYTES + 1))
    assert refused.value.exit_code == errors.EXIT_FETCH
    assert f"over {text.TEXT_FILE_MAX_BYTES:,} bytes" in refused.value.message
    assert "too long; split it" in refused.value.message


@pytest.mark.parametrize("data", [b"", codecs.BOM_UTF8, b" \r\n\t \n"])
def test_an_empty_text_is_refused(data: bytes) -> None:
    with pytest.raises(errors.FetchError, match="holds no text"):
        text.text_from_bytes(data)


# --- reading a file ------------------------------------------------------------------


def test_read_text_file_reads_and_normalizes(tmp_path: Path) -> None:
    target = tmp_path / "inbox.txt"
    target.write_bytes(SAMPLE.replace("\n", "\r\n").encode("cp1252"))
    loaded = text.read_text_file(target)
    assert loaded.text == SAMPLE
    assert loaded.encoding == "cp1252"


def test_read_text_file_reads_at_most_one_byte_over_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # How much is read is not observable from outside, so a spy counts it: the bound is
    # what keeps a mistaken multi-gigabyte file out of memory.
    monkeypatch.setattr(text, "TEXT_FILE_MAX_BYTES", 10)
    target = tmp_path / "big.txt"
    target.write_bytes(b"y" * 1000)
    seen: list[int] = []
    real = text.text_from_bytes
    monkeypatch.setattr(text, "text_from_bytes", lambda data: seen.append(len(data)) or real(data))
    with pytest.raises(errors.FetchError, match="too long"):
        text.read_text_file(target)
    assert seen == [11]


def test_a_missing_file_is_a_fetch_error(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    with pytest.raises(errors.FetchError) as refused:
        text.read_text_file(target)
    assert refused.value.exit_code == errors.EXIT_FETCH
    assert refused.value.message.startswith(
        f"cannot read the text file '{target}': FileNotFoundError ("
    )
    assert "\n" not in refused.value.line()


@pytest.mark.parametrize("make", ["directory", "device"])
def test_only_a_regular_file_is_read(tmp_path: Path, make: str) -> None:
    # A folder, or a device such as NUL / /dev/null (CON or /dev/zero would block or never
    # end), is refused before it is opened.
    target = tmp_path / "notes.txt" if make == "directory" else Path(os.devnull)
    if make == "directory":
        target.mkdir()
    with pytest.raises(errors.FetchError) as refused:
        text.read_text_file(target)
    assert refused.value.exit_code == errors.EXIT_FETCH
    assert refused.value.message == (
        f"cannot read the text file '{target}': it is not a regular file (a folder, a device "
        "or a pipe); give the path of a text file"
    )


# --- the title -----------------------------------------------------------------------


def test_clean_title_makes_one_nfc_line() -> None:
    assert text.clean_title(f"  Notes on\n caf{DECOMPOSED_E_ACUTE}  ") == "Notes on café"
    assert text.clean_title("Family 👨‍👩‍👧 mornings") == "Family 👨‍👩‍👧 mornings"


@pytest.mark.parametrize("raw", ["", "   ", "\n\t", "bell\x07", "lone \ud800 surrogate"])
def test_clean_title_refuses_blank_control_and_unencodable(raw: str) -> None:
    with pytest.raises(ValueError):
        text.clean_title(raw)


# --- the packet ----------------------------------------------------------------------


def test_build_packet_is_a_text_packet() -> None:
    source = text.text_from_bytes(SAMPLE.encode("utf-8"))
    built = text.build_packet(source, title="Notes", fetched_at="2026-11-08T12:00:00Z")

    assert built.to_json() == {
        "schema": packet.PACKET_SCHEMA,
        "source_ref": source.ref,
        "kind": limits.SOURCE_KIND_TEXT,
        "url": None,
        "title": "Notes",
        "channel": None,
        "duration_s": None,
        "fetched_at": "2026-11-08T12:00:00Z",
        "transcript": None,
        "chapters": [],
        "text": SAMPLE,
        "sections": [],
        "warnings": [],
    }
    assert built.counts() == f"{len(SAMPLE)}/0"
    assert text.revision(built) is None
    # The packet round-trips through the reader that `context` and `propose` use.
    assert packet.decode_packet(packet.encode_packet(built)) == built


def test_a_cp1252_packet_carries_a_warning() -> None:
    source = text.text_from_bytes(SAMPLE.encode("cp1252"))
    built = text.build_packet(source, title=None, fetched_at="2026-11-08T12:00:00Z")
    assert built.warnings == (text.CP1252_WARNING,)
    assert built.title is None


def test_the_packet_file_name_is_the_source_key_of_the_text_ref() -> None:
    ref = text.source_ref(SAMPLE)
    name = packet.packet_path(Path("data"), ref).name
    assert name == f"{paths.source_key(ref)}.json"
    assert name.startswith("text-sha256_")
