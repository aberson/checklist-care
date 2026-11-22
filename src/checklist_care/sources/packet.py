"""The source packet (plan §4.4): ``<data-dir>/packets/<source-key>.json``.

``fetch`` writes one packet per source; the skill reads it, and ``context`` and
``propose`` compare its sha256. The file is UTF-8 without a BOM, with LF line
endings. :func:`file_sha256` hashes the file bytes, so every reader that hashes
the file agrees with the ``packet_sha256`` that ``fetch`` printed.

The layout is plain JSON with one list item per line (one transcript segment,
chapter, section or warning each), so a file viewer that reads a fixed number of
lines sees the whole transcript.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass, fields
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from checklist_care import paths
from checklist_care.paths import lacks_hard_links, sibling_path

PACKET_SCHEMA: Final = "checklist-care.packet/1"
PACKET_SUFFIX: Final = ".json"
_INDENT: Final = "  "


class PacketFormatError(ValueError):
    """A packet file is not a valid ``checklist-care.packet/1`` document."""


@dataclass(frozen=True)
class Segment:
    """One transcript snippet, in seconds as the transcript library reports them."""

    start: float
    duration: float
    text: str


@dataclass(frozen=True)
class Transcript:
    kind: str
    """One of ``limits.TRANSCRIPT_KINDS``."""
    language_code: str
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class Chapter:
    """A video chapter; ``start_s`` and ``end_s`` are whole seconds (``floor``)."""

    title: str
    start_s: int
    end_s: int


@dataclass(frozen=True)
class Section:
    """A heading of a wiki or text packet and the character offset where it starts."""

    heading: str
    level: int
    offset: int


@dataclass(frozen=True)
class Packet:
    """One source packet. YouTube packets carry ``transcript`` and ``chapters``;
    wiki and text packets carry ``text`` and ``sections`` instead."""

    source_ref: str
    kind: str
    url: str | None
    title: str | None
    channel: str | None
    duration_s: int | None
    fetched_at: str
    transcript: Transcript | None
    chapters: tuple[Chapter, ...] = ()
    text: str | None = None
    sections: tuple[Section, ...] = ()
    warnings: tuple[str, ...] = ()

    def counts(self) -> str:
        """The ``fetch`` counts: ``<segments>/<chapters>``, or ``<chars>/<sections>``."""

        if self.transcript is not None:
            return f"{len(self.transcript.segments)}/{len(self.chapters)}"
        return f"{len(self.text or '')}/{len(self.sections)}"

    def to_json(self) -> dict[str, Any]:
        """The JSON object, keys in the plan §4.4 order."""

        transcript = None
        if self.transcript is not None:
            transcript = {
                "kind": self.transcript.kind,
                "language_code": self.transcript.language_code,
                "segments": [
                    {"start": seg.start, "duration": seg.duration, "text": seg.text}
                    for seg in self.transcript.segments
                ],
            }
        return {
            "schema": PACKET_SCHEMA,
            "source_ref": self.source_ref,
            "kind": self.kind,
            "url": self.url,
            "title": self.title,
            "channel": self.channel,
            "duration_s": self.duration_s,
            "fetched_at": self.fetched_at,
            "transcript": transcript,
            "chapters": [
                {"title": ch.title, "start_s": ch.start_s, "end_s": ch.end_s}
                for ch in self.chapters
            ],
            "text": self.text,
            "sections": [
                {"heading": sec.heading, "level": sec.level, "offset": sec.offset}
                for sec in self.sections
            ],
            "warnings": list(self.warnings),
        }


def packet_path(data_dir: Path, ref: str) -> Path:
    """``<data-dir>/packets/<source_key(ref)>.json``."""

    return data_dir / paths.PACKETS_DIR / f"{paths.source_key(ref)}{PACKET_SUFFIX}"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _render(value: Any, indent: str) -> str:
    inner = indent + _INDENT
    if isinstance(value, dict) and value:
        body = ",\n".join(
            f"{inner}{_compact(key)}: {_render(v, inner)}" for key, v in value.items()
        )
        return "{\n" + body + "\n" + indent + "}"
    if isinstance(value, list) and value:
        body = ",\n".join(inner + _compact(item) for item in value)
        return "[\n" + body + "\n" + indent + "]"
    return _compact(value)


def encode_packet(packet: Packet) -> bytes:
    """The packet file bytes: UTF-8, no BOM, LF, a final newline."""

    return (_render(packet.to_json(), "") + "\n").encode("utf-8")


def write_packet(path: Path, packet: Packet) -> None:
    """Write the packet atomically: a reader sees the old file or the new one."""

    data = encode_packet(packet)
    temporary = sibling_path(path, ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PacketSwap:
    """Replace a packet file in step with a database transaction.

    Entering writes the new bytes to a temporary file. :meth:`install`, the last
    step inside the transaction, keeps the old packet under a backup name (a hard
    link) and renames the new one over it, so a reader still sees the old file
    or the new one. If the block raises after that (the ``COMMIT`` failed), the
    backup is renamed back, or the new packet removed when there was none, so the
    packet and the ``source`` row never disagree. Links and renames need no free
    space, so the restore also works on a full disk. On success the backup name
    is removed.

    ``fetch`` uses it as::

        with PacketSwap(path, packet) as swap:
            with connection.transaction(conn):
                ...  # checks and the upsert
                swap.install()
    """

    def __init__(self, path: Path, packet: Packet) -> None:
        self.path = path
        self._data = encode_packet(packet)
        self._temporary = sibling_path(path, ".tmp")
        self._backup = sibling_path(path, ".bak")
        self._old_kept: str | None = None
        """None (no old packet), ``"linked"`` (also still at ``path``) or ``"moved"``."""
        self._installed = False

    def __enter__(self) -> PacketSwap:
        try:
            self._temporary.write_bytes(self._data)
        except BaseException:
            self._temporary.unlink(missing_ok=True)
            raise
        return self

    def install(self) -> None:
        """Put the new packet in place, keeping the old one under the backup name."""

        self._backup.unlink(missing_ok=True)  # left by a crashed run with this pid
        try:
            os.link(self.path, self._backup)
        except OSError as error:
            if lacks_hard_links(error):
                # No hard links on this file system (FAT, some shares): move the old
                # packet aside instead; a reader may then briefly find no file.
                try:
                    os.replace(self.path, self._backup)
                except FileNotFoundError:
                    pass  # the first fetch of this source
                else:
                    self._old_kept = "moved"
            # else no old packet to link: the first fetch of this source
        else:
            self._old_kept = "linked"
        try:
            os.replace(self._temporary, self.path)
        except BaseException:
            self._restore()
            raise
        self._installed = True

    def _restore(self) -> None:
        """Undo :meth:`install`. A linked old packet that was never replaced is
        still at ``path`` and needs nothing."""

        if self._old_kept is not None and (self._installed or self._old_kept == "moved"):
            os.replace(self._backup, self.path)
            self._old_kept = None
        elif self._installed:
            self.path.unlink(missing_ok=True)
        self._installed = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self._restore()  # if this raises, the backup stays for recovery
            # Leftovers are harmless dot files; failing to delete one must not
            # turn a committed fetch into an error.
            with suppress(OSError):
                self._backup.unlink(missing_ok=True)
        finally:
            with suppress(OSError):
                self._temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    """The 64 lowercase hex digits of ``sha256`` over the file's bytes."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- reading -------------------------------------------------------------------------


