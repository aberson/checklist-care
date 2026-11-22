"""The ``checklist-care`` command line (also ``python -m checklist_care.cli``).

``main`` reconfigures stdout and stderr to UTF-8 before anything else, so JSON
with any character survives a Windows pipe whatever ``PYTHONIOENCODING`` is.
Every command writes ``data_dir: <path>`` as its first stderr line; stdout stays
clean for JSON. Expected failures are :class:`CheckError` subclasses and map to
their exit codes; anything else prints one ``CC_E_INTERNAL`` line and exits 9
(``--debug`` adds the traceback). Inside ``eval score`` every failure, from the data
dir on, is ``CC_EVAL_ABORT`` with exit 6 instead, so it never looks like an eval
verdict.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import traceback
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from checklist_care import __version__, paths, proposal
from checklist_care import context as context_module
from checklist_care.db import connection, seed
from checklist_care.errors import (
    CODE_EVAL_ABORT,
    CODE_INTERNAL,
    EXIT_EVAL_ABORT,
    EXIT_EVAL_FAIL,
    EXIT_INTERNAL,
    EXIT_OK,
    CheckError,
    ConfigError,
    EvalAbortError,
    ProposalInvalidError,
    one_line,
)
from checklist_care.limits import BUCKET_IDS, TIP_STATUS_PENDING
from checklist_care.scoring import score
from checklist_care.server import DAILY_PORT, HOST
from checklist_care.sources import packet, wiki, youtube
from checklist_care.sources import text as text_source
from checklist_care.store import sources as source_store

Handler = Callable[[argparse.Namespace, paths.DataDir], int]
UsageCheck = Callable[[argparse.Namespace], None]
"""A subcommand's check of a rule between its arguments; it calls ``parser.error``."""


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
            "Fetch a YouTube video's transcript and chapters, a Wikipedia article's text "
            "and sections, or the text of a text file, into a source packet. Put the URL "
            "in single quotes in a shell; a file path too."
        ),
    )
    source = fetch.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "url",
        metavar="URL",
        nargs="?",
        help=(
            "a YouTube video URL or its 11-character id, or a Wikipedia article URL "
            "(https://<lang>.wikipedia.org/wiki/<Title>)"
        ),
    )
    source.add_argument(
        "--text-file",
        action=_StoreOnce,
        metavar="PATH",
        help=(
            "a text file to fetch instead of a URL: UTF-8 (else read as Windows-1252), "
            f"at most {text_source.TEXT_CHARS_MAX:,} characters"
        ),
    )
    fetch.add_argument(
        "--title",
        action=_StoreOnce,
        type=_title,
        metavar="TITLE",
        help="the text's title, stored with the source (only with --text-file)",
    )
    fetch.set_defaults(handler=_cmd_fetch, usage_check=_fetch_usage_check(fetch))

    context = commands.add_parser(
        "context",
        parents=[common],
        help="print what the skill needs to propose, as one JSON object",
        description=(
            f"Print one {context_module.CONTEXT_SCHEMA} JSON object: the skill fingerprint, "
            "the text limits, the bucket rubrics, the lists and their cues, and the stored "
            "tips; with --source-ref, also that source's packet hash, duration and chapters."
        ),
    )
    context.add_argument(
        "--source-ref",
        metavar="REF",
        help="the source_ref that `checklist-care fetch` printed, such as youtube:<id>",
    )
    context.set_defaults(handler=_cmd_context)

    propose = commands.add_parser(
        "propose",
        parents=[common],
        help="validate a proposal file and import its tips as pending",
        description=(
            f"Validate a {proposal.PROPOSAL_SCHEMA} JSON file (UTF-8) and import its tips "
            "as pending, all in one transaction. Any problem exits 5, prints a JSON list "
            "of {index, field, problem} on stdout and writes nothing."
        ),
    )
    propose.add_argument("file", metavar="FILE", help="the proposal JSON file")
    propose.set_defaults(handler=_cmd_propose)

    serve = commands.add_parser(
        "serve",
        parents=[common],
        help=f"serve the review app and its API on http://{HOST}:<port>",
        description=(
            f"Serve /api/* and the built web app on http://{HOST}:<port>, always on the "
            f"loopback address {HOST}, with one worker. Stop it with Ctrl+C."
        ),
    )
    serve.add_argument(
        "--port",
        type=_port,
        default=DAILY_PORT,
        help=f"the port to listen on (default {DAILY_PORT})",
    )
    serve.add_argument(
        "--build",
        action="store_true",
        help="build the web app with npm first (npm install when web/node_modules is absent)",
    )
    serve.add_argument(
        "--demo",
        action="store_true",
        help="serve synthetic demo data from a fresh temp directory instead of the data directory",
    )
    serve.set_defaults(handler=_cmd_serve)

    evaluate = commands.add_parser(
        "eval", parents=[common], help="acceptance testing of the skill (plan §4.8)"
    )
    eval_commands = evaluate.add_subparsers(
        dest="eval_command", required=True, metavar="EVAL_COMMAND"
    )
    eval_score = eval_commands.add_parser(
        "score",
        parents=[common],
        help="score the tips the skill stored against a labels file",
        description=(
            "Score the database rows the skill wrote (never a transcript) against a "
            f"{score.LABELS_SCHEMA} labels file, and write "
            f"<data-dir>/{paths.EVAL_DIR}/{score.SCORE_FILE_PREFIX}<ts>.json. Exits "
            f"{EXIT_OK} on {score.PASS}, {EXIT_EVAL_FAIL} on {score.FAIL} and "
            f"{EXIT_EVAL_ABORT} ({CODE_EVAL_ABORT}) when the input cannot give a verdict. "
            f"Needs --data-dir or {paths.DATA_DIR_ENV}."
        ),
    )
    eval_score.add_argument(
        "--labels",
        required=True,
        metavar="FILE",
        help="the labels file, such as eval/labels/in_sample.json",
    )
    eval_score.add_argument(
        "--json", action="store_true", help="print the score as one JSON object"
    )
    eval_score.set_defaults(handler=_cmd_eval_score, eval_abort=True)
    return parser


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"{value} is not a port number from 1 to 65535")
    return value


