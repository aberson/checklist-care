"""``scripts/smoke.py``, the Step 8 smoke gate (plan Step 8, §10, §11).

The main test runs ``smoke.py --offline tests/fixtures/packet_youtube_synthetic.json``
as a subprocess without a console window, exactly as the operator would, and checks
that it passes in under 120 s, that nothing listens on the port it used afterwards and
that its temp directory is gone. The smoke's temp directory is pointed into this test's
own folder (``TMP``/``TEMP``/``TMPDIR``), so "nothing left behind" is checked there.

Almost every other test is a red anchor for one smoke check: it feeds that check bad
data and expects exit 1 with the check's own failure line, and the cleanup (the real
server stopped, the temp directory gone). Most run ``main()`` in-process against the real
commands and the real server, with one answer rewritten on its way into the smoke
(:class:`Tamper`, :func:`tamper_cli`).

The drift tables keep the row count and change one field of one row, so a check that
compared only counts would pass them. Each table is derived from the module that
produces the data (``proposal.TIP_FIELDS``, ``printing.PrintItem``, ``packet.Chapter``),
so a field added there becomes a new row here.

``--live`` is exercised offline: ``fetch`` runs in-process through ``cli.main`` with the
two network seams faked, so the smoke reads the real ``fetch`` output format. The live
run itself (``smoke.py --live``) runs only in manual step M1.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from collections.abc import Callable, Iterable, Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from youtube_transcript_api import FetchedTranscript, FetchedTranscriptSnippet

from checklist_care import cli, limits, paths, printing, processes, proposal
from checklist_care.server import HOST
from checklist_care.sources import packet as packet_module
from checklist_care.sources import youtube

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "smoke.py"
FIXTURE_ARG = "tests/fixtures/packet_youtube_synthetic.json"
"""The fixture as the plan names it, relative to the checkout root (the smoke's cwd)."""
FIXTURE = ROOT / FIXTURE_ARG
FIXTURE_TIPS = 10
"""The fixture's chapters, so the smoke's tips and printed rows."""
SMOKE_LIMIT_S = 120.0
"""The offline smoke must pass inside the full gate in under this many seconds."""
TEMP_VARIABLES = ("TMP", "TEMP", "TMPDIR")
REFUSE_PROBE_S = 5.0
"""One connect probe; Windows takes about a second to report a refused loopback port."""
REFUSE_DEADLINE_S = 20.0
REFUSE_RETRY_S = 0.2

# The drift tables. Each comes from the producer of the data, never from the smoke, so
# a check that skipped a field would fail that field's row.
PENDING_TABLE = (*proposal.TIP_FIELDS, "source_ref")
"""What a pending tip must carry: every proposal tip field and the envelope's source_ref."""
PRINT_TABLE = tuple(item_field.name for item_field in dataclasses.fields(printing.PrintItem))
CHAPTER_TABLE = tuple(
    chapter_field.name for chapter_field in dataclasses.fields(packet_module.Chapter)
)
FETCH_FAILURES = {
    "packet": "`fetch` printed packet {now}, not {was}",
    "source_ref": "`fetch` printed source_ref {now!r}, not {was!r}",
    "packet_sha256": "`fetch` printed packet_sha256 {now}, but the file hashes to {was}",
}
"""The ``fetch`` lines (``cli._cmd_fetch``) that ``--live`` checks, and each one's failure."""
DRIFTED_ROW = 3
"""The row each drift table changes: the fourth, so neither the first nor the last."""


@dataclass(frozen=True)
class SmokeRun:
    code: int
    out: str
    err: str
    elapsed_s: float = 0.0

    def value(self, key: str) -> str:
        """The value of the smoke's ``<key>: <value>`` stdout line."""

        prefix = f"{key}: "
        found = [line[len(prefix) :] for line in self.out.splitlines() if line.startswith(prefix)]
        assert len(found) == 1, (key, self.out, self.err)
        return found[0]

    @property
    def failures(self) -> list[str]:
        return [line for line in self.err.splitlines() if line.startswith("FAIL: ")]

    @property
    def data_dir(self) -> str:
        return str(Path(self.value("work_dir")) / "data")


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real data directory."""

    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    """The temp directory the smoke creates its work directory in."""

    directory = tmp_path / "temp"
    directory.mkdir()
    return directory


def run_smoke_process(temp_root: Path, *args: str) -> SmokeRun:
    """``python scripts/smoke.py <args>`` from the checkout root, without a console
    window, with its temp directory under ``temp_root``. A run over the limit has its
    whole process tree killed."""

    env = dict(os.environ)
    for name in TEMP_VARIABLES:
        env[name] = str(temp_root)
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, str(SMOKE), *args],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=processes.NO_WINDOW,
        start_new_session=processes.NEW_SESSION,
    )
    try:
        out, err = process.communicate(timeout=SMOKE_LIMIT_S)
    except subprocess.TimeoutExpired:
        processes.kill_tree(process)
        pytest.fail(f"smoke.py {' '.join(args)} ran over {SMOKE_LIMIT_S:g} s")
    except BaseException:
        processes.kill_tree(process)
        raise
    return SmokeRun(
        code=process.returncode,
        out=out.decode("utf-8"),
        err=err.decode("utf-8"),
        elapsed_s=time.monotonic() - started,
    )


def refuses(port: int) -> bool:
    """True when a connection to ``127.0.0.1:<port>`` is refused: nothing listens.

    A port that accepts is False at once. A connect that is neither accepted nor
    refused (a ``TimeoutError`` or another socket error on a loaded machine) proves
    neither, so it is retried until ``REFUSE_DEADLINE_S``; then the answer is False.
    """

    deadline = time.monotonic() + REFUSE_DEADLINE_S
    while True:
        try:
            with socket.create_connection((HOST, port), timeout=REFUSE_PROBE_S):
                return False
        except ConnectionRefusedError:
            return True
        except OSError:  # TimeoutError included
            if time.monotonic() >= deadline:
                return False
            time.sleep(REFUSE_RETRY_S)


def assert_left_nothing(run: SmokeRun, temp_root: Path) -> None:
    work_dir = Path(run.value("work_dir"))
    # The work dir was really under temp_root, so an empty temp_root means it is gone.
    assert work_dir.parent == temp_root
    assert not work_dir.exists()
    assert sorted(temp_root.iterdir()) == []


def packet_variant(tmp_path: Path, name: str, edit: Callable[[dict[str, Any]], None]) -> Path:
    """A copy of the fixture packet with ``edit`` applied."""

    packet: dict[str, Any] = json.loads(FIXTURE.read_bytes().decode("utf-8"))
    edit(packet)
    path = tmp_path / name
    path.write_bytes((json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


# --- the gate: the offline smoke as a subprocess -------------------------------------------


def test_the_offline_smoke_passes_quickly_and_leaves_nothing_behind(temp_root: Path) -> None:
    run = run_smoke_process(temp_root, "--offline", FIXTURE_ARG)

    assert run.code == 0, run.out + run.err
    assert run.elapsed_s < SMOKE_LIMIT_S
    phases = [line.split(":")[0] for line in run.out.splitlines() if line.startswith("phase ")]
    assert phases == [
        "phase 1/7 init",
        "phase 2/7 packet",
        "phase 3/7 propose",
        "phase 4/7 serve",
        "phase 5/7 review",
        "phase 6/7 print",
        "phase 7/7 stop",
    ]
    assert "phase 5/7 review: ok (approved 10 tips)" in run.out.splitlines()
    assert run.out.splitlines()[-1].startswith("PASS in ")
    assert "FAIL" not in run.err
    assert refuses(int(run.value("port")))
    assert_left_nothing(run, temp_root)


def test_a_proposal_the_validator_rejects_fails_the_smoke_and_cleans_up(
    tmp_path: Path, temp_root: Path
) -> None:
    # Red anchor: a chapter title over NAME_MAX becomes a tip name `propose` rejects.
    def overlong_title(packet: dict[str, Any]) -> None:
        packet["chapters"][1]["title"] = "A" * (limits.NAME_MAX + 1)

    broken = packet_variant(tmp_path, "packet_overlong_title.json", overlong_title)

    run = run_smoke_process(temp_root, "--offline", str(broken))

    assert run.code == 1, run.out + run.err
    assert "FAIL: phase 3/7 propose: `checklist-care propose` exited 5" in run.err
    assert "CC_E_PROPOSAL" in run.err
    assert "NAME_MAX" in run.err
    assert "PASS" not in run.out
    assert "port: " not in run.out  # it failed before the server
    assert_left_nothing(run, temp_root)


def test_a_missing_packet_fails_the_smoke_and_cleans_up(tmp_path: Path, temp_root: Path) -> None:
    run = run_smoke_process(temp_root, "--offline", str(tmp_path / "no-such-packet.json"))

    assert run.code == 1, run.out + run.err
    assert "FAIL: phase 2/7 packet: cannot read the packet" in run.err
    assert_left_nothing(run, temp_root)


# --- in-process runs of main() -------------------------------------------------------------


def load_smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A fresh module of ``scripts/smoke.py``."""

    spec = importlib.util.spec_from_file_location("checklist_care_smoke_script", SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve the module through sys.modules while the class is created.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch, temp_root: Path) -> ModuleType:
    """``scripts/smoke.py`` loaded as a module, its temp directory under ``temp_root``."""

    module = load_smoke(monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return module


def run_main(smoke: ModuleType, capsys: pytest.CaptureFixture[str], *argv: str) -> SmokeRun:
    """``smoke.main(argv)`` in this process (``--offline`` on the fixture by default)."""

    code = smoke.main(list(argv) if argv else ["--offline", str(FIXTURE)])
    captured = capsys.readouterr()
    return SmokeRun(code=code, out=captured.out, err=captured.err)


@pytest.fixture
def started_servers(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[subprocess.Popen[bytes]]]:
    """Record every real server the smoke starts; kill any left over at teardown."""

    started: list[subprocess.Popen[bytes]] = []
    real_start = smoke.start_server

    def recording_start(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process: subprocess.Popen[bytes] = real_start(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(smoke, "start_server", recording_start)
    try:
        yield started
    finally:
        for process in started:
            processes.kill_tree(process)


def assert_failed_and_cleaned_up(
    run: SmokeRun,
    started: list[subprocess.Popen[bytes]],
    temp_root: Path,
    failure: str,
    *,
    server: bool = True,
    probe_port: bool = False,
) -> None:
    """Exit 1, and ``failure`` is the start of the one ``FAIL:`` line, so cleanup found no
    problem. With ``server``, the real server it started has exited and its log was shown
    before the temp directory went (``probe_port``: its port refuses too; Windows takes
    about 2 s to say so, so the drift tables leave it to the dedicated anchors). The work
    directory was under ``temp_root``, which is empty."""

    assert run.code == 1, run.out + run.err
    assert len(run.failures) == 1, run.err
    assert run.failures[0].startswith(failure), (run.failures[0], failure)
    assert "PASS" not in run.out
    if server:
        assert len(started) == 1
        assert started[0].poll() is not None  # the real server process is gone
        assert "server log (server.log" in run.err  # shown before the dir was removed
        if probe_port:
            assert refuses(int(run.value("port")))
    else:
        assert started == []
        assert "server log" not in run.err
    assert Path(run.value("work_dir")).parent == temp_root
    assert sorted(temp_root.iterdir()) == []


@dataclass
class Tamper:
    """Rewrites the server's answer to one request on its way into the smoke. The
    rewrite may raise instead, as a failed request would."""

    method: str
    path: str
    rewrite: Callable[[Any], Any]
    when: str = "always"
    """``always``, ``before-approval`` or ``after-approval`` (of ``POST /review/approve``)."""
    applied: int = 0


def tamper_with(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, tamper: Tamper) -> None:
    """Wrap the smoke's one HTTP helper so ``tamper`` rewrites its matching answers.
    Every request still goes to the real server."""

    assert tamper.when in ("always", "before-approval", "after-approval"), tamper.when
    real_http_json = smoke.http_json
    approved = False

    def http_json(port: int, method: str, path: str, body: Any = None) -> Any:
        nonlocal approved
        answer = real_http_json(port, method, path, body)
        wanted = {"always": True, "before-approval": not approved, "after-approval": approved}
        if (method, path) == (tamper.method, tamper.path) and wanted[tamper.when]:
            tamper.applied += 1
            answer = tamper.rewrite(answer)
        if (method, path) == ("POST", "/review/approve"):
            approved = True
        return answer

    monkeypatch.setattr(smoke, "http_json", http_json)


def test_a_failure_after_the_server_started_still_stops_it_and_removes_the_temp_dir(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken_print(port: int, tips: object) -> None:
        raise RuntimeError("injected print failure")

    monkeypatch.setattr(smoke, "check_print", broken_print)

    run = run_main(smoke, capsys)

    assert "phase 5/7 review: ok (approved 10 tips)" in run.out
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 6/7 print: unexpected RuntimeError: injected print failure",
        probe_port=True,
    )


def test_an_interrupt_still_stops_the_server_and_removes_the_temp_dir(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt(answer: Any) -> Any:
        raise KeyboardInterrupt

    tamper_with(smoke, monkeypatch, Tamper("GET", "/tips?status=pending", interrupt))

    with pytest.raises(KeyboardInterrupt):
        smoke.main(["--offline", str(FIXTURE)])
    run = SmokeRun(code=-1, out=capsys.readouterr().out, err="")

    assert "PASS" not in run.out
    assert len(started_servers) == 1
    assert started_servers[0].poll() is not None
    assert refuses(int(run.value("port")))
    assert Path(run.value("work_dir")).parent == temp_root
    assert sorted(temp_root.iterdir()) == []


# --- the drift tables: same row count, one field changed -----------------------------------


def drifted(value: Any) -> Any:
    """Another value of the same kind as ``value``."""

    if value is None:
        return "drifted"
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return f"{value} (drifted)"
    if isinstance(value, list):
        return [drifted(item) for item in value] or ["drifted"]
    raise TypeError(f"drifted() has no rule for {value!r}; add one for the new field")


@dataclass
class Drift:
    """Changes field ``name`` of one row and records both values."""

    name: str
    index: int = DRIFTED_ROW
    was: Any = None
    now: Any = None

    def apply(self, rows: list[dict[str, Any]]) -> None:
        assert len(rows) == FIXTURE_TIPS  # the count stays
        row = rows[self.index]
        self.was = row[self.name]
        self.now = drifted(self.was)
        row[self.name] = self.now

    def line(self, noun: str) -> str:
        """``difference()``'s words for this drift."""

        return (
            f"{noun} {self.index + 1}/{FIXTURE_TIPS} {self.name} is {self.now!r}, "
            f"expected {self.was!r}"
        )


def test_the_checked_fields_come_from_their_producers(smoke: ModuleType) -> None:
    # A3: the drift checks and the producers share one definition of the fields.
    assert smoke.REVIEWED_FIELDS is proposal.TIP_FIELDS
    assert smoke.SOURCE_REF_FIELD in proposal.ENVELOPE_FIELDS
    assert smoke.PENDING_FIELDS == PENDING_TABLE
    assert smoke.PRINTED_FIELDS == PRINT_TABLE
    assert tuple(printing.PrintItem(name="n", action="a").to_json()) == PRINT_TABLE
    assert tuple(FETCH_FAILURES) == smoke.FETCH_KEYS
    # The proposal the smoke writes carries every tip field, in the producer's order.
    tips = smoke.proposal_tips(packet_module.read_packet(FIXTURE))
    assert len(tips) == FIXTURE_TIPS
    assert all(tuple(tip) == proposal.TIP_FIELDS for tip in tips)


def test_the_checked_fields_follow_their_producers_when_these_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Derivation, not a copy: a smoke loaded against producers with one more field
    # checks that field too. A hand-typed list in smoke.py would not follow.
    monkeypatch.setattr(proposal, "TIP_FIELDS", (*proposal.TIP_FIELDS, "future_field"))
    future_item = dataclasses.make_dataclass(
        "PrintItem", [("name", str), ("action", str), ("future_field", str)], frozen=True
    )
    monkeypatch.setattr(printing, "PrintItem", future_item)

    smoke = load_smoke(monkeypatch)

    assert smoke.REVIEWED_FIELDS is proposal.TIP_FIELDS
    assert smoke.PENDING_FIELDS == (*PENDING_TABLE[:-1], "future_field", "source_ref")
    assert smoke.PRINTED_FIELDS == (*PRINT_TABLE, "future_field")


@pytest.mark.parametrize("name", PENDING_TABLE)
def test_a_pending_tip_drifted_in_one_field_fails_the_review_and_cleans_up(
    name: str,
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    drift = Drift(name)

    def drift_one_tip(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        drift.apply(sorted(pending, key=lambda tip: tip["id"]))
        return pending

    tamper = Tamper("GET", "/tips?status=pending", drift_one_tip)
    tamper_with(smoke, monkeypatch, tamper)

    run = run_main(smoke, capsys)

    assert tamper.applied == 1
    assert "phase 5/7 review: ok" not in run.out  # nothing was approved
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        f"FAIL: phase 5/7 review: the pending tips differ from the proposal: {drift.line('tip')}",
    )


def swap_first_two_ids(pending: list[dict[str, Any]]) -> tuple[Any, Any]:
    """The same tips, but the first two proposed swap ids, so the order by id changes."""

    first, second = sorted(pending, key=lambda tip: tip["id"])[:2]
    first["id"], second["id"] = second["id"], first["id"]
    return second["name"], first["name"]


def drop_a_null_field(pending: list[dict[str, Any]]) -> tuple[Any, Any]:
    """The fourth tip loses a field whose value is null, which must not pass as null."""

    tip = sorted(pending, key=lambda tip: tip["id"])[DRIFTED_ROW]
    name = next(key for key in proposal.TIP_FIELDS if tip[key] is None)
    del tip[name]
    return name, None


@pytest.mark.parametrize(
    ("rewrite", "drift"),
    [
        (swap_first_two_ids, "tip 1/10 name is {0!r}, expected {1!r}"),
        (drop_a_null_field, "tip 4/10 {0} is missing, expected None"),
    ],
    ids=["two-tips-swap-ids", "a-null-field-dropped"],
)
def test_a_pending_list_with_the_right_count_but_other_tips_fails_the_review(
    rewrite: Callable[[list[dict[str, Any]]], tuple[Any, Any]],
    drift: str,
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[tuple[Any, Any]] = []

    def apply(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert len(pending) == FIXTURE_TIPS
        seen.append(rewrite(pending))
        return pending

    tamper_with(smoke, monkeypatch, Tamper("GET", "/tips?status=pending", apply))

    run = run_main(smoke, capsys)

    assert len(seen) == 1
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 5/7 review: the pending tips differ from the proposal: "
        + drift.format(*seen[0]),
    )


def test_a_pending_list_missing_a_proposed_tip_fails_the_review_and_cleans_up(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def drop_one_tip(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert len(pending) == FIXTURE_TIPS
        return [tip for index, tip in enumerate(pending) if index != 3]

    tamper = Tamper("GET", "/tips?status=pending", drop_one_tip)
    tamper_with(smoke, monkeypatch, tamper)

    run = run_main(smoke, capsys)

    assert tamper.applied == 1
    assert "phase 5/7 review: ok" not in run.out  # nothing was approved
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 5/7 review: the pending tips differ from the proposal: 9 tips, expected 10",
        probe_port=True,
    )


@pytest.mark.parametrize("name", PRINT_TABLE)
def test_a_printed_row_drifted_in_one_field_fails_the_print_and_cleans_up(
    name: str,
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    drift = Drift(name)

    def drift_one_row(sheet: dict[str, Any]) -> dict[str, Any]:
        drift.apply(sheet["items"])
        return sheet

    tamper = Tamper("GET", "/print/lists/7", drift_one_row, when="after-approval")
    tamper_with(smoke, monkeypatch, tamper)

    run = run_main(smoke, capsys)

    assert tamper.applied == 1
    assert "phase 5/7 review: ok (approved 10 tips)" in run.out
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 6/7 print: list 7 prints other rows than the approved tips: "
        + drift.line("row"),
    )


def replace_a_row_with_another_tip(items: list[dict[str, Any]]) -> str:
    was = items[DRIFTED_ROW]
    items[DRIFTED_ROW] = dict(items[DRIFTED_ROW + 1])
    return f"row 4/10 name is {items[DRIFTED_ROW]['name']!r}, expected {was['name']!r}"


def swap_two_rows(items: list[dict[str, Any]]) -> str:
    was = items[DRIFTED_ROW]
    items[DRIFTED_ROW], items[DRIFTED_ROW + 1] = items[DRIFTED_ROW + 1], was
    return f"row 4/10 name is {items[DRIFTED_ROW]['name']!r}, expected {was['name']!r}"


def add_a_field_to_a_row(items: list[dict[str, Any]]) -> str:
    items[DRIFTED_ROW]["bucket"] = limits.BUCKET_DAILY
    return f"row 4/10 bucket is {limits.BUCKET_DAILY!r}, expected missing"


@pytest.mark.parametrize(
    "rewrite",
    [replace_a_row_with_another_tip, swap_two_rows, add_a_field_to_a_row],
    ids=["another-tip-in-a-row", "two-rows-swapped", "a-row-with-another-field"],
)
def test_a_print_sheet_with_the_right_count_but_other_rows_fails_the_print(
    rewrite: Callable[[list[dict[str, Any]]], str],
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def apply(sheet: dict[str, Any]) -> dict[str, Any]:
        assert len(sheet["items"]) == FIXTURE_TIPS
        seen.append(rewrite(sheet["items"]))
        assert len(sheet["items"]) == FIXTURE_TIPS
        return sheet

    tamper_with(smoke, monkeypatch, Tamper("GET", "/print/lists/7", apply, when="after-approval"))

    run = run_main(smoke, capsys)

    assert len(seen) == 1
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        f"FAIL: phase 6/7 print: list 7 prints other rows than the approved tips: {seen[0]}",
    )


STRANGER = {"name": "A tip nobody approved", "action": "Do this once before your first coffee"}


def drop_last_item(sheet: dict[str, Any]) -> dict[str, Any]:
    assert len(sheet["items"]) == FIXTURE_TIPS
    return {**sheet, "items": sheet["items"][:-1]}


def add_unapproved_item(sheet: dict[str, Any]) -> dict[str, Any]:
    assert len(sheet["items"]) == FIXTURE_TIPS
    return {**sheet, "items": [*sheet["items"], STRANGER]}


@pytest.mark.parametrize(
    ("rewrite", "rows"),
    [(drop_last_item, 9), (add_unapproved_item, 11)],
    ids=["missing-an-approved-tip", "with-an-unapproved-tip"],
)
def test_a_print_sheet_other_than_the_approved_tips_fails_the_smoke_and_cleans_up(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rewrite: Callable[[dict[str, Any]], dict[str, Any]],
    rows: int,
) -> None:
    # Only the sheet printed after the approval is rewritten; the one before stays empty.
    tamper = Tamper("GET", "/print/lists/7", rewrite, when="after-approval")
    tamper_with(smoke, monkeypatch, tamper)

    run = run_main(smoke, capsys)

    assert tamper.applied == 1
    assert "phase 5/7 review: ok (approved 10 tips)" in run.out
    assert "phase 6/7 print: ok" not in run.out
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 6/7 print: list 7 prints other rows than the approved tips: "
        f"{rows} rows, expected 10",
        probe_port=True,
    )


# --- bad server answers: every other HTTP check fails the smoke ----------------------------


def http_error(code: int, body: bytes = b"") -> Callable[[Any], Any]:
    def fail(answer: Any) -> Any:
        raise urllib.error.HTTPError(
            "http://injected", code, "injected", Message(), io.BytesIO(body)
        )

    return fail


def raising(error: BaseException) -> Callable[[Any], Any]:
    def fail(answer: Any) -> Any:
        raise error

    return fail


NOT_JSON = json.JSONDecodeError("Expecting value", "<html>", 0)


@dataclass(frozen=True)
class BadAnswer:
    """One rewritten answer and the start of the failure line it must cause, in which
    the text ``{port}`` and ``{data_dir}`` stands for this run's (no ``str.format``: the
    lines hold the reprs of JSON objects)."""

    method: str
    path: str
    rewrite: Callable[[Any], Any]
    failure: str
    when: str = "always"
    settings: dict[str, Any] = field(default_factory=dict)
    """Smoke constants this row sets."""
    applies: bool = True
    """False when the check may fire before the server's first answer."""


BAD_ANSWERS = {
    "health-not-ok": BadAnswer(
        "GET",
        "/health",
        lambda health: {**health, "ok": False},
        "FAIL: phase 4/7 serve: GET /api/health answered {'ok': False, ",
    ),
    "health-names-another-data-dir": BadAnswer(
        "GET",
        "/health",
        lambda health: {**health, "data_dir": "another-data-dir"},
        "FAIL: phase 4/7 serve: the server on port {port} serves another-data-dir, "
        "not this smoke's {data_dir}",
    ),
    "health-http-500": BadAnswer(
        "GET",
        "/health",
        http_error(500),
        "FAIL: phase 4/7 serve: GET /api/health answered HTTP 500",
    ),
    "health-not-json": BadAnswer(
        "GET",
        "/health",
        raising(NOT_JSON),
        f"FAIL: phase 4/7 serve: GET /api/health answered no JSON: {NOT_JSON}",
    ),
    "health-never-answers": BadAnswer(
        "GET",
        "/health",
        raising(urllib.error.URLError("injected refusal")),
        "FAIL: phase 4/7 serve: the server did not answer http://127.0.0.1:{port}/api/health "
        "within 1 s",
        settings={"READY_TIMEOUT_S": 1.0},
        applies=False,
    ),
    "pending-tip-with-a-string-id": BadAnswer(
        "GET",
        "/tips?status=pending",
        lambda pending: [{**pending[0], "id": str(pending[0]["id"])}, *pending[1:]],
        "FAIL: phase 5/7 review: GET /api/tips answered [{'id': '",
    ),
    "pending-http-503": BadAnswer(
        "GET",
        "/tips?status=pending",
        http_error(503, b"busy"),
        "FAIL: phase 5/7 review: GET /api/tips?status=pending answered HTTP 503: busy",
    ),
    "pending-connection-lost": BadAnswer(
        "GET",
        "/tips?status=pending",
        raising(urllib.error.URLError("connection reset")),
        "FAIL: phase 5/7 review: GET /api/tips?status=pending failed: "
        "<urlopen error connection reset>",
    ),
    "pending-not-json": BadAnswer(
        "GET",
        "/tips?status=pending",
        raising(NOT_JSON),
        f"FAIL: phase 5/7 review: GET /api/tips?status=pending answered no JSON: {NOT_JSON}",
    ),
    "a-row-prints-before-approval": BadAnswer(
        "GET",
        "/print/lists/7",
        lambda sheet: {**sheet, "items": [STRANGER]},
        f"FAIL: phase 5/7 review: list 7 already prints 1 row(s) before any approval: "
        f"[{STRANGER!r}]",
        when="before-approval",
    ),
    "sheet-without-items-before-approval": BadAnswer(
        "GET",
        "/print/lists/7",
        lambda sheet: {key: value for key, value in sheet.items() if key != "items"},
        "FAIL: phase 5/7 review: GET /api/print/lists/7 answered {'title': ",
        when="before-approval",
    ),
    "a-printed-row-not-an-object": BadAnswer(
        "GET",
        "/print/lists/7",
        lambda sheet: {**sheet, "items": [*sheet["items"][:-1], "a bare string"]},
        "FAIL: phase 6/7 print: GET /api/print/lists/7 answered {'title': ",
        when="after-approval",
    ),
    "approve-miscounts": BadAnswer(
        "POST",
        "/review/approve",
        lambda answer: {"approved": answer["approved"] - 1},
        "FAIL: phase 5/7 review: POST /api/review/approve answered {'approved': 9}",
    ),
}


@pytest.mark.parametrize("row", list(BAD_ANSWERS))
def test_a_bad_server_answer_fails_its_check_and_cleans_up(
    row: str,
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = BAD_ANSWERS[row]
    for name, value in bad.settings.items():
        monkeypatch.setattr(smoke, name, value)
    tamper = Tamper(bad.method, bad.path, bad.rewrite, when=bad.when)
    tamper_with(smoke, monkeypatch, tamper)

    run = run_main(smoke, capsys)

    if bad.applies:
        assert tamper.applied == 1
    port = run.value("port")
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        bad.failure.replace("{port}", port).replace("{data_dir}", run.data_dir),
    )


def test_a_server_that_exits_at_once_fails_the_serve_phase_and_cleans_up(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `serve --port 0` is a usage error (exit 2) before anything listens.
    monkeypatch.setattr(smoke, "free_port", lambda: 0)

    run = run_main(smoke, capsys)

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 4/7 serve: the server exited 2 before it answered /api/health",
    )
    assert "is not a port number from 1 to 65535" in run.err  # the log tail


def test_a_server_that_outlives_its_kill_fails_the_stop_and_is_stopped_by_the_cleanup(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Phase 7's kill does nothing; the cleanup's kill is the real one.
    kills: list[int] = []

    def first_kill_misses(process: subprocess.Popen[bytes]) -> None:
        kills.append(process.pid)
        if len(kills) > 1:
            processes.kill_tree(process)

    patched = SimpleNamespace(**vars(processes))
    patched.kill_tree = first_kill_misses
    patched.KILL_WAIT_S = 0.5
    monkeypatch.setattr(smoke, "processes", patched)

    run = run_main(smoke, capsys)

    pid = started_servers[0].pid
    assert kills == [pid, pid]
    assert "phase 6/7 print: ok" in run.out
    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        f"FAIL: phase 7/7 stop: the server (pid {pid}) still runs 0.5 s after its process "
        "tree was killed",
        probe_port=True,
    )


def test_a_temp_dir_that_cannot_be_removed_fails_the_smoke(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def locked(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "injected lock", str(path))

    patched = SimpleNamespace(**vars(shutil))
    patched.rmtree = locked
    monkeypatch.setattr(smoke, "REMOVE_ATTEMPTS", 2)
    monkeypatch.setattr(smoke, "REMOVE_DELAY_S", 0.0)
    monkeypatch.setattr(smoke, "shutil", patched)

    run = run_main(smoke, capsys)
    work_dir = Path(run.value("work_dir"))
    left_behind = work_dir.exists()
    shutil.rmtree(work_dir)

    assert run.code == 1, run.out + run.err
    assert left_behind  # the smoke said so, in phase 7 and again in the cleanup
    refusal = f"cannot remove the temp directory {work_dir} after 2 attempts: "
    assert len(run.failures) == 2, run.err
    assert run.failures[0].startswith(f"FAIL: phase 7/7 stop: {refusal}"), run.err
    assert run.failures[1].startswith(f"FAIL: cleanup: {refusal}"), run.err
    assert "PASS" not in run.out
    assert started_servers[0].poll() is not None
    assert refuses(int(run.value("port")))


# --- the commands and the packet: checks before the server ---------------------------------


def tamper_cli(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    rewrite: Callable[[Path, Any], Any],
) -> list[str]:
    """Wrap the smoke's ``run_cli`` so ``rewrite(data_dir, result)`` replaces the result of
    the real ``command``. Returns the rewritten commands, as a count."""

    real_run_cli = smoke.run_cli
    applied: list[str] = []

    def run_cli(data_dir: Path, *args: str, timeout: float | None = None) -> Any:
        result = real_run_cli(data_dir, *args, timeout=timeout)
        if args[0] == command:
            applied.append(command)
            result = rewrite(data_dir, result)
        return result

    monkeypatch.setattr(smoke, "run_cli", run_cli)
    return applied


def edit_context(edit: Callable[[dict[str, Any]], Any]) -> Callable[[Path, Any], Any]:
    """A ``context`` rewrite: ``edit`` changes the decoded JSON in place."""

    def rewrite(data_dir: Path, result: Any) -> Any:
        payload = json.loads(result.out)
        edit(payload)
        return dataclasses.replace(result, out=json.dumps(payload))

    return rewrite


def replace_out(text: str) -> Callable[[Path, Any], Any]:
    return lambda data_dir, result: dataclasses.replace(result, out=text)


def drop(key: str) -> Callable[[dict[str, Any]], None]:
    def edit(payload: dict[str, Any]) -> None:
        del payload[key]

    return edit


def drift_packet_sha256(payload: dict[str, Any]) -> None:
    payload["source"]["packet_sha256"] = "0" * 64


def drop_last_chapter(payload: dict[str, Any]) -> None:
    payload["source"]["chapters"].pop()


def chapters_not_a_list(payload: dict[str, Any]) -> None:
    payload["source"]["chapters"] = {}


CONTEXT_FAILURES = {
    "not-json": (
        replace_out("context is unavailable\n"),
        "FAIL: phase 3/7 propose: `context` printed no JSON object (Expecting value: line 1 "
        "column 1 (char 0)):",
    ),
    "a-json-array": (
        replace_out("[]\n"),
        "FAIL: phase 3/7 propose: `context` printed list, not a JSON object",
    ),
    "no-skill-sha256": (
        edit_context(drop("skill_sha256")),
        "FAIL: phase 3/7 propose: `context` printed no skill_sha256 or no source block",
    ),
    "no-source-block": (
        edit_context(drop("source")),
        "FAIL: phase 3/7 propose: `context` printed no skill_sha256 or no source block",
    ),
    "another-packet-sha256": (
        edit_context(drift_packet_sha256),
        f"FAIL: phase 3/7 propose: `context` gave packet_sha256 {'0' * 64}, but the packet file ",
    ),
    "chapters-not-a-list": (
        edit_context(chapters_not_a_list),
        "FAIL: phase 3/7 propose: `context` gave chapters {}, not a list of objects",
    ),
    "a-chapter-missing": (
        edit_context(drop_last_chapter),
        "FAIL: phase 3/7 propose: `context` gave other chapters than the packet file holds: "
        "9 chapters, expected 10",
    ),
}


@pytest.mark.parametrize("row", list(CONTEXT_FAILURES))
def test_a_bad_context_fails_the_propose_phase_and_cleans_up(
    row: str,
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rewrite, failure = CONTEXT_FAILURES[row]
    applied = tamper_cli(smoke, monkeypatch, "context", rewrite)

    run = run_main(smoke, capsys)

    assert applied == ["context"]
    assert_failed_and_cleaned_up(run, started_servers, temp_root, failure, server=False)


@pytest.mark.parametrize("name", CHAPTER_TABLE)
def test_a_context_chapter_drifted_in_one_field_fails_the_propose_phase(
    name: str,
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    drift = Drift(name)
    tamper_cli(
        smoke, monkeypatch, "context", edit_context(lambda c: drift.apply(c["source"]["chapters"]))
    )

    run = run_main(smoke, capsys)

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 3/7 propose: `context` gave other chapters than the packet file holds: "
        + drift.line("chapter"),
        server=False,
    )


def test_an_init_that_writes_no_database_fails_the_init_phase(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def lose_the_database(data_dir: Path, result: Any) -> Any:
        (data_dir / paths.DB_FILENAME).unlink()
        return result

    tamper_cli(smoke, monkeypatch, "init", lose_the_database)

    run = run_main(smoke, capsys)

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        f"FAIL: phase 1/7 init: `checklist-care init` exited 0 but wrote no {paths.DB_FILENAME}",
        server=False,
    )


def test_a_command_over_its_time_limit_is_killed_and_fails_the_smoke(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(smoke, "CLI_TIMEOUT_S", 0.01)  # no interpreter starts that fast

    run = run_main(smoke, capsys)

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 1/7 init: `checklist-care init` gave no result within 0.01 s; its process "
        "tree was killed",
        server=False,
    )


@pytest.mark.parametrize(
    ("command", "phase"),
    [("init", "phase 1/7 init"), ("serve", "phase 4/7 serve")],
    ids=["init", "serve"],
)
def test_a_command_that_cannot_start_fails_its_phase_and_cleans_up(
    command: str,
    phase: str,
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_cli_command = smoke.cli_command

    def missing_interpreter(data_dir: Path, *args: str) -> list[str]:
        argv: list[str] = real_cli_command(data_dir, *args)
        if args[0] == command:
            argv[0] = str(tmp_path / "no-such-python.exe")
        return argv

    monkeypatch.setattr(smoke, "cli_command", missing_interpreter)

    run = run_main(smoke, capsys)

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        f"FAIL: {phase}: cannot start `checklist-care {command}`: ",
        server=False,
    )


def test_a_packet_that_cannot_be_copied_fails_the_packet_phase(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def packets_is_a_file(data_dir: Path, result: Any) -> Any:
        (data_dir / paths.PACKETS_DIR).rmdir()
        (data_dir / paths.PACKETS_DIR).write_bytes(b"")
        return result

    tamper_cli(smoke, monkeypatch, "init", packets_is_a_file)

    run = run_main(smoke, capsys)

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 2/7 packet: cannot copy the packet to ",
        server=False,
    )


def test_a_packet_that_cannot_be_read_back_fails_the_propose_phase(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def remove_the_packet(data_dir: Path, result: Any) -> Any:
        for packet_file in (data_dir / paths.PACKETS_DIR).iterdir():
            packet_file.unlink()
        return result

    tamper_cli(smoke, monkeypatch, "context", remove_the_packet)

    run = run_main(smoke, capsys)

    assert_failed_and_cleaned_up(
        run, started_servers, temp_root, "FAIL: phase 3/7 propose: cannot read ", server=False
    )


def test_a_packet_without_chapters_fails_the_propose_phase(
    smoke: ModuleType,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The smoke builds its proposal from chapters, so none would make a vacuous pass.
    def no_chapters(packet: dict[str, Any]) -> None:
        packet["chapters"] = []

    bare = packet_variant(tmp_path, "packet_no_chapters.json", no_chapters)

    run = run_main(smoke, capsys, "--offline", str(bare))

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 3/7 propose: the packet of youtube:LpeDuC2sQ-w has no chapters; ",
        server=False,
    )


# --- the port checks and the options --------------------------------------------------------


def test_the_port_checks_see_a_listener(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red anchor for both port checks: a port something listens on is not "refused".
    monkeypatch.setattr(smoke, "REFUSE_WAIT_S", 0.0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((HOST, 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        assert refuses(port) is False
        assert smoke.port_state(port) == "accepts connections"
        with pytest.raises(smoke.SmokeFailure, match="accepts connections"):
            smoke.check_port_refuses(port)
    assert refuses(port) is True
    smoke.check_port_refuses(port)


def test_the_source_options_are_required_and_exclusive(smoke: ModuleType, temp_root: Path) -> None:
    parser = smoke.build_parser()

    assert parser.parse_args(["--live"]).live is True
    assert parser.parse_args(["--offline", FIXTURE_ARG]).offline == Path(FIXTURE_ARG)
    for argv in ([], ["--live", "--offline", FIXTURE_ARG]):
        assert smoke.main(argv) == 2, argv
    assert sorted(temp_root.iterdir()) == []  # a usage error creates nothing


# --- --live, with fetch in-process and its network seams faked -------------------------------


def fixture_packet() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_bytes().decode("utf-8"))
    return payload


class FakeTranscriptClient:
    """Returns the fixture's segments as the transcript library's own objects."""

    def fetch(self, video_id: str, languages: Iterable[str] = ("en",)) -> FetchedTranscript:
        return FetchedTranscript(
            snippets=[
                FetchedTranscriptSnippet(
                    text=segment["text"], start=segment["start"], duration=segment["duration"]
                )
                for segment in fixture_packet()["transcript"]["segments"]
            ],
            video_id=video_id,
            language="English (auto-generated)",
            language_code="en",
            is_generated=True,
        )


def fake_ytdlp(url: str) -> dict[str, Any]:
    packet = fixture_packet()
    return {
        "id": packet["source_ref"].partition(":")[2],
        "title": packet["title"],
        "channel": packet["channel"],
        "duration": packet["duration_s"],
        "chapters": [
            {
                "title": chapter["title"],
                "start_time": float(chapter["start_s"]),
                "end_time": float(chapter["end_s"]),
            }
            for chapter in packet["chapters"]
        ],
    }


def unchanged(out: str) -> str:
    return out


@dataclass
class FetchSeam:
    """The in-process ``fetch``: the URLs it fetched, and a rewrite of its stdout."""

    urls: list[str] = field(default_factory=list)
    rewrite: Callable[[str], str] = unchanged


@pytest.fixture
def in_process_fetch(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[FetchSeam]:
    """Route the smoke's ``fetch`` command to ``cli.main`` in this process, with the two
    network seams faked; every other command still runs as a real subprocess."""

    monkeypatch.setattr(youtube, "make_transcript_client", FakeTranscriptClient)
    monkeypatch.setattr(youtube, "run_ytdlp", fake_ytdlp)
    real_run_cli = smoke.run_cli
    seam = FetchSeam()

    def run_cli(data_dir: Path, *args: str, timeout: float | None = None) -> Any:
        if args[0] != "fetch":
            return real_run_cli(data_dir, *args, timeout=timeout)
        assert timeout == smoke.FETCH_TIMEOUT_S
        seam.urls.append(args[1])
        out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
        # Text streams with reconfigure(), like the real ones cli.main reconfigures.
        out = io.TextIOWrapper(out_bytes, encoding="utf-8", newline="")
        err = io.TextIOWrapper(err_bytes, encoding="utf-8", newline="")
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--data-dir", str(data_dir), *args])
        out.flush()
        err.flush()
        return smoke.CliResult(
            command=f"checklist-care {args[0]}",
            code=code,
            out=seam.rewrite(out_bytes.getvalue().decode("utf-8")),
            err=err_bytes.getvalue().decode("utf-8"),
        )

    monkeypatch.setattr(smoke, "run_cli", run_cli)
    yield seam


def test_the_fetch_mode_fetches_the_in_sample_video_and_runs_the_whole_cycle(
    smoke: ModuleType,
    in_process_fetch: FetchSeam,
    temp_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = run_main(smoke, capsys, "--live")

    assert run.code == 0, run.out + run.err
    assert in_process_fetch.urls == ["https://www.youtube.com/watch?v=LpeDuC2sQ-w"]
    assert "phase 2/7 packet: ok (youtube:LpeDuC2sQ-w, 10 chapters," in run.out
    assert "phase 6/7 print: ok (list 7 prints the 10 approved tips)" in run.out
    assert run.out.splitlines()[-1].startswith("PASS in ")
    assert refuses(int(run.value("port")))
    assert sorted(temp_root.iterdir()) == []


def test_the_fetch_mode_reports_a_fetch_error_as_exit_4(
    smoke: ModuleType,
    in_process_fetch: FetchSeam,
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BlockedClient:
        def fetch(self, video_id: str, languages: Iterable[str] = ("en",)) -> FetchedTranscript:
            raise OSError("network is unreachable")

    monkeypatch.setattr(youtube, "make_transcript_client", BlockedClient)

    run = run_main(smoke, capsys, "--live")

    assert run.code == 4, run.out + run.err
    assert "FAIL: phase 2/7 packet: `checklist-care fetch" in run.err
    assert "LIVE: BLOCKED-EXTERNAL" in run.err
    assert "CC_E_FETCH" in run.err
    assert sorted(temp_root.iterdir()) == []


def fetch_line_rewrite(key: str, change: Callable[[str], str | None]) -> Callable[[str], str]:
    """A rewrite of ``fetch``'s stdout: ``change`` maps the value of the ``key`` line to a
    new value, or to None to drop the line."""

    def rewrite(out: str) -> str:
        lines = []
        for line in out.splitlines(keepends=True):
            if line.startswith(f"{key}: "):
                value = change(line[len(key) + 2 :].rstrip("\n"))
                if value is None:
                    continue
                line = f"{key}: {value}\n"
            lines.append(line)
        return "".join(lines)

    return rewrite


@pytest.mark.parametrize("key", list(FETCH_FAILURES))
def test_a_fetch_that_prints_no_key_line_fails_the_packet_phase(
    key: str,
    smoke: ModuleType,
    in_process_fetch: FetchSeam,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    in_process_fetch.rewrite = fetch_line_rewrite(key, lambda value: None)

    run = run_main(smoke, capsys, "--live")

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        f"FAIL: phase 2/7 packet: `fetch` printed no {key} line:",
        server=False,
    )


@pytest.mark.parametrize("key", list(FETCH_FAILURES))
def test_a_fetch_that_prints_a_drifted_value_fails_the_packet_phase(
    key: str,
    smoke: ModuleType,
    in_process_fetch: FetchSeam,
    started_servers: list[subprocess.Popen[bytes]],
    temp_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, str] = {}

    def change(value: str) -> str:
        seen["was"], seen["now"] = value, drifted(value)
        return seen["now"]

    in_process_fetch.rewrite = fetch_line_rewrite(key, change)

    run = run_main(smoke, capsys, "--live")

    assert_failed_and_cleaned_up(
        run,
        started_servers,
        temp_root,
        "FAIL: phase 2/7 packet: " + FETCH_FAILURES[key].format(**seen),
        server=False,
    )
