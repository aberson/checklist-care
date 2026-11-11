"""Smoke gate (plan Step 8): one real first-delivery cycle, end to end, in about 90 s.

    uv run python scripts/smoke.py --offline tests/fixtures/packet_youtube_synthetic.json
    uv run python scripts/smoke.py --live

Every component is the real one: each command runs as its own ``checklist-care``
process (``python -m checklist_care.cli``), the server is a real uvicorn process, and
the checks go over HTTP. That catches producer/consumer drift across fetch -> packet ->
proposal -> DB -> API -> print. The seven phases:

1. ``init`` a fresh data directory inside a new temp directory.
2. Obtain the packet. ``--live`` runs the real ``fetch`` of the in-sample video (it
   needs the network, so it runs only in manual step M1). ``--offline <packet>`` copies
   the packet file into ``packets/`` and upserts its source in-process through
   ``store/sources.py``, as ``fetch`` does.
3. Run ``context``, write a proposal built from the packet's chapters (each chapter
   title as a name, one fixed action, bucket 1, list 7) carrying the ``skill_sha256``
   and ``packet_sha256`` that ``context`` printed, and run ``propose``.
4. Start ``checklist-care serve --port <free port>`` with ``CREATE_NEW_PROCESS_GROUP |
   CREATE_NO_WINDOW`` and wait until ``/api/health`` names this data directory.
5. ``GET /api/tips?status=pending`` must hold the proposed tips, in order, with the
   proposed value of every proposal tip field (``proposal.TIP_FIELDS``) and the
   proposal's ``source_ref``. Nothing may print yet. Then ``POST /api/review/approve``.
6. ``GET /api/print/lists/7`` prints exactly the approved tips, in order, each row's
   fields (``printing.PrintItem``: name, action) equal to the approved tip's.
7. Stop the server (``taskkill /PID <pid> /T /F``, then ``wait(10)``), check that its
   port refuses connections, and remove the temp directory, retrying briefly.

A check that fails names where the two sides first differ: the row count, or the row
number and the drifted field with both values.

A failure in any phase still stops the server and removes the temp directory; the
server log's last lines are shown first.

Output: ``work_dir: <path>``, ``port: <n>``, one ``phase N/7 <name>: ok`` line per phase
and ``PASS in <s> s`` on stdout; ``FAIL: ...`` lines on stderr. Exit 0 is PASS, 1 FAIL
and 2 a usage error. With ``--live``, a ``fetch`` that exits 4 (``CC_E_FETCH``) exits 4:
M1 records a network or block error as ``LIVE: BLOCKED-EXTERNAL (<class>)``, not as a
code FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Final

from checklist_care import limits, paths, printing, processes, proposal
from checklist_care.db import connection
from checklist_care.errors import CODE_FETCH, EXIT_FETCH, EXIT_OK
from checklist_care.server import API_PREFIX, HOST, base_url
from checklist_care.sources import packet as packet_module
from checklist_care.sources import youtube
from checklist_care.store import sources as source_store

PHASES: Final = 7

IN_SAMPLE_VIDEO: Final = "LpeDuC2sQ-w"
"""The in-sample video that ``--live`` fetches (plan §4.8, Appendix F)."""

LIST_ID: Final = 7
"""The seeded Coffee Machine list (adults), where every smoke tip is placed."""

ACTION: Final = "Do this once before your first coffee"
"""The one action of every smoke tip. With the longest of the 40 test-video chapter
titles (36 characters) the printed line stays inside ``LINE_MAX``."""

REASON: Final = "A smoke-test tip named after one chapter of the source video."
NOTE: Final = "written by scripts/smoke.py"
MODEL: Final = "smoke-test"

EXIT_FAIL: Final = 1

WORK_DIR_PREFIX: Final = "checklist-care-smoke-"
DATA_DIR_NAME: Final = "data"
PROPOSAL_NAME: Final = "proposal.json"
SERVER_LOG_NAME: Final = "server.log"

FETCH_KEYS: Final = ("packet", "source_ref", "packet_sha256")
"""The ``key: value`` lines of ``fetch`` that ``--live`` checks against the packet file."""

REVIEWED_FIELDS: Final = proposal.TIP_FIELDS
"""Every field of a proposal tip, as ``proposal.py`` defines them. Each pending tip must
carry the proposed value of each one."""

SOURCE_REF_FIELD: Final = "source_ref"
"""The proposal envelope field that each pending tip must carry as well."""

PENDING_FIELDS: Final = (*REVIEWED_FIELDS, SOURCE_REF_FIELD)
"""The fields of ``GET /api/tips`` that phase 5 compares with the proposal."""

PRINTED_FIELDS: Final = tuple(field.name for field in fields(printing.PrintItem))
"""The fields of one printed row (``printing.PrintItem``), which phase 6 compares."""

CLI_TIMEOUT_S: Final = 60.0
"""Limit for ``init``, ``context`` and ``propose``; the tree is killed after it."""

FETCH_TIMEOUT_S: Final = 180.0
"""Limit for the live ``fetch``: the transcript request plus yt-dlp's own 60 s."""