_TEXT_OR_NONE: Final = (str, type(None))
_INT_OR_NONE: Final = (int, type(None))
_NUMBER: Final = (int, float)


def _object(value: Any, cls: type, where: str) -> dict[str, Any]:
    """``value`` as a JSON object that has every field of the dataclass ``cls``."""

    if not isinstance(value, dict):
        raise PacketFormatError(f"{where} is not an object")
    missing = [item.name for item in fields(cls) if item.name not in value]
    if missing:
        raise PacketFormatError(f"{where} lacks {', '.join(missing)}")
    return value


def _typed(value: Any, types: tuple[type, ...], where: str) -> Any:
    wrong_bool = isinstance(value, bool) and bool not in types
    if wrong_bool or not isinstance(value, types):
        raise PacketFormatError(f"{where} has the wrong type")
    return value


def _items(value: Any, where: str) -> list[Any]:
    return list(_typed(value, (list,), where))


def _segment(value: Any, where: str) -> Segment:
    obj = _object(value, Segment, where)
    return Segment(
        start=float(_typed(obj["start"], _NUMBER, f"{where}.start")),
        duration=float(_typed(obj["duration"], _NUMBER, f"{where}.duration")),
        text=_typed(obj["text"], (str,), f"{where}.text"),
    )


def _transcript(value: Any) -> Transcript | None:
    if value is None:
        return None
    obj = _object(value, Transcript, "transcript")
    segments = _items(obj["segments"], "transcript.segments")
    return Transcript(
        kind=_typed(obj["kind"], (str,), "transcript.kind"),
        language_code=_typed(obj["language_code"], (str,), "transcript.language_code"),
        segments=tuple(_segment(item, f"segment {i}") for i, item in enumerate(segments)),
    )


