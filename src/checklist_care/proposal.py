"""``checklist-care propose <file>``: decode, validate and import a proposal (plan §4.5).

A proposal is the skill's only output. :func:`decode` reads the file bytes,
:func:`validate` checks every field against ``limits.py``, the placement rules,
the stored tips and the current packet and skill, and :func:`submit` runs both
and imports the tips as ``pending`` in one ``BEGIN IMMEDIATE`` transaction:

* the tips, with their placements appended at the end of each list;
* ``source.reviewed_at`` and ``source.mode``;
* the archived copy ``proposals/<source-key>.1.json``, put in place as the
  transaction's last step and removed again if the ``COMMIT`` fails.

Any decoding or validation failure raises :class:`ProposalInvalidError` (exit 5,
``CC_E_PROPOSAL``) carrying every problem as ``{index, field, problem}``, where
``index`` is the tip's position in ``tips`` or null for the envelope, and
nothing is written. A source that is already reviewed raises
``AlreadyReviewedError`` (exit 3) before any other check.

Every string in a proposal comes from an AI host and is untrusted: tip text
fields are one line and capped by ``limits.py``, the file by
``PROPOSAL_FILE_MAX_BYTES``, and a ``\\ud800``-style JSON escape that decodes to
an unpaired surrogate (not a character, so neither storable nor printable as
UTF-8) is reported on its field before any other check reads the value.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, TypeGuard
from urllib.parse import urlsplit

from checklist_care import context, paths, placement
from checklist_care.db import connection
from checklist_care.errors import ConfigError, Problem, ProposalInvalidError, one_line
from checklist_care.limits import (
    ACTION_MAX,
    AUDIENCES_TIP,
    BUCKET_DAILY,
    BUCKET_IDS,
    BUCKET_LEFT_OUT,
    EVIDENCE_URL_MAX,
    ID_MAX,
    LINE_MAX,
    LINE_SEPARATOR,
    LOCATOR_MAX,
    NAME_MAX,
    NOTE_MAX,
    PROPOSAL_FILE_MAX_BYTES,
    PROPOSAL_HOSTS,
    PROPOSAL_MODES,
    REASON_MAX,
    SOURCE_KIND_MANUAL,
    SOURCE_KIND_YOUTUBE,
    SOURCE_MODE_INVESTIGATE,
    START_S_MAX,
    TIP_STATUS_PENDING,
    line_length,
    tips_max,
)
from checklist_care.server import base_url
from checklist_care.store import lists as list_store
from checklist_care.store import sources as source_store
from checklist_care.store import tips as tip_store

PROPOSAL_SCHEMA: Final = "checklist-care.proposal/1"

REVIEW_URL: Final = f"{base_url()}/#/review"
"""The review inbox of the daily server (plan §1, §2), built from the server's address."""

ARCHIVE_SUFFIX: Final = ".1.json"
"""``proposals/<source-key>.1.json``: the one accepted proposal of a source."""

ENVELOPE_FIELDS: Final = (
    "schema",
    "source_ref",
    "packet_sha256",
    "skill_sha256",
    "host",
    "model",
    "mode",
    "degraded",
    "tips",
)
"""Every envelope field; all are required."""

TIP_REQUIRED: Final = ("name", "action", "reason", "bucket", "audience", "list_ids")
TIP_OPTIONAL: Final = (
    "start_s",
    "locator",
    "evidence_url",
    "duplicate_of",
    "possible_duplicate_of",
    "note",
)
"""Tip fields that may be omitted, meaning null."""
TIP_FIELDS: Final = TIP_REQUIRED + TIP_OPTIONAL

FILE_FIELD: Final = "file"
"""The ``field`` of a problem with the file itself (encoding or JSON syntax)."""
TIP_FIELD: Final = "tip"
"""The ``field`` of a problem with a whole tip entry."""

SAVE_AS_UTF8: Final = "save as UTF-8 (without a byte-order mark is best) and submit it again"