READY_TIMEOUT_S: Final = 60.0
"""How long the server may take to answer ``/api/health``."""

HTTP_TIMEOUT_S: Final = 10.0
POLL_S: Final = 0.2

CONNECT_TIMEOUT_S: Final = 5.0
"""One probe of the stopped server's port. Windows takes about a second to report a
refused loopback connection, so the probe waits longer than that."""

REFUSE_WAIT_S: Final = 10.0
"""How long the stopped server's port may keep accepting before the smoke fails."""

REMOVE_ATTEMPTS: Final = 10
REMOVE_DELAY_S: Final = 0.5
"""The temp directory is removed with retries: a killed process releases its file
handles (the database, its WAL, the server log) shortly after it exits."""

LOG_TAIL_LINES: Final = 30
OUTPUT_TAIL_LINES: Final = 20

SERVER_CREATIONFLAGS: Final = processes.NO_WINDOW | int(
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)
"""``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` on Windows, 0 elsewhere."""

_NO_PROXY: Final = urllib.request.build_opener(urllib.request.ProxyHandler({}))
"""An opener that ignores ``HTTP_PROXY``: the server is on the loopback address."""

_ABSENT: Final = object()
"""The value of a field a row does not carry."""


class SmokeFailure(Exception):
    """A phase failed; ``exit_code`` is the smoke's exit code (1 unless stated)."""

    def __init__(self, message: str, *, exit_code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class CliResult:
    """One finished ``checklist-care`` command."""

    command: str
    code: int
    out: str
    err: str

    def tail(self) -> str:
        lines = [*self.out.splitlines(), *self.err.splitlines()]
        return "\n".join(f"  | {line}" for line in lines[-OUTPUT_TAIL_LINES:])


@dataclass
class Run:
    """The state that cleanup needs, whichever phase failed."""

    work_dir: Path
    phase: int = 0
    phase_name: str = "setup"
    server: subprocess.Popen[bytes] | None = None
    server_stopped: bool = False
    removed: bool = False

    @property
    def data_dir(self) -> Path:
        return self.work_dir / DATA_DIR_NAME

    @property
    def log_path(self) -> Path:
        return self.work_dir / SERVER_LOG_NAME

    def begin(self, phase: int, name: str) -> None:
        self.phase = phase
        self.phase_name = name

    def done(self, detail: str) -> None:
        say(f"phase {self.phase}/{PHASES} {self.phase_name}: ok ({detail})")


def say(line: str) -> None:
    print(line, file=sys.stdout, flush=True)


def complain(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def shown(value: object) -> str:
    return "missing" if value is _ABSENT else repr(value)


def difference(
    rows: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]], *, noun: str
) -> str | None:
    """None when ``rows`` equals ``expected``. Otherwise where they first differ: the
    number of rows, or the first drifted field of the first drifted row, by name.

    Every field of either side counts, so a row that loses a field or gains one differs
    as much as a row whose value changed.
    """

    if len(rows) != len(expected):
        return f"{len(rows)} {noun}s, expected {len(expected)}"
    for number, (row, wanted) in enumerate(zip(rows, expected, strict=True), start=1):
        for field in (*wanted, *(key for key in row if key not in wanted)):
            have, want = row.get(field, _ABSENT), wanted.get(field, _ABSENT)
            if have != want:
                return (
                    f"{noun} {number}/{len(rows)} {field} is {shown(have)}, expected {shown(want)}"
                )
    return None


