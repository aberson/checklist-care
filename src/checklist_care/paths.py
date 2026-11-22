"""The private data directory (plan §3.1).

Resolution order, first hit wins: ``--data-dir``, then ``CHECKLIST_CARE_DATA_DIR``,
then ``%LOCALAPPDATA%\\checklist-care`` on Windows or
``$XDG_DATA_HOME/checklist-care`` (else ``~/.local/share/checklist-care``) elsewhere.
A directory inside a git checkout is refused. Follows
``agent-advocate/src/agent_advocate/store.py`` (``resolve_data_dir()`` and
``reject_git_worktree()``).
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from checklist_care.errors import ConfigError, DataInRepoError

APP_DIR_NAME: Final = "checklist-care"
DATA_DIR_ENV: Final = "CHECKLIST_CARE_DATA_DIR"
DB_FILENAME: Final = "checklist-care.db"

PACKETS_DIR: Final = "packets"
INBOX_DIR: Final = "inbox"
PROPOSALS_DIR: Final = "proposals"
BACKUPS_DIR: Final = "backups"
EVAL_DIR: Final = "eval"
SUBDIRECTORIES: Final = (PACKETS_DIR, INBOX_DIR, PROPOSALS_DIR, BACKUPS_DIR, EVAL_DIR)

FILE_STAMP_FORMAT: Final = "%Y%m%d-%H%M%S"
"""``<YYYYMMDD-HHMMSS>`` in file names (backups, inbox, eval results), UTC."""

SOURCE_KEY_PART_MAX: Final = 60
_UNSAFE_KEY_CHARS: Final = re.compile(r"[^A-Za-z0-9._-]")

DataDirOrigin = Literal["flag", "env", "default"]


@dataclass(frozen=True)
class DataDir:
    """A resolved data directory and where the choice came from."""

    path: Path
    origin: DataDirOrigin

    @property
    def db_path(self) -> Path:
        return self.path / DB_FILENAME

    @property
    def explicit(self) -> bool:
        """True when the flag or the environment variable chose the directory."""

        return self.origin != "default"

    def has_database(self) -> bool:
        """True when :attr:`db_path` is a non-empty file. A zero-byte file is no
        database: opening it would apply the schema without the seed rows, a silently
        empty database."""

        return self.db_path.is_file() and self.db_path.stat().st_size > 0


def resolve_data_dir(
    flag: str | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    windows: bool | None = None,
    home: Path | None = None,
) -> DataDir:
    """Choose the data directory without touching the filesystem.

    ``environ``, ``windows`` and ``home`` default to the running process and
    exist so tests can cover every branch on any platform.
    """

    env = os.environ if environ is None else environ
    if flag is not None:
        return DataDir(_absolute(flag, "--data-dir"), "flag")
    raw = env.get(DATA_DIR_ENV)
    if raw is not None:
        return DataDir(_absolute(raw, DATA_DIR_ENV), "env")
    is_windows = os.name == "nt" if windows is None else windows
    base_name = "LOCALAPPDATA" if is_windows else "XDG_DATA_HOME"
    base = env.get(base_name)
    if base:
        root = Path(base)
    else:
        home_dir = Path.home() if home is None else home
        root = home_dir / "AppData" / "Local" if is_windows else home_dir / ".local" / "share"
    return DataDir(Path(os.path.abspath(root / APP_DIR_NAME)), "default")


def _absolute(raw: str, label: str) -> Path:
    if not raw.strip():
        raise ConfigError(f"{label} is empty; give it a directory path")
    return Path(os.path.abspath(Path(raw).expanduser()))


def find_git_marker(path: Path) -> Path | None:
    """Return the first ``.git`` directory or file at ``path`` or above it.

    Both the absolute path and its ``resolve()``d form are walked, so a symlink
    or junction into a checkout is caught. A normal checkout has a ``.git``
    directory; a linked worktree or submodule has a ``.git`` file.
    """

    requested = Path(os.path.abspath(path))
    for candidate in (requested, requested.resolve(strict=False)):
        for directory in (candidate, *candidate.parents):
            marker = directory / ".git"
            if marker.is_dir() or marker.is_file():
                return marker
    return None


def reject_git_checkout(path: Path) -> None:
    """Exit 2 with ``CC_E_DATA_IN_REPO`` when ``path`` is inside a git checkout."""

    try:
        marker = find_git_marker(path)
    except OSError as error:
        raise ConfigError(f"cannot inspect data directory {path}: {error}") from error
    if marker is not None:
        raise DataInRepoError(
            f"refusing a data directory inside a git checkout: {path} ({marker} exists); "
            "choose a directory outside every repository"
        )


def ensure_layout(data_dir: Path) -> None:
    """Create the data directory and its subdirectories (idempotent)."""

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRECTORIES:
            (data_dir / name).mkdir(exist_ok=True)
    except OSError as error:
        raise ConfigError(f"cannot create data directory {data_dir}: {error}") from error


def file_stamp(moment: datetime) -> str:
    """``YYYYMMDD-HHMMSS`` of ``moment`` in UTC."""

    return moment.astimezone(UTC).strftime(FILE_STAMP_FORMAT)


def numbered_paths(directory: Path, stem: str, suffix: str) -> Iterator[Path]:
    """The names a new file in ``directory`` may take, in order and without end:
    ``<stem><suffix>``, then ``<stem>-2<suffix>``, ``<stem>-3<suffix>`` …; the caller
    takes the first one that is free."""

    yield directory / f"{stem}{suffix}"
    for counter in itertools.count(2):
        yield directory / f"{stem}-{counter}{suffix}"


def sibling_path(path: Path, suffix: str) -> Path:
    """The dot-named sibling ``.<name>.<pid><suffix>`` of ``path``: this process's
    temporary (``.tmp``) or backup (``.bak``) file next to it, hidden by its dot."""

    return path.with_name(f".{path.name}.{os.getpid()}{suffix}")


def lacks_hard_links(error: OSError) -> bool:
    """True when :func:`os.link` failed with ``error`` because the file system has no
    hard links (FAT, some shares): any ``OSError`` but ``FileNotFoundError``, which
    means the file to link is missing. A caller that tells a taken name apart catches
    ``FileExistsError`` before asking."""

    return not isinstance(error, FileNotFoundError)


def backup_path(data_dir: Path, moment: datetime) -> Path:
    """A not-yet-existing ``backups/checklist-care-<stamp>[-n].db`` path."""

    names = numbered_paths(data_dir / BACKUPS_DIR, f"{APP_DIR_NAME}-{file_stamp(moment)}", ".db")
    return next(candidate for candidate in names if not candidate.exists())


def source_key(ref: str) -> str:
    """The file-name-safe key ``<kind>-<body>-<h8>`` of a source ref.

    ``<kind>`` is the text before the first ``:``; ``<body>`` is the rest. Each
    has every character outside ``[A-Za-z0-9._-]`` replaced by ``_`` and is cut
    to 60 characters (the kind cap only matters for a malformed ref). ``<h8>`` is
    the first 8 lowercase hex digits of ``sha256(ref)``, which keeps keys unique
    when sanitizing or truncation merges two refs, and on case-insensitive NTFS.
    A ref without a body (``manual``) gives ``<kind>-<h8>``.
    """

    kind, _, rest = ref.partition(":")
    digest = hashlib.sha256(ref.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    parts = [_UNSAFE_KEY_CHARS.sub("_", kind)[:SOURCE_KEY_PART_MAX]]
    body = _UNSAFE_KEY_CHARS.sub("_", rest)[:SOURCE_KEY_PART_MAX]
    if body:
        parts.append(body)
    parts.append(digest)
    return "-".join(parts)
