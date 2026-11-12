"""Pasted text and text files (plan §4.3): file bytes → normalized text → packet.

``fetch --text-file <path> [--title TITLE]`` reads one local file. The skill first
copies a file the operator names, unread, into ``<data-dir>/inbox/``, and saves
pasted chat text there as UTF-8 (plan §3.1, §4.6), so this module only sees files.

* **Decode** as ``utf-8-sig`` (a UTF-8 byte-order mark is dropped), else as
  ``cp1252``. A UTF-16 byte-order mark, a NUL byte (a binary file such as a PDF or a
  Word document) or bytes that neither encoding decodes are refused.
* **Normalize** to Unicode NFC with LF line endings: CRLF and a lone CR become LF.
* **Cap** at :data:`TEXT_CHARS_MAX` characters of normalized text: a longer text is
  refused as too long, to be split. So is a file over :data:`TEXT_FILE_MAX_BYTES`,
  before it is decoded.
* **Ref:** ``text:sha256:<64 lowercase hex>`` of the normalized text's UTF-8 bytes,
  so the same text gives the same ref whatever encoding, byte-order mark or line
  endings its file had.

Every refusal raises :class:`FetchError` (exit 4, ``CC_E_FETCH``), before ``fetch``
writes anything. A text packet has ``transcript``, ``url``, ``channel`` and
``duration_s`` null, no chapters and no sections; ``text`` holds the whole text.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from checklist_care.errors import FetchError, one_line
from checklist_care.limits import SOURCE_KIND_TEXT
from checklist_care.sources.packet import Packet
from checklist_care.strict_json import (
    NOT_REGULAR_FILE,
    PRIMARY_ENCODING,
    UTF16_BOMS,
    is_regular_file,
)

# The ingest caps live here, not in limits.py: limits.py is the tip-text and enum
# surface that `GET /api/meta` exports whole, and the web app never sees an ingest cap.

TEXT_CHARS_MAX: Final = 200_000
"""The longest text ``fetch --text-file`` accepts, in characters of normalized text
(NFC, LF line endings), plan §4.3. A longer one exits 4: too long; split it."""

TEXT_FILE_MAX_BYTES: Final = 16 * 1024 * 1024
"""The largest file ``fetch --text-file`` reads. It only stops a mistaken huge file (a
video, an archive) from being read whole: one normalized character comes from at most
four code points of at most four UTF-8 bytes each, so a text within ``TEXT_CHARS_MAX``
fits in about 3.2 MB."""

FALLBACK_ENCODING: Final = "cp1252"
ENCODINGS: Final = (PRIMARY_ENCODING, FALLBACK_ENCODING)
"""The decoding order of plan §4.3: UTF-8 (with or without a byte-order mark), then
Windows-1252."""

NORMAL_FORM: Final = "NFC"
REF_PREFIX: Final = f"{SOURCE_KIND_TEXT}:sha256:"
TOO_LONG_HINT: Final = "too long; split it"
"""The refusal of plan §4.3 for a text over ``TEXT_CHARS_MAX`` characters."""

CP1252_WARNING: Final = (
    "the file is not UTF-8, so it was read as Windows-1252 (cp1252); if characters look "
    "wrong, save it as UTF-8 and fetch it again"
)

_SAVE_AS_UTF8: Final = "save it as UTF-8 text and fetch it again"
_SPLIT_IT: Final = f"{TOO_LONG_HINT} into parts and fetch each part"


@dataclass(frozen=True)
class SourceText:
    """The normalized text of one file and the encoding it was read with."""

    text: str
    encoding: str
    """One of :data:`ENCODINGS`."""

    @property
    def ref(self) -> str:
        return source_ref(self.text)


def normalize_text(text: str) -> str:
    """``text`` in Unicode NFC with LF line endings (CRLF and lone CR become LF)."""

    composed = unicodedata.normalize(NORMAL_FORM, text)
    return composed.replace("\r\n", "\n").replace("\r", "\n")


def source_ref(text: str) -> str:
    """``text:sha256:<64 lowercase hex>`` of the normalized text, the dedupe key of a
    text source (plan §3.2). ``text`` is normalized first, so passing normalized or
    raw text gives the same ref."""

    digest = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
    return f"{REF_PREFIX}{digest}"


def decode_text(data: bytes) -> tuple[str, str]:
    """The text of file bytes and the encoding that decoded them, ``utf-8-sig`` first,
    then ``cp1252``; :class:`FetchError` for UTF-16, NUL bytes or undecodable bytes."""

    # On the bytes, so a Windows-1252 text that starts with "ÿþ" or "þÿ" (the same two
    # bytes) is refused too; no advice text starts that way.
    if data.startswith(UTF16_BOMS):
        raise FetchError(
            f"the text file is UTF-16 (it starts with a UTF-16 byte-order mark); {_SAVE_AS_UTF8}"
        )
    if b"\x00" in data:
        raise FetchError(
            "the text file holds NUL bytes, so it is not plain text (a PDF, a Word document or "
            f"another binary file); {_SAVE_AS_UTF8}"
        )
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise FetchError(f"the text file is neither UTF-8 nor Windows-1252 text; {_SAVE_AS_UTF8}")


def text_from_bytes(data: bytes) -> SourceText:
    """Decode, normalize and check file bytes: the text must hold more than white space
    and at most ``TEXT_CHARS_MAX`` characters once normalized."""

    if len(data) > TEXT_FILE_MAX_BYTES:
        raise FetchError(
            f"the text file is over {TEXT_FILE_MAX_BYTES:,} bytes, far over the "
            f"{TEXT_CHARS_MAX:,}-character limit: {_SPLIT_IT}"
        )
    decoded, encoding = decode_text(data)
    text = normalize_text(decoded)
    if len(text) > TEXT_CHARS_MAX:
        raise FetchError(
            f"the text is {len(text):,} characters, over the {TEXT_CHARS_MAX:,}-character "
            f"limit: {_SPLIT_IT}"
        )
    if not text.strip():
        raise FetchError("the text file holds no text; give a file or paste text with the advice")
    return SourceText(text=text, encoding=encoding)


def read_text_file(path: Path) -> SourceText:
    """Read the file at ``path`` (at most ``TEXT_FILE_MAX_BYTES`` + 1 bytes) and return
    its checked, normalized text; :class:`FetchError` when it cannot be read or used.

    Only a regular file is opened: a folder, a device (``NUL``, ``CON``, ``/dev/zero``)
    or a pipe is refused first, since reading one can block or never end.
    """

    unreadable = f"cannot read the text file '{path}'"
    try:
        if not is_regular_file(path):
            raise FetchError(f"{unreadable}: {NOT_REGULAR_FILE}; give the path of a text file")
        with path.open("rb") as handle:
            data = handle.read(TEXT_FILE_MAX_BYTES + 1)
    except OSError as error:
        reason = error.strerror or str(error)
        raise FetchError(f"{unreadable}: {type(error).__name__} ({one_line(reason)})") from error
    return text_from_bytes(data)


def clean_title(raw: str) -> str:
    """The ``--title`` value as one NFC line; ``ValueError`` when it is blank, holds a
    control character or cannot be stored as UTF-8 (a lone surrogate)."""

    title = unicodedata.normalize(NORMAL_FORM, one_line(raw))
    if not title:
        raise ValueError("the title is empty")
    if any(unicodedata.category(char) == "Cc" for char in title):
        raise ValueError("the title holds a control character")
    try:
        title.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("the title is not valid text") from error
    return title


def revision(packet: Packet) -> None:
    """``source.revision`` of a text source: none. The ref is the text's own hash, so a
    changed text is a new source, not a new revision of the old one."""

    return None


def build_packet(source: SourceText, *, title: str | None, fetched_at: str) -> Packet:
    """The text packet (plan §4.4): ``text`` filled, ``transcript`` null, no chapters.

    A file read as Windows-1252 gets a warning, since a file in another legacy
    encoding also decodes as Windows-1252, only with wrong characters.
    """

    warnings = (CP1252_WARNING,) if source.encoding == FALLBACK_ENCODING else ()
    return Packet(
        source_ref=source.ref,
        kind=SOURCE_KIND_TEXT,
        url=None,
        title=title,
        channel=None,
        duration_s=None,
        fetched_at=fetched_at,
        transcript=None,
        chapters=(),
        text=source.text,
        sections=(),
        warnings=warnings,
    )