# --- child processes --------------------------------------------------------------------


def cli_command(data_dir: Path, *args: str) -> list[str]:
    """``python -m checklist_care.cli --data-dir <data_dir> <args>``."""

    return [sys.executable, "-m", "checklist_care.cli", "--data-dir", str(data_dir), *args]


def child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def run_cli(data_dir: Path, *args: str, timeout: float | None = None) -> CliResult:
    """Run one ``checklist-care`` command without a console window, for up to
    ``timeout`` seconds (``CLI_TIMEOUT_S`` when None).

    A timeout or an interrupt kills the whole process tree: the venv ``python.exe``
    is a launcher whose child is the real interpreter.
    """

    if timeout is None:
        timeout = CLI_TIMEOUT_S
    command = cli_command(data_dir, *args)
    shown = f"checklist-care {args[0]}"
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env(),
            creationflags=processes.NO_WINDOW,
            start_new_session=processes.NEW_SESSION,
        )
    except OSError as error:
        raise SmokeFailure(f"cannot start `{shown}`: {error}") from error
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        processes.kill_tree(process)
        raise SmokeFailure(
            f"`{shown}` gave no result within {timeout:g} s; its process tree was killed"
        ) from None
    except BaseException:
        processes.kill_tree(process)
        raise
    return CliResult(
        command=shown,
        code=process.returncode,
        out=out.decode("utf-8", errors="replace"),
        err=err.decode("utf-8", errors="replace"),
    )


def expect_ok(result: CliResult) -> CliResult:
    if result.code != EXIT_OK:
        raise SmokeFailure(f"`{result.command}` exited {result.code}:\n{result.tail()}")
    return result


# --- phases 1-3: data dir, packet, proposal -----------------------------------------------


def init_data_dir(data_dir: Path) -> None:
    expect_ok(run_cli(data_dir, "init"))
    if not (data_dir / paths.DB_FILENAME).is_file():
        raise SmokeFailure(f"`checklist-care init` exited 0 but wrote no {paths.DB_FILENAME}")


def read_source_packet(path: Path) -> packet_module.Packet:
    try:
        return packet_module.read_packet(path)
    except (OSError, packet_module.PacketFormatError) as error:
        raise SmokeFailure(f"cannot read the packet {path}: {error}") from error


def offline_packet(data_dir: Path, source: Path) -> Path:
    """``--offline``: copy ``source`` into ``packets/`` byte for byte and upsert its
    source row in-process, as ``fetch`` does. Returns the packet's path."""

    loaded = read_source_packet(source)
    target = packet_module.packet_path(data_dir, loaded.source_ref)
    try:
        shutil.copyfile(source, target)
    except OSError as error:
        raise SmokeFailure(f"cannot copy the packet to {target}: {error}") from error
    with closing(connection.connect(data_dir / paths.DB_FILENAME)) as conn:
        with connection.transaction(conn):
            source_store.upsert_fetched(
                conn,
                kind=loaded.kind,
                ref=loaded.source_ref,
                title=loaded.title,
                url=loaded.url,
                revision=youtube.revision(loaded),
                fetched_at=loaded.fetched_at,
            )
    return target


def fetch_fields(out: str) -> dict[str, str]:
    """The ``key: value`` lines that ``fetch`` prints on stdout."""

    found: dict[str, str] = {}
    for line in out.splitlines():
        key, separator, value = line.partition(": ")
        if separator:
            found[key] = value
    return found


