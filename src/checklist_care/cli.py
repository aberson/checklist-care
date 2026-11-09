"""The ``checklist-care`` command line (also ``python -m checklist_care.cli``).

``main`` reconfigures stdout and stderr to UTF-8 before anything else, so JSON
with any character survives a Windows pipe whatever ``PYTHONIOENCODING`` is.
Every command writes ``data_dir: <path>`` as its first stderr line; stdout stays
clean for JSON. Expected failures are :class:`CheckError` subclasses and map to
their exit codes; anything else prints one ``CC_E_INTERNAL`` line and exits 9
(``--debug`` adds the traceback).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import traceback
from collections.abc import Callable, Sequence
from contextlib import closing
from typing import Any

from checklist_care import __version__, paths
from checklist_care.db import connection, seed
from checklist_care.errors import (
    CODE_INTERNAL,
    EXIT_INTERNAL,
    EXIT_OK,
    CheckError,
    ConfigError,
    one_line,
)
from checklist_care.limits import BUCKET_IDS, TIP_STATUS_PENDING
from checklist_care.sources import packet, youtube
from checklist_care.store import sources as source_store

Handler = Callable[[argparse.Namespace, paths.DataDir], int]


def _use_utf8(stream: Any, errors: str) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors=errors)


def _out(line: str) -> None:
    print(line, file=sys.stdout)


def _err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    """The argument parser. ``--data-dir`` and ``--debug`` live on a shared parent
    parser with ``default=argparse.SUPPRESS``, so they work before or after the
    subcommand."""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help=(
            f"private data directory (default: ${paths.DATA_DIR_ENV}, "
            r"else %%LOCALAPPDATA%%\checklist-care)"
        ),
    )
    common.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print a traceback for unexpected errors",
    )

    parser = argparse.ArgumentParser(
        prog="checklist-care",
        description="Turn life-advice sources into printable room checklists.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"checklist-care {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    init = commands.add_parser(
        "init", parents=[common], help="create the database and its seed rows (idempotent)"
    )
    init.set_defaults(handler=_cmd_init)

    status = commands.add_parser("status", parents=[common], help="show the data directory")
    status.add_argument("--json", action="store_true", help="print one JSON object")
    status.set_defaults(handler=_cmd_status)

    backup = commands.add_parser(
        "backup", parents=[common], help="write a consistent copy of the database"
    )
    backup.set_defaults(handler=_cmd_backup)

    fetch = commands.add_parser(
        "fetch",
        parents=[common],
        help="fetch a source into a packet under <data-dir>/packets/",
        description=(
            "Fetch a YouTube video's transcript and chapters into a source packet. "
            "Put the URL in single quotes in a shell."
        ),
    )
    fetch.add_argument("url", metavar="URL", help="a YouTube video URL or its 11-character id")
    fetch.set_defaults(handler=_cmd_fetch)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code."""

    _use_utf8(sys.stdout, "strict")
    _use_utf8(sys.stderr, "backslashreplace")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:  # --help, --version and usage errors
        code = exit_request.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else 2)
    debug = bool(getattr(args, "debug", False))
    try:
        data_dir = paths.resolve_data_dir(getattr(args, "data_dir", None))
        _err(f"data_dir: {data_dir.path}")
        paths.reject_git_checkout(data_dir.path)
        handler: Handler = args.handler
        return handler(args, data_dir)
    except CheckError as error:
        _err(error.line())
        return error.exit_code
    except Exception as error:
        if debug:
            traceback.print_exc(file=sys.stderr)
        _err(f"{CODE_INTERNAL}: unexpected {type(error).__name__}: {one_line(str(error))}")
        return EXIT_INTERNAL


def _has_database(data_dir: paths.DataDir) -> bool:
    # A zero-byte file is no database: opening it would apply the schema without
    # the seed rows, a silently empty database.
    db_path = data_dir.db_path
    return db_path.is_file() and db_path.stat().st_size > 0


def _require_database(data_dir: paths.DataDir) -> None:
    if not _has_database(data_dir):
        raise ConfigError(f"no database at {data_dir.db_path}; run `checklist-care init` first")