class _StoreOnce(argparse.Action):
    """Store an option's value, but refuse the option a second time. Plain ``store``
    keeps the last value, so a title that a shell quoting slip splits into several
    arguments could carry a second ``--text-file`` that wins. No ``fetch`` option is
    meant to be repeated, so a repeat is a usage error (exit 2)."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"argument {option_string}: given more than once")
        setattr(namespace, self.dest, values)


def _title(text: str) -> str:
    try:
        return text_source.clean_title(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{error}; give one line of text") from None


def _fetch_usage_check(fetch: argparse.ArgumentParser) -> UsageCheck:
    """The ``fetch`` rule argparse cannot state: ``--title`` needs ``--text-file``."""

    def check(args: argparse.Namespace) -> None:
        if args.title is not None and args.text_file is None:
            fetch.error("argument --title: only allowed with --text-file")

    return check


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code."""

    _use_utf8(sys.stdout, "strict")
    _use_utf8(sys.stderr, "backslashreplace")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        usage_check: UsageCheck | None = getattr(args, "usage_check", None)
        if usage_check is not None:
            usage_check(args)
    except SystemExit as exit_request:  # --help, --version and usage errors
        code = exit_request.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else 2)
    debug = bool(getattr(args, "debug", False))
    try:
        data_dir = _data_dir(args)
        handler: Handler = args.handler
        return handler(args, data_dir)
    except CheckError as error:
        line = error.line()
        # An eval abort line can carry untrusted text (a labels key, a path), so it
        # prints as inert text, as every report line does.
        _err(score.printable(line) if _is_eval(args) else line)
        return error.exit_code
    except Exception as error:
        if debug:
            traceback.print_exc(file=sys.stderr)
        crash = f"{CODE_INTERNAL}: unexpected {type(error).__name__}: {one_line(str(error))}"
        if _is_eval(args):
            # An eval that crashed has no verdict: exit 6, never a FAIL (plan Appendix C).
            _err(
                score.printable(
                    f"{CODE_EVAL_ABORT}: eval score crashed, so there is no verdict; {crash}"
                )
            )
            return EXIT_EVAL_ABORT
        _err(crash)
        return EXIT_INTERNAL


def _is_eval(args: argparse.Namespace) -> bool:
    """True inside ``eval score``, where every failure is an abort with no verdict (exit 6)."""

    return bool(getattr(args, "eval_abort", False))


def _data_dir(args: argparse.Namespace) -> paths.DataDir:
    """The command's data directory, printed as the first stderr line and refused
    (exit 2) inside a git checkout. Inside ``eval score`` a data dir that cannot be used
    (an empty value, a git checkout) is an abort instead (exit 6, plan Appendix C)."""

    try:
        data_dir = paths.resolve_data_dir(getattr(args, "data_dir", None))
        _err(f"data_dir: {data_dir.path}")
        paths.reject_git_checkout(data_dir.path)
    except ConfigError as error:
        if _is_eval(args):
            raise score.data_dir_abort(error) from error
        raise
    return data_dir


