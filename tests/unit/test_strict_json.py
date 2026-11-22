"""``strict_json.decode``: the one bytes-to-JSON decoder of proposals and eval labels.

``proposal.decode`` and the eval labels reader (``scoring/score.py``) both wrap it, so
the same bytes get the same classification and the same message from either reader;
each only adds its own advice. Every shared-decoder defect is asserted here, through
both readers; each reader's own advice is asserted with that reader
(``tests/unit/test_scorer_rules.py`` for the labels, the propose tests for proposals).

Every reader also holds the leaf's own objects (``is``, not ``==``), so a copy re-spelled
in a reader fails here, and the regular-file gate they share is asserted once.
"""

from __future__ import annotations

import codecs
import json
from pathlib import Path
from types import ModuleType

import pytest

from checklist_care import proposal, strict_json
from checklist_care.errors import ProposalInvalidError
from checklist_care.scoring import score
from checklist_care.sources import text

DOCUMENT = '{"a": [1, 2.5, null, true], "b": {"c": "d"}}'

BAD_FILES: dict[str, tuple[bytes, str, str]] = {
    "UTF-16 LE byte-order mark": (
        codecs.BOM_UTF16_LE + DOCUMENT.encode("utf-16-le"),
        strict_json.UTF16,
        "the file is UTF-16 (it starts with a UTF-16 byte-order mark)",
    ),
    "UTF-16 BE byte-order mark": (
        codecs.BOM_UTF16_BE + DOCUMENT.encode("utf-16-be"),
        strict_json.UTF16,
        "the file is UTF-16 (it starts with a UTF-16 byte-order mark)",
    ),
    "UTF-16 without a byte-order mark": (
        DOCUMENT.encode("utf-16-le"),
        strict_json.NUL_BYTES,
        "the file holds NUL bytes, as UTF-16 or UTF-32 text does",
    ),
    "one NUL byte": (
        b'{"a": "b\x00"}',
        strict_json.NUL_BYTES,
        "the file holds NUL bytes, as UTF-16 or UTF-32 text does",
    ),
    "cp1252": (
        '{"a": "caf\xe9"}'.encode("cp1252"),
        strict_json.NOT_UTF8,
        "the file is not UTF-8 (byte 10 is invalid)",
    ),
    "repeated key": (
        b'{"outer": {"b": 1, "b": 1}}',
        strict_json.DUPLICATE_KEY,
        "the key 'b' appears twice in one object",
    ),
    "NaN": (b'{"a": [NaN]}', strict_json.BAD_CONSTANT, "NaN is not a JSON number"),
    "-Infinity": (b'{"a": -Infinity}', strict_json.BAD_CONSTANT, "-Infinity is not a JSON number"),
    "truncated": (
        b'{"a": ',
        strict_json.NOT_JSON,
        "the file is not valid JSON: Expecting value at line 1 column 7",
    ),
    "over-long integer": (b"1" * 5000, strict_json.NOT_JSON, "the file is not valid JSON: "),
    "deep nesting": (b"[" * 100_000, strict_json.NOT_JSON, "the file is not valid JSON: "),
}


def test_valid_json_decodes_like_json_loads_with_or_without_a_utf8_byte_order_mark() -> None:
    assert strict_json.decode(DOCUMENT.encode("utf-8")) == json.loads(DOCUMENT)
    assert strict_json.decode(codecs.BOM_UTF8 + DOCUMENT.encode("utf-8")) == json.loads(DOCUMENT)


@pytest.mark.parametrize("defect", sorted(BAD_FILES))
def test_each_defect_is_one_classified_error_both_readers_refuse_alike(defect: str) -> None:
    data, kind, message = BAD_FILES[defect]

    with pytest.raises(strict_json.JsonFileError) as shared:
        strict_json.decode(data)
    with pytest.raises(ProposalInvalidError) as as_proposal:
        proposal.decode(data)
    with pytest.raises(score.LabelsError) as as_labels:
        score.parse_labels(data, Path("labels.json"))

    assert shared.value.kind == kind
    assert shared.value.message.startswith(message)
    assert "\n" not in shared.value.message
    # Each reader keeps the shared message and only adds its own advice.
    (problem,) = as_proposal.value.problems
    assert problem.field == proposal.FILE_FIELD
    assert problem.problem.startswith(shared.value.message)
    assert str(as_labels.value).startswith(shared.value.message)


SHARED: tuple[tuple[ModuleType, str, str], ...] = (
    # (the reader, the name it binds, the leaf's name): proposal.py defines its own
    # decode, so both readers bind the leaf's as decode_json.
    (text, "PRIMARY_ENCODING", "PRIMARY_ENCODING"),
    (text, "UTF16_BOMS", "UTF16_BOMS"),
    (text, "is_regular_file", "is_regular_file"),
    (text, "NOT_REGULAR_FILE", "NOT_REGULAR_FILE"),
    (score, "is_regular_file", "is_regular_file"),
    (score, "NOT_REGULAR_FILE", "NOT_REGULAR_FILE"),
    (score, "read_capped", "read_capped"),
    (proposal, "read_capped", "read_capped"),
    (score, "read_error_reason", "read_error_reason"),
    (proposal, "read_error_reason", "read_error_reason"),
    (score, "is_whole", "is_whole"),
    (proposal, "is_whole", "is_whole"),
    (score, "decode_json", "decode"),
    (proposal, "decode_json", "decode"),
    (score, "JsonFileError", "JsonFileError"),
    (proposal, "JsonFileError", "JsonFileError"),
    (score, "ENCODING_KINDS", "ENCODING_KINDS"),
    (proposal, "ENCODING_KINDS", "ENCODING_KINDS"),
    (proposal, "BAD_CONSTANT", "BAD_CONSTANT"),
)


@pytest.mark.parametrize(
    ("reader", "name", "leaf_name"),
    SHARED,
    ids=[f"{reader.__name__.rsplit('.', 1)[-1]}.{name}" for reader, name, _ in SHARED],
)
def test_each_reader_holds_the_leafs_own_object_not_a_copy(
    reader: ModuleType, name: str, leaf_name: str
) -> None:
    # The reader's attribute against the owner's: a re-spelled copy is equal, not the same.
    assert reader is not strict_json
    assert getattr(reader, name) is getattr(strict_json, leaf_name)


def test_only_a_regular_file_passes_the_gate(tmp_path: Path) -> None:
    regular = tmp_path / "labels.json"
    regular.write_bytes(b"{}")

    assert strict_json.is_regular_file(regular)
    assert not strict_json.is_regular_file(tmp_path)  # a folder stands in for a device or pipe
    with pytest.raises(FileNotFoundError):
        strict_json.is_regular_file(tmp_path / "missing.json")
