"""limits.py is the single source of every text limit and enum (plan §4.2)."""

from __future__ import annotations

import ast
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from checklist_care import limits
from checklist_care.db import connection, seed


def test_limit_values_match_the_plan() -> None:
    """Pins the §4.2 numbers the plan states as a contract; no other test fixes them."""
    assert limits.NAME_MAX == 40
    assert limits.ACTION_MAX == 60
    assert limits.LINE_MAX == 80
    assert limits.REASON_MAX == 110
    assert limits.PRINT_CAPACITY == 48
    assert limits.TIPS_MAX_EXTRACT == 60
    assert limits.TIPS_MAX_INVESTIGATE == 300


def test_line_length_counts_name_separator_and_action() -> None:
    assert limits.LINE_SEPARATOR == " — "
    assert limits.line_length("Drink Water", "Drink a glass") == 11 + 3 + 13
    name = "N" * 40
    action = "A" * (limits.LINE_MAX - 40 - len(limits.LINE_SEPARATOR))
    assert limits.line_length(name, action) == limits.LINE_MAX


# --- drift guard: schema.sql CHECK constraints spell out the same enums ---------------

_CREATE_TABLE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\);", re.DOTALL)
_CHECK_IN = re.compile(r"CHECK \((\w+) IN \(([^)]*)\)\)")

EXPECTED_SCHEMA_ENUMS = {
    ("source", "kind"): "SOURCE_KINDS",
    ("source", "mode"): "SOURCE_MODES",
    ("tip", "audience"): "AUDIENCES_TIP",
    ("tip", "status"): "TIP_STATUSES",
    ("list", "audience"): "AUDIENCES_LIST",
}


def _schema_check_enums() -> dict[tuple[str, str], tuple[str, ...]]:
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for table, body in _CREATE_TABLE.findall(connection.schema_sql()):
        for column, values in _CHECK_IN.findall(body):
            found[(table, column)] = tuple(re.findall(r"'([^']*)'", values))
    return found


def test_every_schema_enum_is_mapped_to_limits() -> None:
    # A new CHECK ... IN (...) in schema.sql must be added to limits.py and to this map.
    assert set(_schema_check_enums()) == set(EXPECTED_SCHEMA_ENUMS)


@pytest.mark.parametrize(("table_column", "constant"), sorted(EXPECTED_SCHEMA_ENUMS.items()))
def test_schema_enum_equals_limits(table_column: tuple[str, str], constant: str) -> None:
    assert _schema_check_enums()[table_column] == getattr(limits, constant)


def test_schema_bucket_range_equals_bucket_ids() -> None:
    match = re.search(r"CHECK \(id BETWEEN (\d+) AND (\d+)\)", connection.schema_sql())
    assert match is not None
    low, high = int(match.group(1)), int(match.group(2))
    assert tuple(range(low, high + 1)) == limits.BUCKET_IDS


def test_schema_rejects_values_outside_the_enums() -> None:
    # Behavioural check that the parsed CHECKs are live, not just text.
    with closing(sqlite3.connect(":memory:")) as conn:
        for statement in connection.schema_statements():
            conn.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source (kind, ref, mode) VALUES ('podcast', 'podcast:1', 'extract')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO list (place, audience) VALUES ('Kitchen', ?)",
                (limits.AUDIENCES_TIP[0],),  # "kids+" is a tip audience, not a list audience
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bucket VALUES (5, 't', 'r', 'now')")


def test_seeds_use_only_enum_values() -> None:
    assert {bucket.id for bucket in seed.BUCKETS} == set(limits.BUCKET_IDS)
    assert {item.audience for item in seed.LISTS} <= set(limits.AUDIENCES_LIST)


# --- no production module restates an enum value -------------------------------------
#
# `is` cannot catch a restated short string (CPython interns it), so this reads the
# source instead. Exempt: payload field names (dict keys and subscripts, such as the
# status counts' "pending") and the manual source's ref, which is spelled like its kind.

_PACKAGE = Path(limits.__file__).parent
# Enums that no CHECK constraint spells out are guarded the same way.
_EXTRA_ENUMS = ("TRANSCRIPT_KINDS",)
_ENUM_STRINGS = frozenset(
    value
    for constant in (*EXPECTED_SCHEMA_ENUMS.values(), *_EXTRA_ENUMS)
    for value in getattr(limits, constant)
)
_EXEMPT_ASSIGNMENTS = frozenset({"MANUAL_SOURCE_REF"})


def _restated_enum_literals(source: str) -> list[str]:
    tree = ast.parse(source)
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            exempt.update(id(key) for key in node.keys if key is not None)
        elif isinstance(node, ast.Subscript):
            exempt.add(id(node.slice))
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in _EXEMPT_ASSIGNMENTS for t in targets):
                exempt.add(id(node.value))
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in exempt:
            continue
        sql_quoted = {value for value in _ENUM_STRINGS if f"'{value}'" in node.value}
        if node.value in _ENUM_STRINGS or sql_quoted:
            found.append(f"line {node.lineno}: {node.value!r}")
    return found


@pytest.mark.parametrize(
    "module",
    sorted(
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if path.name != "limits.py"
    ),
)
def test_production_module_names_enum_members_instead_of_literals(module: str) -> None:
    source = (_PACKAGE / module).read_text(encoding="utf-8")
    assert _restated_enum_literals(source) == []


def test_restated_enum_literal_detector_flags_literals() -> None:
    # Red anchor: the detector sees the shapes it forbids and spares the exempt ones.
    restated = "\n".join(
        [
            'AUDIENCE = "kids"',
            "conn.execute(\"SELECT count(*) FROM tip WHERE status = 'pending'\")",
            'payload = {"pending": counts["pending"]}',
            'MANUAL_SOURCE_REF = "manual"',
            'kind = "generated" if is_generated else other',
        ]
    )
    assert _restated_enum_literals(restated) == [
        "line 1: 'kids'",
        "line 2: \"SELECT count(*) FROM tip WHERE status = 'pending'\"",
        "line 5: 'generated'",
    ]


def test_transcript_kinds() -> None:
    assert limits.TRANSCRIPT_KINDS == ("generated", "manual")
    assert limits.TRANSCRIPT_KIND_GENERATED is limits.TRANSCRIPT_KINDS[0]
    assert limits.TRANSCRIPT_KIND_MANUAL is limits.TRANSCRIPT_KINDS[1]
