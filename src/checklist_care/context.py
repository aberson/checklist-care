"""``checklist-care context``: what the skill reads before it proposes (plan §4.5).

:func:`build_context` returns the ``checklist-care.context/1`` object: the
``skill_sha256`` fingerprint, the text limits, the bucket rubrics, the list cues,
the stored tips a candidate is compared against and, with a source ref, that
source's packet hash, duration, chapters and section headings. ``propose``
checks a proposal against the same values, read through the same functions.

**``skill_sha256``** is one sha256 over the four skill files, in
:data:`SKILL_FILES` order. Each present file contributes its repo-relative path,
a NUL byte, then its bytes with line endings normalized to LF; a missing file
contributes its path followed by ``<absent>``, so the fingerprint is defined
before the skill exists. Any edit to the core, the shared rules or an adapter
yields a new value.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from checklist_care import limits
from checklist_care.db import connection
from checklist_care.errors import ConfigError
from checklist_care.sources import packet as packet_module
from checklist_care.store import buckets as bucket_store
from checklist_care.store import lists as list_store
from checklist_care.store import sources as source_store
from checklist_care.store import tips as tip_store

CONTEXT_SCHEMA: Final = "checklist-care.context/1"

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
"""The checkout root: ``src/checklist_care/context.py`` is two levels below it."""

SKILL_FILES: Final = (
    "skills/checklist-ingest/core.md",
    "skills/_shared/proposal-contract.md",
    ".claude/skills/checklist-ingest/SKILL.md",
    ".agents/skills/checklist-ingest/SKILL.md",
)
"""The files ``skill_sha256`` covers, repo-relative, in hashing order."""

ABSENT_MARKER: Final = b"<absent>"

CONTEXT_LIMITS: Final = (
    "NAME_MAX",
    "ACTION_MAX",
    "LINE_MAX",
    "REASON_MAX",
    "TIPS_MAX_EXTRACT",
    "TIPS_MAX_INVESTIGATE",
)
"""The ``limits.py`` names the skill receives; values are read from the module."""


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def skill_sha256(root: Path | None = None) -> str:
    """The skill fingerprint (64 lowercase hex digits) of the files under ``root``.

    ``root`` defaults to :data:`REPO_ROOT`, looked up at call time.
    """

    base = REPO_ROOT if root is None else root
    digest = hashlib.sha256()
    for relative in SKILL_FILES:
        digest.update(relative.encode("utf-8"))
        path = base / relative
        try:
            data = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            digest.update(ABSENT_MARKER)
            continue
        except OSError as error:
            raise ConfigError(f"cannot read skill file {path}: {error}") from error
        digest.update(b"\0")
        digest.update(_lf(data))
    return digest.hexdigest()


@dataclass(frozen=True)
class SourcePacket:
    """A fetched source with its packet, read once: the hash is of the decoded bytes."""

    source: source_store.SourceRow
    path: Path
    sha256: str
    packet: packet_module.Packet


def read_source_packet(path: Path) -> tuple[str, packet_module.Packet]:
    """The sha256 and the parsed packet of the file at ``path``, from one read.

    Raises ``FileNotFoundError`` for a missing file, ``OSError`` for an unreadable
    one and ``PacketFormatError`` for a malformed one.
    """

    return decode_source_packet(path.read_bytes())


def decode_source_packet(data: bytes) -> tuple[str, packet_module.Packet]:
    """The sha256 and the parsed packet of packet file bytes; ``PacketFormatError`` for
    malformed ones. :func:`read_source_packet` and the eval scorer's capped read
    (``scoring/score.py``) both decode through it."""

    return hashlib.sha256(data).hexdigest(), packet_module.decode_packet(data)


def load_source_packet(conn: sqlite3.Connection, data_dir: Path, ref: str) -> SourcePacket:
    """The source row of ``ref`` and its current packet; ``ConfigError`` (exit 2) when
    the source was never fetched or its packet is missing, unreadable or not its own."""

    source = source_store.get_by_ref(conn, ref)
    if source is None:
        raise ConfigError(f"no source {ref} in the database; run `checklist-care fetch` first")
    path = packet_module.packet_path(data_dir, ref)
    try:
        sha256, packet = read_source_packet(path)
    except FileNotFoundError as error:
        raise ConfigError(
            f"source {ref} has no packet at {path}; run `checklist-care fetch` again"
        ) from error
    except (OSError, packet_module.PacketFormatError) as error:
        raise ConfigError(
            f"cannot read the packet of {ref} at {path}: {error}; run `checklist-care fetch` again"
        ) from error
    if packet.source_ref != ref:
        raise ConfigError(
            f"the packet at {path} belongs to {packet.source_ref}, not {ref}; "
            "run `checklist-care fetch` again"
        )
    return SourcePacket(source=source, path=path, sha256=sha256, packet=packet)


def _source_block(loaded: SourcePacket) -> dict[str, Any]:
    packet = loaded.packet
    return {
        "ref": loaded.source.ref,
        "kind": loaded.source.kind,
        "packet_path": str(loaded.path),
        "packet_sha256": loaded.sha256,
        "duration_s": packet.duration_s,
        "chapters": [
            {"title": chapter.title, "start_s": chapter.start_s, "end_s": chapter.end_s}
            for chapter in packet.chapters
        ],
        "section_headings": [section.heading for section in packet.sections],
    }


def build_context(
    conn: sqlite3.Connection, data_dir: Path, source_ref: str | None = None
) -> dict[str, Any]:
    """The ``checklist-care.context/1`` object; without ``source_ref`` it has no
    ``source`` block. The database reads share one snapshot."""

    with connection.snapshot(conn):
        payload: dict[str, Any] = {
            "schema": CONTEXT_SCHEMA,
            "data_dir": str(data_dir),
            "skill_sha256": skill_sha256(),
            "limits": {name: getattr(limits, name) for name in CONTEXT_LIMITS},
            "buckets": [
                {"id": bucket.id, "title": bucket.title, "rubric": bucket.rubric}
                for bucket in bucket_store.all_buckets(conn)
            ],
            "lists": [
                {"id": row.id, "place": row.place, "audience": row.audience, "cue": row.cue}
                for row in list_store.all_lists(conn)
            ],
            "tips": [
                {
                    "id": tip.id,
                    "name": tip.name,
                    "action": tip.action,
                    "bucket": tip.bucket,
                    "audience": tip.audience,
                    "status": tip.status,
                    "source_ref": tip.source_ref,
                    "list_ids": list(tip.list_ids),
                }
                for tip in tip_store.known_tips(conn)
            ],
        }
        if source_ref is not None:
            payload["source"] = _source_block(load_source_packet(conn, data_dir, source_ref))
    return payload