def live_packet(data_dir: Path) -> Path:
    """``--live``: the real ``fetch`` of the in-sample video. Returns the packet's path."""

    url = youtube.watch_url(IN_SAMPLE_VIDEO)
    result = run_cli(data_dir, "fetch", url, timeout=FETCH_TIMEOUT_S)
    if result.code == EXIT_FETCH:
        raise SmokeFailure(
            f"`checklist-care fetch {url}` exited {EXIT_FETCH} ({CODE_FETCH}). A network or "
            "block error (IpBlocked, RequestBlocked, HTTP 429/503, DNS) is recorded as "
            f"LIVE: BLOCKED-EXTERNAL (<class>) and retried later:\n{result.tail()}",
            exit_code=EXIT_FETCH,
        )
    expect_ok(result)
    printed = fetch_fields(result.out)
    missing = [key for key in FETCH_KEYS if key not in printed]
    if missing:
        raise SmokeFailure(f"`fetch` printed no {', '.join(missing)} line:\n{result.tail()}")
    ref = youtube.source_ref(IN_SAMPLE_VIDEO)
    if printed["source_ref"] != ref:
        raise SmokeFailure(f"`fetch` printed source_ref {printed['source_ref']!r}, not {ref!r}")
    target = packet_module.packet_path(data_dir, ref)
    if Path(printed["packet"]) != target:
        raise SmokeFailure(f"`fetch` printed packet {printed['packet']}, not {target}")
    actual = file_sha256(target)
    if printed["packet_sha256"] != actual:
        raise SmokeFailure(
            f"`fetch` printed packet_sha256 {printed['packet_sha256']}, but the file hashes "
            f"to {actual}"
        )
    return target


def file_sha256(path: Path) -> str:
    try:
        return packet_module.file_sha256(path)
    except OSError as error:
        raise SmokeFailure(f"cannot read {path}: {error}") from error


def read_context(data_dir: Path, source: packet_module.Packet, packet_file: Path) -> dict[str, Any]:
    """``context --source-ref``, checked against the packet file it describes."""

    result = expect_ok(run_cli(data_dir, "context", "--source-ref", source.source_ref))
    try:
        payload = json.loads(result.out)
    except json.JSONDecodeError as error:
        raise SmokeFailure(
            f"`context` printed no JSON object ({error}):\n{result.tail()}"
        ) from None
    if not isinstance(payload, dict):
        raise SmokeFailure(f"`context` printed {type(payload).__name__}, not a JSON object")
    context: dict[str, Any] = payload
    block = context.get("source")
    if not isinstance(block, dict) or not isinstance(context.get("skill_sha256"), str):
        raise SmokeFailure("`context` printed no skill_sha256 or no source block")
    actual = file_sha256(packet_file)
    if block.get("packet_sha256") != actual:
        raise SmokeFailure(
            f"`context` gave packet_sha256 {block.get('packet_sha256')}, but the packet file "
            f"{packet_file} hashes to {actual}"
        )
    served = block.get("chapters")
    if not isinstance(served, list) or not all(isinstance(item, dict) for item in served):
        raise SmokeFailure(f"`context` gave chapters {served!r}, not a list of objects")
    # Every field of packet.Chapter, so a field the packet gains is compared too.
    drift = difference(served, [asdict(chapter) for chapter in source.chapters], noun="chapter")
    if drift is not None:
        raise SmokeFailure(f"`context` gave other chapters than the packet file holds: {drift}")
    return context


def proposal_tips(source: packet_module.Packet) -> list[dict[str, Any]]:
    """One bucket-1 tip on list 7 per chapter, named by the chapter title verbatim."""

    if not source.chapters:
        raise SmokeFailure(
            f"the packet of {source.source_ref} has no chapters; the smoke builds its "
            "proposal from chapters"
        )
    return [
        {
            "name": chapter.title,
            "action": ACTION,
            "reason": REASON,
            "bucket": limits.BUCKET_DAILY,
            "audience": limits.AUDIENCE_ADULTS,
            "list_ids": [LIST_ID],
            "start_s": chapter.start_s,
            "locator": None,
            "evidence_url": None,
            "duplicate_of": None,
            "possible_duplicate_of": None,
            "note": NOTE,
        }
        for chapter in source.chapters
    ]