def _chapter(value: Any, where: str) -> Chapter:
    obj = _object(value, Chapter, where)
    return Chapter(
        title=_typed(obj["title"], (str,), f"{where}.title"),
        start_s=_typed(obj["start_s"], (int,), f"{where}.start_s"),
        end_s=_typed(obj["end_s"], (int,), f"{where}.end_s"),
    )


def _section(value: Any, where: str) -> Section:
    obj = _object(value, Section, where)
    return Section(
        heading=_typed(obj["heading"], (str,), f"{where}.heading"),
        level=_typed(obj["level"], (int,), f"{where}.level"),
        offset=_typed(obj["offset"], (int,), f"{where}.offset"),
    )


def decode_packet(data: bytes) -> Packet:
    """Parse packet file bytes; raises :class:`PacketFormatError` on any mismatch."""

    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PacketFormatError(f"packet is not UTF-8 JSON: {error}") from error
    if not isinstance(parsed, dict) or parsed.get("schema") != PACKET_SCHEMA:
        raise PacketFormatError(f"packet is not a {PACKET_SCHEMA!r} object")
    obj = _object(parsed, Packet, "packet")
    chapters = _items(obj["chapters"], "packet.chapters")
    sections = _items(obj["sections"], "packet.sections")
    warnings = _items(obj["warnings"], "packet.warnings")
    return Packet(
        source_ref=_typed(obj["source_ref"], (str,), "packet.source_ref"),
        kind=_typed(obj["kind"], (str,), "packet.kind"),
        url=_typed(obj["url"], _TEXT_OR_NONE, "packet.url"),
        title=_typed(obj["title"], _TEXT_OR_NONE, "packet.title"),
        channel=_typed(obj["channel"], _TEXT_OR_NONE, "packet.channel"),
        duration_s=_typed(obj["duration_s"], _INT_OR_NONE, "packet.duration_s"),
        fetched_at=_typed(obj["fetched_at"], (str,), "packet.fetched_at"),
        transcript=_transcript(obj["transcript"]),
        chapters=tuple(_chapter(item, f"chapter {i}") for i, item in enumerate(chapters)),
        text=_typed(obj["text"], _TEXT_OR_NONE, "packet.text"),
        sections=tuple(_section(item, f"section {i}") for i, item in enumerate(sections)),
        warnings=tuple(_typed(item, (str,), f"warning {i}") for i, item in enumerate(warnings)),
    )


def read_packet(path: Path) -> Packet:
    """Read and parse the packet file at ``path``."""

    return decode_packet(path.read_bytes())