def _cmd_init(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    paths.ensure_layout(data_dir.path)
    existed = _has_database(data_dir)
    now = connection.format_timestamp(connection.utc_now())
    with closing(connection.connect(data_dir.db_path)) as conn:
        seed.seed_database(conn, now=now)
        version = connection.schema_version(conn)
    _out(f"db_path: {data_dir.db_path}")
    _out(f"schema_version: {version}")
    _out(f"result: {'already initialized' if existed else 'created'}")
    return EXIT_OK


def _counts(conn: sqlite3.Connection) -> dict[str, Any]:
    by_bucket = {str(bucket): 0 for bucket in BUCKET_IDS}
    for row in conn.execute("SELECT bucket, count(*) FROM tip GROUP BY bucket ORDER BY bucket"):
        by_bucket[str(row[0])] = int(row[1])
    return {
        "sources": int(conn.execute("SELECT count(*) FROM source").fetchone()[0]),
        "tips_by_bucket": by_bucket,
        "pending": int(
            conn.execute(
                "SELECT count(*) FROM tip WHERE status = ?", (TIP_STATUS_PENDING,)
            ).fetchone()[0]
        ),
        "lists": int(conn.execute("SELECT count(*) FROM list").fetchone()[0]),
    }


def _cmd_status(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    _require_database(data_dir)
    with closing(connection.connect(data_dir.db_path)) as conn:
        version = connection.schema_version(conn)
        counts = _counts(conn)
    if args.json:
        payload = {
            "data_dir": str(data_dir.path),
            "db_path": str(data_dir.db_path),
            "schema_version": version,
            "counts": counts,
        }
        _out(json.dumps(payload, ensure_ascii=False))
        return EXIT_OK
    buckets = ", ".join(f"bucket {key}: {value}" for key, value in counts["tips_by_bucket"].items())
    _out(f"data_dir: {data_dir.path}")
    _out(f"db_path: {data_dir.db_path}")
    _out(f"schema_version: {version}")
    _out(f"sources: {counts['sources']}")
    _out(f"tips: {sum(counts['tips_by_bucket'].values())} ({buckets})")
    _out(f"pending: {counts['pending']}")
    _out(f"lists: {counts['lists']}")
    return EXIT_OK


def _cmd_backup(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    _require_database(data_dir)
    paths.ensure_layout(data_dir.path)
    target = paths.backup_path(data_dir.path, connection.utc_now())
    with closing(connection.connect(data_dir.db_path)) as conn:
        conn.execute("VACUUM INTO ?", (str(target),))
    _out(f"backup: {target}")
    return EXIT_OK


def _cmd_fetch(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    video_id = youtube.parse_video_id(args.url)
    ref = youtube.source_ref(video_id)
    _require_database(data_dir)
    paths.ensure_layout(data_dir.path)
    target = packet.packet_path(data_dir.path, ref)
    with closing(connection.connect(data_dir.db_path)) as conn:
        # Refuse a reviewed source before any network call, and again under the
        # write lock in case `propose` reviewed it while the fetch ran.
        source_store.refuse_reviewed(conn, ref)
        fetched_at = connection.format_timestamp(connection.utc_now())
        fetched = youtube.fetch_packet(video_id, fetched_at=fetched_at)
        # The packet goes into place as the transaction's last step, and a failed
        # COMMIT puts the old one back, so packet and source row change together.
        with packet.PacketSwap(target, fetched) as swap:
            with connection.transaction(conn):
                source_store.refuse_reviewed(conn, ref)
                source_store.upsert_fetched(
                    conn,
                    kind=fetched.kind,
                    ref=ref,
                    title=fetched.title,
                    url=fetched.url,
                    revision=youtube.revision(fetched),
                    fetched_at=fetched_at,
                )
                swap.install()
    for warning in fetched.warnings:
        _err(f"warning: {warning}")
    _out(f"packet: {target}")
    _out(f"source_ref: {ref}")
    _out(f"packet_sha256: {packet.file_sha256(target)}")
    _out(f"counts: {fetched.counts()}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
