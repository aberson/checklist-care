"""Source packet files (plan §4.4): layout, encoding, hashing and reading back."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from checklist_care import paths
from checklist_care.sources import packet
from checklist_care.sources.packet import Chapter, Packet, Section, Segment, Transcript

EMOJI = "\U0001f600"


def youtube_packet() -> Packet:
    return Packet(
        source_ref="youtube:LpeDuC2sQ-w",
        kind="youtube",
        url="https://www.youtube.com/watch?v=LpeDuC2sQ-w",
        title=f"Tiny Changes {EMOJI}",
        channel="Some Channel",
        duration_s=563,
        fetched_at="2026-11-08T12:00:00Z",
        transcript=Transcript(
            kind="generated",
            language_code="en",
            segments=(Segment(0.0, 2.1, "line one\r\nwith a break"), Segment(2.1, 3.0, "two")),
        ),
        chapters=(Chapter("The Accountability Text", 0, 60), Chapter("Second", 60, 563)),
        warnings=("a warning",),
    )


def text_packet() -> Packet:
    return Packet(
        source_ref="text:sha256:" + "0" * 64,
        kind="text",
        url=None,
        title="Notes",
        channel=None,
        duration_s=None,
        fetched_at="2026-11-08T12:00:00Z",
        transcript=None,
        text="== A ==\nbody",
        sections=(Section("A", 2, 0),),
    )


def test_json_keys_follow_the_plan_order() -> None:
    assert list(youtube_packet().to_json()) == [
        "schema",
        "source_ref",
        "kind",
        "url",
        "title",
        "channel",
        "duration_s",
        "fetched_at",
        "transcript",
        "chapters",
        "text",
        "sections",
        "warnings",
    ]
    assert youtube_packet().to_json()["schema"] == "checklist-care.packet/1"


def test_encoded_file_is_utf8_lf_without_bom_one_item_per_line() -> None:
    data = packet.encode_packet(youtube_packet())

    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data  # a CR inside a string is escaped as \r
    assert data.endswith(b"}\n")
    text = data.decode("utf-8")
    assert EMOJI in text  # not \u-escaped
    assert json.loads(text) == youtube_packet().to_json()
    lines = text.splitlines()
    assert '      {"start": 0.0, "duration": 2.1, "text": "line one\\r\\nwith a break"},' in lines
    assert '    {"title": "The Accountability Text", "start_s": 0, "end_s": 60},' in lines
    assert '  "sections": [],' in lines


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "p.json"
    for item in (youtube_packet(), text_packet()):
        packet.write_packet(target, item)
        assert packet.read_packet(target) == item
        assert target.read_bytes() == packet.encode_packet(item)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["p.json"]  # no temp file left


def test_file_sha256_hashes_the_file_bytes(tmp_path: Path) -> None:
    target = tmp_path / "p.json"
    packet.write_packet(target, youtube_packet())
    digest = packet.file_sha256(target)
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert len(digest) == 64 and digest == digest.lower()


def test_counts() -> None:
    assert youtube_packet().counts() == "2/2"
    assert text_packet().counts() == f"{len('== A ==' + chr(10) + 'body')}/1"


def test_packet_path_uses_the_source_key(tmp_path: Path) -> None:
    ref = "youtube:LpeDuC2sQ-w"
    assert packet.packet_path(tmp_path, ref) == (
        tmp_path / "packets" / f"{paths.source_key(ref)}.json"
    )


@pytest.mark.parametrize(
    "data",
    [
        b"\xef\xbb\xbf{}",
        b"not json",
        b"[]",
        b'{"schema": "checklist-care.packet/2"}',
        b'{"schema": "checklist-care.packet/1"}',
    ],
)
def test_decode_rejects_bad_files(data: bytes) -> None:
    with pytest.raises(packet.PacketFormatError):
        packet.decode_packet(data)


@pytest.mark.parametrize(
    ("key", "value"),
    [("duration_s", "563"), ("duration_s", True), ("chapters", {}), ("warnings", [1])],
)
def test_decode_rejects_wrong_field_types(key: str, value: object) -> None:
    obj = youtube_packet().to_json()
    obj[key] = value
    with pytest.raises(packet.PacketFormatError):
        packet.decode_packet(json.dumps(obj).encode("utf-8"))


# --- PacketSwap: the packet changes only together with the committed row ------------


class CommitFailed(Exception):
    pass


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


@pytest.fixture(params=["hard-links", "no-hard-links"])
def links(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run each swap test with hard links, and on a file system without them (FAT)."""

    if request.param == "no-hard-links":

        def no_link(source: str | Path, destination: str | Path) -> None:
            raise OSError(1, "Incorrect function", str(source))

        monkeypatch.setattr(os, "link", no_link)
    return str(request.param)


