"""``skill_sha256`` (plan §4.5): the fingerprint of the four skill files."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from checklist_care import context

FILES = (
    "skills/checklist-ingest/core.md",
    "skills/_shared/proposal-contract.md",
    ".claude/skills/checklist-ingest/SKILL.md",
    ".agents/skills/checklist-ingest/SKILL.md",
)


def write(root: Path, relative: str, data: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def expected(parts: list[tuple[str, bytes | None]]) -> str:
    """The plan's definition, computed independently of the implementation."""

    stream = b""
    for relative, data in parts:
        stream += relative.encode("utf-8")
        stream += b"<absent>" if data is None else b"\0" + data
    return hashlib.sha256(stream).hexdigest()


def test_the_four_files_in_the_plan_order() -> None:
    assert context.SKILL_FILES == FILES


def test_the_repo_root_is_the_checkout() -> None:
    assert (context.REPO_ROOT / "pyproject.toml").is_file()
    assert (context.REPO_ROOT / "src" / "checklist_care" / "context.py").is_file()


def test_absent_files_hash_as_path_plus_absent(tmp_path: Path) -> None:
    value = context.skill_sha256(tmp_path)
    assert value == expected([(relative, None) for relative in FILES])
    assert len(value) == 64 and value == value.lower()


def test_present_files_hash_path_nul_and_bytes(tmp_path: Path) -> None:
    write(tmp_path, FILES[0], b"core\n")
    write(tmp_path, FILES[2], b"claude adapter\n")
    assert context.skill_sha256(tmp_path) == expected(
        [(FILES[0], b"core\n"), (FILES[1], None), (FILES[2], b"claude adapter\n"), (FILES[3], None)]
    )


def test_line_endings_are_normalized_to_lf(tmp_path: Path) -> None:
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    for relative in FILES:
        write(lf, relative, b"line one\nline two\n")
        write(crlf, relative, b"line one\r\nline two\r\n")
    assert context.skill_sha256(lf) == context.skill_sha256(crlf)


@pytest.mark.parametrize("relative", FILES)
def test_editing_any_file_changes_the_fingerprint(tmp_path: Path, relative: str) -> None:
    for name in FILES:
        write(tmp_path, name, b"text\n")
    before = context.skill_sha256(tmp_path)
    write(tmp_path, relative, b"text, edited\n")
    assert context.skill_sha256(tmp_path) != before


def test_an_empty_file_differs_from_an_absent_one(tmp_path: Path) -> None:
    absent = context.skill_sha256(tmp_path)
    write(tmp_path, FILES[1], b"")
    assert context.skill_sha256(tmp_path) != absent


def test_the_default_root_is_read_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, FILES[0], b"core\n")
    monkeypatch.setattr(context, "REPO_ROOT", tmp_path)
    assert context.skill_sha256() == context.skill_sha256(tmp_path)