_SHA256_PATTERN: Final = re.compile(r"[0-9a-fA-F]{64}")
_UTF16_BOMS: Final = (b"\xff\xfe", b"\xfe\xff")
_LINE_BREAKING: Final = frozenset({"Cc", "Zl", "Zp"})
"""Unicode categories that break or control a one-line text: controls and separators."""
_SURROGATE: Final = re.compile("[\ud800-\udfff]")
"""Unicode category Cs. In a string decoded from JSON a surrogate is always unpaired:
a valid ``\\ud83d\\ude00`` escape pair decodes to one character."""
UNPAIRED_SURROGATE: Final = (
    "holds an unpaired surrogate escape (a lone \\ud800 to \\udfff), which is not a "
    "character and cannot be stored; write the character itself, or a complete surrogate "
    "pair, and submit again"
)


# --- problems ------------------------------------------------------------------------


def _reject(field_name: str, problem: str) -> ProposalInvalidError:
    return ProposalInvalidError([Problem(None, field_name, problem)])


# --- decoding ------------------------------------------------------------------------


class _DuplicateKeyError(ValueError):
    """A JSON object in the proposal repeats a key; the argument is the key."""


class _BadConstantError(ValueError):
    """The proposal holds ``NaN``, ``Infinity`` or ``-Infinity``; the argument is which."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise _BadConstantError(name)


def decode(data: bytes) -> dict[str, Any]:
    """Parse proposal file bytes (``utf-8-sig``) into its JSON object.

    A UTF-16 or UTF-32 file gets an explicit "save as UTF-8" problem instead of a
    JSON error; so does a file that is not UTF-8. Duplicate keys, ``NaN`` and
    ``Infinity`` are rejected, as is a top level that is not an object.
    """

    if data.startswith(_UTF16_BOMS):
        raise _reject(
            FILE_FIELD,
            f"the file is UTF-16 (it starts with a UTF-16 byte-order mark); {SAVE_AS_UTF8}",
        )
    if b"\x00" in data:
        raise _reject(
            FILE_FIELD,
            f"the file holds NUL bytes, as UTF-16 or UTF-32 text does; {SAVE_AS_UTF8}",
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _reject(
            FILE_FIELD, f"the file is not UTF-8 (byte {error.start} is invalid); {SAVE_AS_UTF8}"
        ) from error
    try:
        document = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except _DuplicateKeyError as error:
        raise _reject(
            FILE_FIELD, f"the key {error.args[0]!r} appears twice in one object"
        ) from error
    except _BadConstantError as error:
        raise _reject(
            FILE_FIELD, f"{error.args[0]} is not a JSON number; use a finite number or null"
        ) from error
    except json.JSONDecodeError as error:
        raise _reject(
            FILE_FIELD,
            f"the file is not valid JSON: {error.msg} at line {error.lineno} column {error.colno}",
        ) from error
    except (ValueError, RecursionError) as error:  # an over-long integer, deep nesting
        detail = one_line(str(error))[:200] or type(error).__name__
        raise _reject(FILE_FIELD, f"the file is not valid JSON: {detail}") from error
    if not isinstance(document, dict):
        raise _reject(FILE_FIELD, "the proposal must be one JSON object")
    return document


def read_file(path: Path) -> bytes:
    """The bytes of the proposal file. An unreadable file, or one larger than
    ``PROPOSAL_FILE_MAX_BYTES`` (read no further than that), is a ``file`` problem."""

    try:
        with path.open("rb") as handle:
            data = handle.read(PROPOSAL_FILE_MAX_BYTES + 1)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise _reject(FILE_FIELD, f"cannot read the proposal file {path}: {reason}") from error
    if len(data) > PROPOSAL_FILE_MAX_BYTES:
        raise _reject(
            FILE_FIELD,
            f"the file is larger than PROPOSAL_FILE_MAX_BYTES ({PROPOSAL_FILE_MAX_BYTES} bytes), "
            "far more than any valid proposal holds; submit the proposal file the skill wrote",
        )
    return data


# --- validation ----------------------------------------------------------------------


@dataclass(frozen=True)
class ValidTip:
    """A checked tip, ready to insert, and the lists it goes on."""

    tip: tip_store.NewTip
    list_ids: tuple[int, ...]


@dataclass(frozen=True)
class ValidProposal:
    """A proposal that passed every check: its source, mode and tips to import."""

    source: source_store.SourceRow
    mode: str
    tips: tuple[ValidTip, ...]


class _Checker:
    """Collects every problem, so one submission reports all of them."""

    def __init__(self) -> None:
        self.problems: list[Problem] = []
        self.references: list[tuple[int, str, int]] = []
        """``(tip index, field, tip id)`` of every well-formed duplicate reference,
        checked against the stored tips in one query once all tips are read."""

    def add(self, index: int | None, field_name: str, problem: str) -> None:
        self.problems.append(Problem(index, field_name, problem))

    # Each typed reader returns the value when it is valid, else records a problem
    # and returns None (or a default) so later checks can skip what depends on it.

    def text(
        self,
        obj: dict[str, Any],
        index: int | None,
        name: str,
        limit: tuple[str, int],
        *,
        optional: bool = False,
    ) -> str | None:
        """A non-blank, one-line string of at most ``limit`` characters, given as
        ``(constant name, value)``. With ``optional``, null (or no key) is accepted
        and returned as None."""

        value = obj.get(name)
        if value is None and optional:
            return None
        if not isinstance(value, str):
            self.add(index, name, "must be a string or null" if optional else "must be a string")
            return None
        if not value.strip():
            none_hint = "; use null when there is none" if optional else ""
            self.add(index, name, f"must not be empty{none_hint}")
            return None
        if len(value) > limit[1]:  # first, so the character scan below stays short
            self.add(
                index, name, f"is {len(value)} characters; {limit[0]} is {limit[1]}; shorten it"
            )
            return None
        if any(unicodedata.category(char) in _LINE_BREAKING for char in value):
            self.add(index, name, "must be one line, without line breaks, tabs or control codes")
            return None
        return value

    def optional_id(self, obj: dict[str, Any], index: int, name: str) -> int | None:
        value = obj.get(name)
        if value is None:
            return None
        if not _is_id(value):
            self.add(index, name, "must be a tip id (a positive whole number) or null")
            return None
        self.references.append((index, name, int(value)))
        return int(value)


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= ID_MAX


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_http_url(value: str) -> bool:
    if not value or any(char.isspace() for char in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _fields(
    checker: _Checker,
    obj: dict[str, Any],
    index: int | None,
    required: Sequence[str],
    allowed: Sequence[str],
    what: str,
) -> None:
    for key in obj:
        if key not in allowed:
            checker.add(index, key, f"is not a {what} field; the fields are {', '.join(allowed)}")
    for key in required:
        if key not in obj:
            checker.add(index, key, "is required")


@dataclass(frozen=True)
class _Envelope:
    """What the top-level checks read: source, loaded packet and mode; ``None`` where one failed."""

    source: source_store.SourceRow | None
    loaded: context.SourcePacket | None
    mode: str | None


def _refuse_reviewed(conn: sqlite3.Connection, document: dict[str, Any]) -> None:
    ref = document.get("source_ref")
    if isinstance(ref, str) and not _has_surrogate(ref):  # else a source_ref problem
        source_store.refuse_reviewed(conn, ref)


def _has_surrogate(text: str) -> bool:
    return _SURROGATE.search(text) is not None


def _strings(value: Any) -> Iterator[str]:
    """Every string in a decoded JSON value, object keys included, at any depth."""

    pending = [value]
    while pending:  # a loop, not recursion: decode() accepts nesting near the recursion limit
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _holds_surrogate(*values: Any) -> bool:
    return any(_has_surrogate(text) for value in values for text in _strings(value))


def _shown_key(key: str) -> str:
    """A key as a printable field name: an unpaired surrogate is shown as ``\\udXXX``."""

    return key.encode("utf-8", "backslashreplace").decode("utf-8")


def _unpaired_surrogates(document: dict[str, Any]) -> list[Problem]:
    """One problem per envelope or tip field whose key or value holds an unpaired
    surrogate. Checked first, because no later check may store, query or echo one."""

    problems: list[Problem] = []
    for key, value in document.items():
        if key == "tips" and isinstance(value, list):
            for index, tip in enumerate(value):
                if isinstance(tip, dict):
                    problems.extend(
                        Problem(index, _shown_key(tip_key), UNPAIRED_SURROGATE)
                        for tip_key, tip_value in tip.items()
                        if _holds_surrogate(tip_key, tip_value)
                    )
                elif _holds_surrogate(tip):
                    problems.append(Problem(index, TIP_FIELD, UNPAIRED_SURROGATE))
        elif _holds_surrogate(key, value):
            problems.append(Problem(None, _shown_key(key), UNPAIRED_SURROGATE))
    return problems


def _check_envelope(
    checker: _Checker, conn: sqlite3.Connection, data_dir: Path, document: dict[str, Any]
) -> _Envelope:
    _fields(checker, document, None, ENVELOPE_FIELDS, ENVELOPE_FIELDS, "proposal")
    if "schema" in document and document["schema"] != PROPOSAL_SCHEMA:
        checker.add(None, "schema", f"must be {PROPOSAL_SCHEMA!r}")

    host = document.get("host")
    if "host" in document and host not in PROPOSAL_HOSTS:
        checker.add(None, "host", f"must be one of {', '.join(PROPOSAL_HOSTS)}")
    if "model" in document:
        model = document["model"]
        if not isinstance(model, str) or not model.strip():
            checker.add(None, "model", "must be the model name the host reports (a string)")

    mode = document.get("mode")
    if "mode" in document and mode not in PROPOSAL_MODES:
        checker.add(None, "mode", f"must be one of {', '.join(PROPOSAL_MODES)}")
        mode = None
    degraded = document.get("degraded")
    if "degraded" in document:
        if not isinstance(degraded, bool):
            checker.add(None, "degraded", "must be true or false")
        elif degraded and mode is not None and mode != SOURCE_MODE_INVESTIGATE:
            checker.add(
                None, "degraded", f"may be true only in {SOURCE_MODE_INVESTIGATE} mode; use false"
            )

    tips = document.get("tips")
    if "tips" in document:
        if not isinstance(tips, list):
            checker.add(None, "tips", "must be a list of tips (it may be empty)")
        elif isinstance(mode, str) and len(tips) > tips_max(mode):
            checker.add(
                None, "tips", f"holds {len(tips)} tips; {mode} mode allows at most {tips_max(mode)}"
            )

    source, loaded = _check_source(checker, conn, data_dir, document)
    for name in ("packet_sha256", "skill_sha256"):
        if name in document and not _is_sha256(document[name]):
            checker.add(None, name, "must be 64 hex digits, copied from `checklist-care context`")
    packet_hash = document.get("packet_sha256")
    if loaded is not None and _is_sha256(packet_hash) and packet_hash.lower() != loaded.sha256:
        checker.add(
            None,
            "packet_sha256",
            f"does not match the current packet of {loaded.source.ref}, which was fetched again "
            "after this proposal was made; run `checklist-care context --source-ref "
            f"'{loaded.source.ref}'` again, read the packet again and redo the proposal",
        )
    skill_hash = document.get("skill_sha256")
    if _is_sha256(skill_hash) and skill_hash.lower() != context.skill_sha256():
        checker.add(
            None,
            "skill_sha256",
            "does not match the current skill fingerprint: the skill files changed after this "
            "proposal was made; reload the skill, run `checklist-care context` again and redo "
            "the proposal",
        )
    return _Envelope(source=source, loaded=loaded, mode=mode if isinstance(mode, str) else None)


def _is_sha256(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _check_source(
    checker: _Checker, conn: sqlite3.Connection, data_dir: Path, document: dict[str, Any]
) -> tuple[source_store.SourceRow | None, context.SourcePacket | None]:
    if "source_ref" not in document:
        return None, None
    ref = document["source_ref"]
    if not isinstance(ref, str) or not ref:
        checker.add(
            None, "source_ref", "must be the source_ref that `checklist-care fetch` printed"
        )
        return None, None
    source = source_store.get_by_ref(conn, ref)
    if source is None:
        checker.add(None, "source_ref", f"no source {ref} exists; run `checklist-care fetch` first")
        return None, None
    if source.kind == SOURCE_KIND_MANUAL:
        checker.add(
            None, "source_ref", "the manual source holds your own tips and takes no proposal"
        )
        return None, None
    try:
        loaded = context.load_source_packet(conn, data_dir, ref)
    except ConfigError as error:
        checker.add(None, "source_ref", error.message)
        return source, None
    return source, loaded


def _check_start(
    checker: _Checker, index: int, obj: dict[str, Any], envelope: _Envelope
) -> int | None:
    value = obj.get("start_s")
    if envelope.source is None or envelope.loaded is None:
        return None  # the envelope problem already rejects the proposal
    packet = envelope.loaded.packet
    if envelope.source.kind != SOURCE_KIND_YOUTUBE:
        if value is not None:
            checker.add(
                index,
                "start_s",
                f"must be null: a {envelope.source.kind} source has no time positions",
            )
        return None
    if value is None:
        if packet.chapters:
            checker.add(
                index,
                "start_s",
                "is required: this video has chapters, so give the whole second the tip's "
                "chapter (or the advice) starts at",
            )
        return None
    if not _is_whole(value):
        checker.add(index, "start_s", "must be a whole number of seconds")
        return None
    duration = packet.duration_s
    # START_S_MAX applies with or without a duration: an unbounded int would overflow
    # SQLite INTEGER on insert. The tighter of the two bounds is the one reported.
    if value < 0 or value > START_S_MAX or (duration is not None and value >= duration):
        if duration is not None and duration <= START_S_MAX:
            bound = f"0 <= start_s < duration_s ({duration})"
        else:
            bound = f"0 <= start_s <= START_S_MAX ({START_S_MAX})"
        checker.add(index, "start_s", f"is {value}; it must be {bound}")
        return None
    return int(value)


def _check_list_ids(checker: _Checker, index: int, obj: dict[str, Any]) -> list[int] | None:
    value = obj.get("list_ids")
    if not isinstance(value, list) or not all(_is_id(item) for item in value):
        checker.add(index, "list_ids", "must be a list of list ids (whole numbers), such as [5, 6]")
        return None
    return [int(item) for item in value]


@dataclass(frozen=True)
class _TipDraft:
    """One tip that passed its own checks, before its ids are checked against stored tips."""

    index: int
    tip: tip_store.NewTip
    list_ids: tuple[int, ...]


def _check_tip(
    checker: _Checker,
    index: int,
    obj: Any,
    envelope: _Envelope,
    lists: dict[int, list_store.ListRow],
) -> _TipDraft | None:
    if not isinstance(obj, dict):
        checker.add(index, TIP_FIELD, "must be a JSON object")
        return None
    before = len(checker.problems)
    _fields(checker, obj, index, TIP_REQUIRED, TIP_FIELDS, "tip")
    name = checker.text(obj, index, "name", ("NAME_MAX", NAME_MAX)) if "name" in obj else None
    action = (
        checker.text(obj, index, "action", ("ACTION_MAX", ACTION_MAX)) if "action" in obj else None
    )
    if name is not None and action is not None and line_length(name, action) > LINE_MAX:
        checker.add(
            index,
            "action",
            f"the printed line `name{LINE_SEPARATOR}action` is {line_length(name, action)} "
            f"characters; LINE_MAX is {LINE_MAX}; shorten the action (or the name)",
        )
    reason = (
        checker.text(obj, index, "reason", ("REASON_MAX", REASON_MAX)) if "reason" in obj else None
    )

    bucket = obj.get("bucket")
    if "bucket" in obj and (not _is_whole(bucket) or bucket not in BUCKET_IDS):
        checker.add(index, "bucket", f"must be one of {', '.join(map(str, BUCKET_IDS))}")
        bucket = None
    audience = obj.get("audience")
    if "audience" in obj and audience not in AUDIENCES_TIP:
        checker.add(index, "audience", f"must be one of {', '.join(AUDIENCES_TIP)}")
        audience = None

    list_ids = _check_list_ids(checker, index, obj) if "list_ids" in obj else None
    if list_ids is not None and isinstance(bucket, int):
        for problem in placement.placement_problems(
            bucket=bucket,
            audience=audience if isinstance(audience, str) else None,
            list_ids=list_ids,
            lists=lists,
        ):
            checker.add(index, "list_ids", problem)

    duplicate_of = checker.optional_id(obj, index, "duplicate_of")
    if duplicate_of is not None and isinstance(bucket, int) and bucket != BUCKET_LEFT_OUT:
        checker.add(
            index,
            "duplicate_of",
            f"is set, which requires bucket {BUCKET_LEFT_OUT} (left out); this tip is in bucket "
            f"{bucket}. Use possible_duplicate_of for a near match that keeps its bucket",
        )
    possible = checker.optional_id(obj, index, "possible_duplicate_of")
    start_s = _check_start(checker, index, obj, envelope)
    locator = checker.text(obj, index, "locator", ("LOCATOR_MAX", LOCATOR_MAX), optional=True)
    note = checker.text(obj, index, "note", ("NOTE_MAX", NOTE_MAX), optional=True)
    evidence_url = checker.text(
        obj, index, "evidence_url", ("EVIDENCE_URL_MAX", EVIDENCE_URL_MAX), optional=True
    )
    if evidence_url is not None and not _is_http_url(evidence_url):
        checker.add(index, "evidence_url", "must be an http(s) URL or null")
        evidence_url = None

    if len(checker.problems) != before:
        return None
    if (
        name is None
        or action is None
        or reason is None
        or not isinstance(bucket, int)
        or not isinstance(audience, str)
        or list_ids is None
    ):  # pragma: no cover - each reader that returns None records a problem first
        raise RuntimeError(f"tip {index} has no problems, yet a required field was not read")
    return _TipDraft(
        index=index,
        tip=tip_store.NewTip(
            name=name,
            action=action,
            reason=reason,
            bucket=bucket,
            audience=audience,
            start_s=start_s,
            locator=locator,
            evidence_url=evidence_url,
            duplicate_of=duplicate_of,
            possible_duplicate_of=possible,
            note=note,
        ),
        list_ids=tuple(list_ids),
    )


def validate(document: Any, conn: sqlite3.Connection, data_dir: Path) -> ValidProposal:
    """Check a decoded proposal against the current database, packet and skill.

    Raises ``AlreadyReviewedError`` (exit 3) for a reviewed source, else
    :class:`ProposalInvalidError` with every problem found. Reads only.
    """

    if not isinstance(document, dict):
        raise _reject(FILE_FIELD, "the proposal must be one JSON object")
    _refuse_reviewed(conn, document)
    unpaired = _unpaired_surrogates(document)
    if unpaired:
        raise ProposalInvalidError(unpaired)
    checker = _Checker()
    envelope = _check_envelope(checker, conn, data_dir, document)
    tips = document.get("tips")
    drafts: list[_TipDraft] = []
    if isinstance(tips, list):
        lists = list_store.lists_by_id(conn)
        for index, obj in enumerate(tips):
            draft = _check_tip(checker, index, obj, envelope, lists)
            if draft is not None:
                drafts.append(draft)
        _check_references(checker, conn)
    if checker.problems:
        raise ProposalInvalidError(checker.problems)
    if envelope.source is None or envelope.mode is None:  # pragma: no cover - both are problems
        raise RuntimeError("the proposal has no problems, yet its source or mode was not read")
    return ValidProposal(
        source=envelope.source,
        mode=envelope.mode,
        tips=tuple(ValidTip(tip=draft.tip, list_ids=draft.list_ids) for draft in drafts),
    )


def _check_references(checker: _Checker, conn: sqlite3.Connection) -> None:
    """``duplicate_of`` and ``possible_duplicate_of`` must name stored tips."""

    existing = tip_store.existing_ids(conn, (tip_id for _, _, tip_id in checker.references))
    for index, name, tip_id in checker.references:
        if tip_id not in existing:
            checker.add(
                index,
                name,
                f"names tip #{tip_id}, which does not exist; use an id from the tips list of "
                "`checklist-care context`",
            )


# --- import --------------------------------------------------------------------------


def archive_path(data_dir: Path, ref: str) -> Path:
    """``<data-dir>/proposals/<source_key(ref)>.1.json``."""

    return data_dir / paths.PROPOSALS_DIR / f"{paths.source_key(ref)}{ARCHIVE_SUFFIX}"


class ArchiveWrite:
    """Put the archived proposal in place in step with the import transaction.

    :meth:`install`, the transaction's last step, writes the bytes to a
    temporary sibling and renames it to the archive path. If the block raises
    after that (the ``COMMIT`` failed), the archive is removed again, so it
    exists exactly when the source is stored as reviewed::

        with ArchiveWrite(data) as archive:
            with connection.transaction(conn):
                ...  # validate and insert
                archive.install(target)
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._target: Path | None = None
        self._temporary: Path | None = None

    def __enter__(self) -> ArchiveWrite:
        return self

    def install(self, target: Path) -> None:
        """Write the archive at ``target``, which must not exist yet."""

        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        self._temporary = temporary
        try:
            temporary.write_bytes(self._data)
            os.replace(temporary, target)
        except OSError as error:
            raise ConfigError(f"cannot write the archived proposal {target}: {error}") from error
        self._target = target

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None and self._target is not None:
                self._target.unlink(missing_ok=True)
                self._target = None
        finally:
            if self._temporary is not None:
                with suppress(OSError):
                    self._temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ImportedTip:
    """One stored tip and what the import summary reports about it."""

    id: int
    name: str
    bucket: int
    list_ids: tuple[int, ...]
    duplicate_of: int | None
    possible_duplicate_of: int | None


