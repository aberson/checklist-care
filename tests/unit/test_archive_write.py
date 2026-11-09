"""``proposal.ArchiveWrite``: the archived proposal exists exactly when the import commits."""

from __future__ import annotations

from pathlib import Path

import pytest

from checklist_care import proposal
from checklist_care.errors import ConfigError


def test_a_successful_block_keeps_the_archive_and_no_temporary(tmp_path: Path) -> None:
    target = tmp_path / "youtube-x.1.json"
    with proposal.ArchiveWrite(b"bytes") as archive:
        archive.install(target)
    assert target.read_bytes() == b"bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == [target.name]


def test_an_error_after_install_removes_the_archive(tmp_path: Path) -> None:
    target = tmp_path / "youtube-x.1.json"
    with pytest.raises(RuntimeError), proposal.ArchiveWrite(b"bytes") as archive:
        archive.install(target)
        assert target.exists()
        raise RuntimeError("COMMIT failed")
    assert list(tmp_path.iterdir()) == []


def test_an_error_before_install_touches_nothing(tmp_path: Path) -> None:
    existing = tmp_path / "youtube-x.1.json"
    existing.write_bytes(b"older")
    with pytest.raises(RuntimeError), proposal.ArchiveWrite(b"bytes"):
        raise RuntimeError("validation failed")
    assert existing.read_bytes() == b"older"


def test_an_unwritable_archive_is_a_config_error(tmp_path: Path) -> None:
    target = tmp_path / "missing-dir" / "youtube-x.1.json"
    with pytest.raises(ConfigError), proposal.ArchiveWrite(b"bytes") as archive:
        archive.install(target)
    assert not target.parent.exists()