def test_packet_swap_installs_the_new_file_and_leaves_nothing_behind(
    tmp_path: Path, links: str
) -> None:
    target = tmp_path / "p.json"
    packet.write_packet(target, text_packet())

    with packet.PacketSwap(target, youtube_packet()) as swap:
        assert target.read_bytes() == packet.encode_packet(text_packet())  # not yet
        swap.install()
        assert target.read_bytes() == packet.encode_packet(youtube_packet())

    assert target.read_bytes() == packet.encode_packet(youtube_packet())
    assert names(tmp_path) == ["p.json"]


def test_packet_swap_puts_the_old_file_back_when_the_block_fails_after_install(
    tmp_path: Path, links: str
) -> None:
    target = tmp_path / "p.json"
    packet.write_packet(target, text_packet())

    with pytest.raises(CommitFailed), packet.PacketSwap(target, youtube_packet()) as swap:
        swap.install()
        assert target.read_bytes() == packet.encode_packet(youtube_packet())
        raise CommitFailed

    assert target.read_bytes() == packet.encode_packet(text_packet())
    assert names(tmp_path) == ["p.json"]


def test_packet_swap_removes_a_first_packet_when_the_block_fails(
    tmp_path: Path, links: str
) -> None:
    target = tmp_path / "p.json"

    with pytest.raises(CommitFailed), packet.PacketSwap(target, youtube_packet()) as swap:
        swap.install()
        assert target.exists()
        raise CommitFailed

    assert names(tmp_path) == []


def test_packet_swap_leaves_the_file_alone_when_the_block_fails_before_install(
    tmp_path: Path,
) -> None:
    target = tmp_path / "p.json"
    packet.write_packet(target, text_packet())

    with pytest.raises(CommitFailed), packet.PacketSwap(target, youtube_packet()):
        raise CommitFailed

    assert target.read_bytes() == packet.encode_packet(text_packet())
    assert names(tmp_path) == ["p.json"]


def test_packet_swap_keeps_the_old_file_when_the_new_one_cannot_move_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, links: str
) -> None:
    target = tmp_path / "p.json"
    packet.write_packet(target, text_packet())
    real_replace = os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        if Path(source).name.endswith(".tmp"):
            raise PermissionError(13, "in use", str(destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(PermissionError), packet.PacketSwap(target, youtube_packet()) as swap:
        swap.install()

    assert target.read_bytes() == packet.encode_packet(text_packet())
    assert names(tmp_path) == ["p.json"]


def test_packet_swap_never_leaves_the_path_empty_where_links_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reader such as `context` sees the old file or the new one, never no file.
    target = tmp_path / "p.json"
    packet.write_packet(target, text_packet())
    real_replace = os.replace
    present: list[bool] = []

    def replace(source: str | Path, destination: str | Path) -> None:
        present.append(target.exists())
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)

    with packet.PacketSwap(target, youtube_packet()) as swap:
        swap.install()

    assert present == [True]  # one rename, over the still-present old file
    assert target.read_bytes() == packet.encode_packet(youtube_packet())