@dataclass(frozen=True)
class ImportResult:
    """What one accepted proposal stored, and its summary lines."""

    source_ref: str
    mode: str
    archive: Path
    tips: tuple[ImportedTip, ...]

    def summary_lines(self) -> list[str]:
        """Per-bucket counts, duplicates, possible duplicates, unplaced daily tips,
        then the review URL (plan §4.5)."""

        per_bucket = ", ".join(
            f"bucket {bucket}: {sum(1 for tip in self.tips if tip.bucket == bucket)}"
            for bucket in BUCKET_IDS
        )
        duplicates = [
            f"#{tip.id} of #{tip.duplicate_of}" for tip in self.tips if tip.duplicate_of is not None
        ]
        possible = [
            f"#{tip.id} near #{tip.possible_duplicate_of}"
            for tip in self.tips
            if tip.possible_duplicate_of is not None
        ]
        unplaced = [
            f"#{tip.id}" for tip in self.tips if tip.bucket == BUCKET_DAILY and not tip.list_ids
        ]
        return [
            f"proposal: {self.archive}",
            f"source_ref: {self.source_ref}",
            f"mode: {self.mode}",
            f"tips: {len(self.tips)} pending ({per_bucket})",
            _counted("duplicates", duplicates),
            _counted("possible duplicates", possible),
            _counted("unplaced daily tips", unplaced),
            f"Review: {REVIEW_URL}",
        ]


