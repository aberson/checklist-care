"""Strict JSON decoding for every file the skill or the operator writes and checklist-care reads.

``json.loads`` keeps the last of two repeated keys and accepts ``NaN``, ``Infinity``
and ``-Infinity``; :func:`decode` refuses all of them, so the value read is the value
that was written. It owns the whole ladder from file bytes to one JSON value: a
UTF-16 byte-order mark and NUL bytes (UTF-16 or UTF-32 text) are refused before
decoding, the text is UTF-8 (a leading UTF-8 byte-order mark is allowed), and the
JSON is strict. Every defect is one :class:`JsonFileError` whose :attr:`~JsonFileError.kind`
classifies it. The proposal decoder (``proposal.decode``) and the eval labels reader
(``scoring/score.py``) both read through it and each turns that error into its own
error type, adding its own advice to the shared message, so the same bytes get the
same treatment whichever reader sees them.

Both readers also share the rungs around it: :func:`read_capped` reads a file no
further than its cap, :func:`read_error_reason` words why a file could not be read,
and :func:`is_whole` is the one test for a JSON whole number. The text-file reader
(``sources/text.py``) shares the byte-level facts: :data:`PRIMARY_ENCODING`, the
encoding it tries first, and :data:`UTF16_BOMS`, which it refuses too. Every eval input
file (``scoring/score.py``) and the text file pass the one regular-file gate,
:func:`is_regular_file`, and word its refusal with :data:`NOT_REGULAR_FILE`.
"""

from __future__ import annotations

import codecs
import json
import stat
from pathlib import Path
from typing import Any, Final, TypeGuard

from checklist_care.errors import one_line

UTF16: Final = "utf-16"
NUL_BYTES: Final = "nul-bytes"
NOT_UTF8: Final = "not-utf-8"
DUPLICATE_KEY: Final = "duplicate-key"
BAD_CONSTANT: Final = "bad-constant"
NOT_JSON: Final = "not-json"

ENCODING_KINDS: Final = frozenset({UTF16, NUL_BYTES, NOT_UTF8})
"""The kinds a reader answers with "save the file as UTF-8"."""

PRIMARY_ENCODING: Final = "utf-8-sig"
"""UTF-8, with a leading UTF-8 byte-order mark dropped: the encoding :func:`decode`
reads, and the first one the text-file reader (``sources/text.py``) tries."""

UTF16_BOMS: Final = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
"""Byte-order marks of UTF-16 (and of UTF-32 LE, which starts like UTF-16 LE). Such a
file is not refused by a UTF-8 or Windows-1252 decoder, only garbled, so it is caught
first, on the bytes. Windows PowerShell 5.1's ``>`` and ``Out-File`` write UTF-16 LE.
:func:`decode` and the text-file reader (``sources/text.py``) both check them."""

NOT_REGULAR_FILE: Final = "it is not a regular file (a folder, a device or a pipe)"
"""The refusal clause of :func:`is_regular_file`, which each reader wraps in its own
sentence."""

_DETAIL_MAX: Final = 200


class JsonFileError(ValueError):
    """File bytes that are not one strict UTF-8 JSON value.

    :attr:`kind` is one of the module's kind constants; :attr:`message` is one line
    naming the defect (``the key 'a' appears twice in one object``), with no advice,
    which each reader adds for itself.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def is_whole(value: Any) -> TypeGuard[int]:
    """True for a JSON whole number: an ``int`` that is not a ``bool``."""

    return isinstance(value, int) and not isinstance(value, bool)


def read_capped(path: Path, max_bytes: int) -> bytes | None:
    """The bytes of the file at ``path``, or None when it holds more than ``max_bytes``
    (read no further than one byte past the cap). An ``OSError`` propagates; each
    reader words both refusals itself."""

    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    return None if len(data) > max_bytes else data


def is_regular_file(path: Path) -> bool:
    """True when ``path`` is a regular file. A folder, a device (``NUL``, ``CON``,
    ``/dev/zero``) or a pipe is not, and is refused before anything opens it, since
    reading one can block or never end (:data:`NOT_REGULAR_FILE`). An ``OSError`` from
    inspecting the path (``FileNotFoundError`` included) propagates."""

    return stat.S_ISREG(path.stat().st_mode)


def read_error_reason(error: OSError) -> str:
    """Why a file could not be read: the system's message (``strerror``), else the
    error's type name. Each file reader words its ``OSError`` refusal around it."""

    return error.strerror or type(error).__name__


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonFileError(DUPLICATE_KEY, f"the key {key!r} appears twice in one object")
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise JsonFileError(BAD_CONSTANT, f"{name} is not a JSON number")


def decode(data: bytes) -> Any:
    """The JSON value in the file bytes ``data``; :class:`JsonFileError` on any defect.

    In order: a UTF-16 byte-order mark (:data:`UTF16`; a UTF-32 little-endian mark
    starts with one too), NUL bytes (:data:`NUL_BYTES`), bytes that are not UTF-8
    (:data:`NOT_UTF8`), a repeated key at any depth (:data:`DUPLICATE_KEY`), ``NaN``
    or an infinity (:data:`BAD_CONSTANT`), and anything else ``json.loads`` refuses,
    including an over-long integer and deep nesting (:data:`NOT_JSON`).
    """

    if data.startswith(UTF16_BOMS):
        raise JsonFileError(UTF16, "the file is UTF-16 (it starts with a UTF-16 byte-order mark)")
    if b"\x00" in data:
        raise JsonFileError(NUL_BYTES, "the file holds NUL bytes, as UTF-16 or UTF-32 text does")
    try:
        text = data.decode(PRIMARY_ENCODING)
    except UnicodeDecodeError as error:
        raise JsonFileError(
            NOT_UTF8, f"the file is not UTF-8 (byte {error.start} is invalid)"
        ) from error
    try:
        return json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except JsonFileError:
        raise
    except json.JSONDecodeError as error:
        raise JsonFileError(
            NOT_JSON,
            f"the file is not valid JSON: {error.msg} at line {error.lineno} column {error.colno}",
        ) from error
    except (ValueError, RecursionError) as error:  # an over-long integer, deep nesting
        detail = one_line(str(error))[:_DETAIL_MAX] or type(error).__name__
        raise JsonFileError(NOT_JSON, f"the file is not valid JSON: {detail}") from error
