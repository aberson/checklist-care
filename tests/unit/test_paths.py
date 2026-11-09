"""Data-directory resolution, git-checkout refusal and source keys (plan §3.1)."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from checklist_care import paths
from checklist_care.errors import ConfigError, DataInRepoError


def _abs(path: Path | str) -> Path:
    return Path(os.path.abspath(path))


# --- resolution order ----------------------------------------------------------------


def test_flag_wins_over_env(tmp_path: Path) -> None:
    env = {paths.DATA_DIR_ENV: str(tmp_path / "env"), "LOCALAPPDATA": str(tmp_path / "lad")}
    resolved = paths.resolve_data_dir(str(tmp_path / "flag"), env, windows=True)
    assert resolved == paths.DataDir(_abs(tmp_path / "flag"), "flag")
    assert resolved.explicit


def test_env_wins_over_default(tmp_path: Path) -> None:
    env = {paths.DATA_DIR_ENV: str(tmp_path / "env"), "LOCALAPPDATA": str(tmp_path / "lad")}
    resolved = paths.resolve_data_dir(None, env, windows=True)
    assert resolved == paths.DataDir(_abs(tmp_path / "env"), "env")
    assert resolved.explicit


def test_windows_default_is_localappdata(tmp_path: Path) -> None:
    resolved = paths.resolve_data_dir(None, {"LOCALAPPDATA": str(tmp_path)}, windows=True)
    assert resolved == paths.DataDir(_abs(tmp_path / "checklist-care"), "default")
    assert not resolved.explicit


def test_windows_default_without_localappdata_uses_home(tmp_path: Path) -> None:
    resolved = paths.resolve_data_dir(None, {}, windows=True, home=tmp_path)
    assert resolved.path == _abs(tmp_path / "AppData" / "Local" / "checklist-care")


def test_posix_default_is_xdg_data_home(tmp_path: Path) -> None:
    resolved = paths.resolve_data_dir(None, {"XDG_DATA_HOME": str(tmp_path)}, windows=False)
    assert resolved.path == _abs(tmp_path / "checklist-care")


def test_posix_default_without_xdg_uses_local_share(tmp_path: Path) -> None:
    resolved = paths.resolve_data_dir(None, {"XDG_DATA_HOME": ""}, windows=False, home=tmp_path)
    assert resolved.path == _abs(tmp_path / ".local" / "share" / "checklist-care")


def test_relative_flag_becomes_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = paths.resolve_data_dir("rel/data", {})
    assert resolved.path.is_absolute()
    assert resolved.path == _abs(tmp_path / "rel" / "data")


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_env_var_is_a_config_error(value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        paths.resolve_data_dir(None, {paths.DATA_DIR_ENV: value})
    assert caught.value.code == "CC_E_CONFIG"
    assert caught.value.exit_code == 2
    assert paths.DATA_DIR_ENV in caught.value.message


def test_empty_flag_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        paths.resolve_data_dir("", {})


def test_db_path_is_inside_the_data_dir(tmp_path: Path) -> None:
    resolved = paths.resolve_data_dir(str(tmp_path), {})
    assert resolved.db_path == _abs(tmp_path) / "checklist-care.db"


# --- git-checkout refusal ------------------------------------------------------------


def test_plain_directory_is_accepted(tmp_path: Path) -> None:
    assert paths.find_git_marker(tmp_path / "data") is None
    paths.reject_git_checkout(tmp_path / "data")


@pytest.mark.parametrize("marker_kind", ["dir", "file"])
@pytest.mark.parametrize("depth", [0, 1, 3])
def test_any_git_marker_at_or_above_is_refused(
    tmp_path: Path, marker_kind: str, depth: int
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = repo / ".git"
    if marker_kind == "dir":
        marker.mkdir()
    else:  # a linked worktree or submodule
        marker.write_text("gitdir: elsewhere\n", encoding="utf-8")
    data_dir = repo.joinpath(*["sub"] * depth) if depth else repo
    assert paths.find_git_marker(data_dir) == marker
    with pytest.raises(DataInRepoError) as caught:
        paths.reject_git_checkout(data_dir)
    assert caught.value.code == "CC_E_DATA_IN_REPO"
    assert caught.value.exit_code == 2
    if depth:
        assert not data_dir.exists()  # the walk also covers directories not created yet


def test_this_checkout_is_refused() -> None:
    checkout = Path(__file__).resolve().parents[2]
    with pytest.raises(DataInRepoError):
        paths.reject_git_checkout(checkout / "private-data")


def test_symlink_into_a_checkout_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "data").mkdir()
    link = tmp_path / "outside" / "link"
    link.parent.mkdir()
    try:
        link.symlink_to(repo / "data", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")
    with pytest.raises(DataInRepoError):
        paths.reject_git_checkout(link)


# --- layout --------------------------------------------------------------------------


def test_ensure_layout_creates_every_subdirectory(tmp_path: Path) -> None:
    data_dir = tmp_path / "deep" / "data"
    paths.ensure_layout(data_dir)
    paths.ensure_layout(data_dir)  # idempotent
    assert sorted(p.name for p in data_dir.iterdir()) == sorted(
        ["packets", "inbox", "proposals", "backups", "eval"]
    )


def test_ensure_layout_on_a_file_is_a_config_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        paths.ensure_layout(blocker)


def test_file_stamp_is_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert paths.file_stamp(datetime(2026, 11, 8, 14, 5, 9, tzinfo=plus_two)) == "20261108-120509"


def test_backup_path_never_reuses_a_name(tmp_path: Path) -> None:
    moment = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
    (tmp_path / "backups").mkdir()
    first = paths.backup_path(tmp_path, moment)
    assert first == tmp_path / "backups" / "checklist-care-20261108-120000.db"
    first.write_bytes(b"")
    second = paths.backup_path(tmp_path, moment)
    assert second == tmp_path / "backups" / "checklist-care-20261108-120000-2.db"
    second.write_bytes(b"")
    assert paths.backup_path(tmp_path, moment).name == "checklist-care-20261108-120000-3.db"


# --- source_key ----------------------------------------------------------------------

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")

# Each <h8> below is a literal, computed once as sha256(ref encoded as UTF-8)[:8] and
# hard-coded, so any change to production's digest recipe (hash, encoding, prefix length)
# fails here. _TEXT_DIGEST is sha256(b"some pasted text"), a realistic text: ref body.
_TEXT_DIGEST = "309fc4161264af7120c9abba1c32d5c61e9e9a42b35cb8bd3d256f413ea974a9"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("youtube:LpeDuC2sQ-w", "youtube-LpeDuC2sQ-w-64034214"),
        ("wiki:en:The_Happiness_Hypothesis", "wiki-en_The_Happiness_Hypothesis-6231242a"),
        ("manual", "manual-36bde66f"),
        ("wiki:en:Ünïcödé_Tïtle", "wiki-en__n_c_d__T_tle-ff7f8b67"),
    ],
)
def test_source_key_examples(ref: str, expected: str) -> None:
    assert paths.source_key(ref) == expected


def test_text_source_key_body_is_truncated_to_60() -> None:
    ref = f"text:sha256:{_TEXT_DIGEST}"
    body = ("sha256_" + _TEXT_DIGEST)[:60]
    assert paths.source_key(ref) == f"text-{body}-8a077ab9"


@pytest.mark.parametrize(
    "ref",
    [
        'text:a:b?c/d"e',
        "wiki:en:What?_Who/Why",
        'youtube:"quoted"',
        "wiki:en:a*b<c>d|e\\f",
        "wiki:en:Ünïcödé_Tïtle",
        "text: spaces\tand\nnewlines ",
    ],
)
def test_source_key_is_file_name_safe(ref: str, tmp_path: Path) -> None:
    key = paths.source_key(ref)
    assert _SAFE_KEY.match(key), key
    assert re.search(r"-[0-9a-f]{8}$", key)
    (tmp_path / f"{key}.json").write_text("{}", encoding="utf-8")  # a valid file name here


def test_source_key_replaces_each_unsafe_character() -> None:
    assert paths.source_key('wiki:en:a:b?c/d"e') == "wiki-en_a_b_c_d_e-f4f4daf8"


def test_source_key_keeps_case_and_stays_unique_ignoring_case() -> None:
    refs = ["youtube:AbCdEfGhIjK", "youtube:abcdefghijk", "youtube:ABCDEFGHIJK"]
    keys = [paths.source_key(ref) for ref in refs]
    assert keys[0].startswith("youtube-AbCdEfGhIjK-")
    assert len({key.casefold() for key in keys}) == len(refs)


def test_source_keys_are_unique_when_sanitizing_merges_refs(tmp_path: Path) -> None:
    # All of these sanitize to the same body; only the hash separates them.
    refs = [
        "text:a:b",
        "text:a?b",
        "text:a/b",
        'text:a"b',
        "text:a_b",
        "text:A_B",
        "text:a b",
        "text:" + "x" * 60 + "1",  # truncation merges these two bodies
        "text:" + "x" * 60 + "2",
    ]
    keys = [paths.source_key(ref) for ref in refs]
    assert len(set(keys)) == len(refs)
    assert len({key.casefold() for key in keys}) == len(refs)
    # Proven on the real file system too (NTFS is case-insensitive).
    for key in keys:
        (tmp_path / f"{key}.json").write_text(key, encoding="utf-8")
    assert len(list(tmp_path.iterdir())) == len(refs)


def test_source_key_is_deterministic_and_bounded() -> None:
    ref = "wiki:en:" + "Very_Long_Title_" * 20
    assert paths.source_key(ref) == paths.source_key(ref)
    assert len(paths.source_key(ref)) <= len("wiki") + 1 + 60 + 1 + 8
    long_kind = "k" * 200
    assert len(paths.source_key(long_kind)) <= 60 + 1 + 8