def _require_database(data_dir: paths.DataDir) -> None:
    if not data_dir.has_database():
        raise ConfigError(f"no database at {data_dir.db_path}; run `checklist-care init` first")


def _cmd_init(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    paths.ensure_layout(data_dir.path)
    existed = data_dir.has_database()
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


FetchedSource = tuple[packet.Packet, str | None]
"""What one fetch gives ``_fetch_source``: the packet and its ``source.revision``."""


def _cmd_fetch(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    if args.text_file is not None:
        # The file is read and checked first: its ref is the hash of its text.
        text = text_source.read_text_file(Path(args.text_file))
        title: str | None = args.title

        def make_text_source(fetched_at: str) -> FetchedSource:
            # No revision: a changed text is a new source (``text_source.revision``).
            return text_source.build_packet(text, title=title, fetched_at=fetched_at), None

        return _fetch_source(data_dir, make_text_source, ref=text.ref)

    if wiki.is_wikipedia_url(args.url):
        article = wiki.parse_wiki_url(args.url)

        def make_wiki_source(fetched_at: str) -> FetchedSource:
            fetched = wiki.fetch_article(article, fetched_at=fetched_at)
            return fetched.packet, fetched.revision

        # The ref holds the canonical title after redirects, which only the answer
        # gives, so a reviewed article is refused after the request, before writing.
        return _fetch_source(data_dir, make_wiki_source, ref=None)

    video_id = youtube.parse_video_id(args.url)

    def make_video_source(fetched_at: str) -> FetchedSource:
        fetched = youtube.fetch_packet(video_id, fetched_at=fetched_at)
        return fetched, youtube.revision(fetched)

    return _fetch_source(data_dir, make_video_source, ref=youtube.source_ref(video_id))


def _fetch_source(
    data_dir: paths.DataDir,
    make_source: Callable[[str], FetchedSource],
    *,
    ref: str | None,
) -> int:
    """Fetch a source with ``make_source(fetched_at)``, then write its packet and
    upsert its row together, and print the four ``fetch`` lines (plan §4.4).

    ``ref`` is the source's ref when the input alone gives it (YouTube, text); a
    reviewed source is then refused before any network call. A Wikipedia ref is known
    only from the answer (``ref=None``). Either way the packet's ``source_ref`` is
    checked again under the write lock, so a reviewed source is never written.
    """

    _require_database(data_dir)
    paths.ensure_layout(data_dir.path)
    with closing(connection.connect(data_dir.db_path)) as conn:
        # Refuse a reviewed source before any network call, and again under the
        # write lock in case `propose` reviewed it while the fetch ran.
        if ref is not None:
            source_store.refuse_reviewed(conn, ref)
        fetched_at = connection.format_timestamp(connection.utc_now())
        fetched, revision = make_source(fetched_at)
        if ref is not None and fetched.source_ref != ref:
            raise RuntimeError(f"fetched {fetched.source_ref}, expected {ref}")
        ref = fetched.source_ref
        target = packet.packet_path(data_dir.path, ref)
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
                    revision=revision,
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


def _cmd_context(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    _require_database(data_dir)
    with closing(connection.connect(data_dir.db_path)) as conn:
        payload = context_module.build_context(conn, data_dir.path, args.source_ref)
    _out(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_propose(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    _require_database(data_dir)
    with closing(connection.connect(data_dir.db_path)) as conn:
        try:
            result = proposal.submit(conn, data_dir.path, Path(args.file))
        except ProposalInvalidError as rejected:
            _out(rejected.problems_json())
            raise
    for line in result.summary_lines():
        _out(line)
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    # Imported here so the other commands do not load FastAPI and uvicorn.
    from checklist_care.server import run

    if not args.demo:
        _require_database(data_dir)
    return run.serve(data_dir.db_path, port=args.port, build=args.build, demo_mode=args.demo)


def _cmd_eval_score(args: argparse.Namespace, data_dir: paths.DataDir) -> int:
    try:
        report = score.run(data_dir, Path(args.labels))
    except EvalAbortError:
        raise
    except CheckError as error:
        # Inside an eval every expected failure is an abort: no verdict (exit 6).
        raise EvalAbortError(error.line()) from error
    if args.json:
        # ASCII-only on stdout: every non-ASCII character, control codes included, is a
        # JSON escape, so the parsed value is exact and nothing reaches the terminal raw.
        payload = {**report.to_json(), "score_file": str(report.path)}
        _out(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        for line in report.lines():
            _out(line)
    if report.verdict == score.ABORT:
        raise EvalAbortError(report.abort_message())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