def build_proposal(
    source: packet_module.Packet, context: Mapping[str, Any], tips: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The proposal, with both fingerprints copied from ``context`` as the skill does."""

    return {
        "schema": proposal.PROPOSAL_SCHEMA,
        "source_ref": source.source_ref,
        "packet_sha256": context["source"]["packet_sha256"],
        "skill_sha256": context["skill_sha256"],
        "host": limits.HOST_CLAUDE_CODE,
        "model": MODEL,
        "mode": limits.SOURCE_MODE_EXTRACT,
        "degraded": False,
        "tips": list(tips),
    }


def submit_proposal(data_dir: Path, path: Path, document: Mapping[str, Any]) -> None:
    """Write ``document`` (UTF-8, no BOM, LF) and run ``propose`` on it."""

    path.write_bytes((json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    expect_ok(run_cli(data_dir, "propose", str(path)))


# --- phase 4: the server ------------------------------------------------------------------


def free_port() -> int:
    """A loopback port nothing listens on right now."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        port: int = probe.getsockname()[1]
    return port


def start_server(data_dir: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    """``python -m checklist_care.cli serve --port <port>``, output to ``log_path``."""

    command = cli_command(data_dir, "serve", "--port", str(port))
    with log_path.open("wb") as log:  # the child keeps its own handle
        try:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=child_env(PYTHONUNBUFFERED="1"),
                creationflags=SERVER_CREATIONFLAGS,
                start_new_session=processes.NEW_SESSION,
            )
        except OSError as error:
            raise SmokeFailure(f"cannot start `checklist-care serve`: {error}") from error


def api_url(port: int, path: str) -> str:
    return f"{base_url(port)}{API_PREFIX}{path}"


def http_json(port: int, method: str, path: str, body: Any = None) -> Any:
    """One request to the server; the decoded JSON answer. Raises what urllib raises."""

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(api_url(port, path), data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with _NO_PROXY.open(request, timeout=HTTP_TIMEOUT_S) as response:
        raw: bytes = response.read()
    return json.loads(raw.decode("utf-8"))


def api(port: int, method: str, path: str, body: Any = None) -> Any:
    """:func:`http_json`, with every failure as a :class:`SmokeFailure`."""

    shown = f"{method} {API_PREFIX}{path}"
    try:
        return http_json(port, method, path, body)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise SmokeFailure(f"{shown} answered HTTP {error.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as error:
        raise SmokeFailure(f"{shown} failed: {error}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SmokeFailure(f"{shown} answered no JSON: {error}") from None


def wait_ready(process: subprocess.Popen[bytes], port: int, data_dir: Path) -> None:
    """Poll ``/api/health`` until this server answers for ``data_dir``."""

    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        code = process.poll()
        if code is not None:
            raise SmokeFailure(f"the server exited {code} before it answered /api/health")
        try:
            health = http_json(port, "GET", "/health")
        except urllib.error.HTTPError as error:
            raise SmokeFailure(f"GET {API_PREFIX}/health answered HTTP {error.code}") from None
        except (urllib.error.URLError, OSError):
            if time.monotonic() >= deadline:
                raise SmokeFailure(
                    f"the server did not answer {api_url(port, '/health')} within "
                    f"{READY_TIMEOUT_S:g} s"
                ) from None
            time.sleep(POLL_S)
            continue
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SmokeFailure(f"GET {API_PREFIX}/health answered no JSON: {error}") from None
        break
    served = health.get("data_dir") if isinstance(health, dict) else None
    if not isinstance(health, dict) or health.get("ok") is not True or not isinstance(served, str):
        raise SmokeFailure(f"GET {API_PREFIX}/health answered {health!r}")
    if Path(served) != Path(os.path.abspath(data_dir)):
        raise SmokeFailure(
            f"the server on port {port} serves {served}, not this smoke's {data_dir}"
        )


# --- phases 5-6: review and print -----------------------------------------------------------


def review(port: int, source_ref: str, tips: Sequence[Mapping[str, Any]]) -> list[int]:
    """Check the pending tips against the proposal, field by field (``PENDING_FIELDS``)
    and in proposal order (by id), check that none prints yet, then approve them all.
    Returns their ids."""

    pending = api(port, "GET", "/tips?status=pending")
    if not isinstance(pending, list) or not all(
        isinstance(tip, dict) and type(tip.get("id")) is int for tip in pending
    ):
        raise SmokeFailure(f"GET {API_PREFIX}/tips answered {pending!r}")
    pending = sorted(pending, key=lambda tip: int(tip["id"]))
    expected = [
        {**{field: tip[field] for field in REVIEWED_FIELDS}, SOURCE_REF_FIELD: source_ref}
        for tip in tips
    ]
    # A field the server does not send stays missing, so it cannot pass as null.
    served = [{field: tip[field] for field in PENDING_FIELDS if field in tip} for tip in pending]
    drift = difference(served, expected, noun="tip")
    if drift is not None:
        raise SmokeFailure(f"the pending tips differ from the proposal: {drift}")
    tip_ids = [int(tip["id"]) for tip in pending]

    before = print_items(port)
    if before:
        raise SmokeFailure(
            f"list {LIST_ID} already prints {len(before)} row(s) before any approval: "
            f"{before[:3]!r}"
        )

    answer = api(port, "POST", "/review/approve", {"tip_ids": tip_ids})
    if answer != {"approved": len(tip_ids)}:
        raise SmokeFailure(f"POST {API_PREFIX}/review/approve answered {answer!r}")
    return tip_ids


def print_items(port: int) -> list[dict[str, Any]]:
    sheet = api(port, "GET", f"/print/lists/{LIST_ID}")
    items = sheet.get("items") if isinstance(sheet, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise SmokeFailure(f"GET {API_PREFIX}/print/lists/{LIST_ID} answered {sheet!r}")
    return items


def check_print(port: int, tips: Sequence[Mapping[str, Any]]) -> None:
    """The list's print sheet holds exactly the approved tips, field by field
    (``PRINTED_FIELDS``, and no other) and in proposal order."""

    expected = [{field: tip[field] for field in PRINTED_FIELDS} for tip in tips]
    drift = difference(print_items(port), expected, noun="row")
    if drift is not None:
        raise SmokeFailure(f"list {LIST_ID} prints other rows than the approved tips: {drift}")


# --- phase 7: stop and clean up -------------------------------------------------------------


def stop_server(process: subprocess.Popen[bytes]) -> None:
    """``taskkill /PID <pid> /T /F`` (the whole tree; a process group kill elsewhere),
    then ``wait(10)``."""

    processes.kill_tree(process)
    try:
        process.wait(timeout=processes.KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        raise SmokeFailure(
            f"the server (pid {process.pid}) still runs {processes.KILL_WAIT_S:g} s after "
            "its process tree was killed"
        ) from None


def port_state(port: int) -> str:
    """``refused``, or what else a connection to ``port`` did."""

    try:
        with socket.create_connection((HOST, port), timeout=CONNECT_TIMEOUT_S):
            return "accepts connections"
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return f"neither accepted nor refused a connection within {CONNECT_TIMEOUT_S:g} s"
    except OSError as error:
        return f"gave {type(error).__name__}: {error}"


def check_port_refuses(port: int) -> None:
    deadline = time.monotonic() + REFUSE_WAIT_S
    while True:
        state = port_state(port)
        if state == "refused":
            return
        if time.monotonic() >= deadline:
            raise SmokeFailure(f"port {port} {state} after the server was stopped")
        time.sleep(POLL_S)


def remove_tree(path: Path) -> None:
    """Remove ``path``, retrying while a killed process still holds a file in it."""

    error: OSError | None = None
    for attempt in range(REMOVE_ATTEMPTS):
        if attempt:
            time.sleep(REMOVE_DELAY_S)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as caught:
            error = caught
            continue
        if not path.exists():
            return
    raise SmokeFailure(
        f"cannot remove the temp directory {path} after {REMOVE_ATTEMPTS} attempts: {error}"
    )


def log_tail(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-LOG_TAIL_LINES:]


# --- the run ------------------------------------------------------------------------------


def run_phases(run: Run, args: argparse.Namespace) -> None:
    data_dir = run.data_dir

    run.begin(1, "init")
    init_data_dir(data_dir)
    run.done(f"data_dir {data_dir}")

    run.begin(2, "packet")
    packet_file = live_packet(data_dir) if args.live else offline_packet(data_dir, args.offline)
    source = read_source_packet(packet_file)
    run.done(f"{source.source_ref}, {len(source.chapters)} chapters, {packet_file.name}")

    run.begin(3, "propose")
    context = read_context(data_dir, source, packet_file)
    tips = proposal_tips(source)
    submit_proposal(data_dir, run.work_dir / PROPOSAL_NAME, build_proposal(source, context, tips))
    run.done(f"{len(tips)} tips pending")

    run.begin(4, "serve")
    port = free_port()
    say(f"port: {port}")
    server = start_server(data_dir, port, run.log_path)
    run.server = server
    wait_ready(server, port, data_dir)
    run.done(f"{base_url(port)}, pid {server.pid}")

    run.begin(5, "review")
    tip_ids = review(port, source.source_ref, tips)
    run.done(f"approved {len(tip_ids)} tips")

    run.begin(6, "print")
    check_print(port, tips)
    run.done(f"list {LIST_ID} prints the {len(tips)} approved tips")

    run.begin(7, "stop")
    stop_server(server)
    run.server_stopped = True
    check_port_refuses(port)
    remove_tree(run.work_dir)
    run.removed = True
    run.done(f"server stopped, port {port} refuses, temp dir removed")


def report(run: Run, message: str) -> None:
    complain(f"FAIL: phase {run.phase}/{PHASES} {run.phase_name}: {message}")
    if run.server is not None and not run.removed:
        tail = log_tail(run.log_path)
        if tail:
            complain(f"server log ({run.log_path.name}, last {len(tail)} lines):")
            for line in tail:
                complain(f"  | {line}")


def cleanup(run: Run) -> list[str]:
    """Stop the server and remove the temp directory, whatever already failed.
    Returns the problems."""

    problems: list[str] = []
    if run.server is not None and not run.server_stopped:
        try:
            stop_server(run.server)
            run.server_stopped = True
        except SmokeFailure as failure:
            problems.append(str(failure))
    if not run.removed:
        try:
            remove_tree(run.work_dir)
            run.removed = True
        except SmokeFailure as failure:
            problems.append(str(failure))
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke.py",
        description=(
            "Run one real first-delivery cycle (init, packet, propose, serve, review, "
            "print, stop) in a temp data directory."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--live",
        action="store_true",
        help=(
            f"fetch the in-sample video {IN_SAMPLE_VIDEO} over the network "
            "(manual step M1; never in the default gate)"
        ),
    )
    source.add_argument(
        "--offline",
        type=Path,
        metavar="PACKET",
        help="use this packet file instead of fetching, such as "
        "tests/fixtures/packet_youtube_synthetic.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_request:  # --help and usage errors
        code = exit_request.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else 2)
    started = time.monotonic()
    run = Run(Path(tempfile.mkdtemp(prefix=WORK_DIR_PREFIX)))
    say(f"work_dir: {run.work_dir}")
    code = EXIT_OK
    try:
        run_phases(run, args)
    except SmokeFailure as failure:
        code = failure.exit_code
        report(run, str(failure))
    except Exception as error:
        code = EXIT_FAIL
        traceback.print_exc(file=sys.stderr)
        report(run, f"unexpected {type(error).__name__}: {error}")
    finally:  # also on Ctrl+C
        problems = cleanup(run)
    # A run that got through phase 7 stopped its server and removed its temp directory
    # there, so cleanup only finds problems after a phase failed and set the exit code.
    for problem in problems:
        complain(f"FAIL: cleanup: {problem}")
    if code == EXIT_OK:
        say(f"PASS in {time.monotonic() - started:.1f} s")
    return code


if __name__ == "__main__":
    sys.exit(main())