def _counted(label: str, items: Sequence[str]) -> str:
    return f"{label}: {len(items)}" + (f" ({', '.join(items)})" if items else "")


def _insert(conn: sqlite3.Connection, valid: ValidProposal, *, now: str) -> Iterator[ImportedTip]:
    for item in valid.tips:
        tip_id = tip_store.insert_tip(
            conn, item.tip, source_id=valid.source.id, status=TIP_STATUS_PENDING, now=now
        )
        for list_id in item.list_ids:
            list_store.append_item(conn, list_id, tip_id)
        yield ImportedTip(
            id=tip_id,
            name=item.tip.name,
            bucket=item.tip.bucket,
            list_ids=item.list_ids,
            duplicate_of=item.tip.duplicate_of,
            possible_duplicate_of=item.tip.possible_duplicate_of,
        )


def submit(conn: sqlite3.Connection, data_dir: Path, file: Path) -> ImportResult:
    """Decode, validate and import the proposal at ``file`` in one transaction.

    The archive gets exactly the bytes that were validated. Raises
    :class:`ProposalInvalidError` (exit 5) or ``AlreadyReviewedError`` (exit 3) with
    nothing written; ``ConfigError`` (exit 2) when the archive cannot be written
    or already exists for an unreviewed source (moving it away is the
    operator's call).
    """

    data = read_file(file)
    document = decode(data)
    now = connection.format_timestamp(connection.utc_now())
    with ArchiveWrite(data) as archive:
        with connection.transaction(conn):
            valid = validate(document, conn, data_dir)
            target = archive_path(data_dir, valid.source.ref)
            if target.exists():
                raise ConfigError(
                    f"{target} already exists, but {valid.source.ref} is not reviewed; move "
                    "that file out of proposals/ and submit again"
                )
            imported = tuple(_insert(conn, valid, now=now))
            source_store.mark_reviewed(conn, valid.source.id, mode=valid.mode, reviewed_at=now)
            archive.install(target)
    return ImportResult(source_ref=valid.source.ref, mode=valid.mode, archive=target, tips=imported)
