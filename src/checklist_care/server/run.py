"""``checklist-care serve [--port 8450] [--build] [--demo]`` (plan §5).

* The host is always :data:`HOST` (``127.0.0.1``); there is no ``--host`` option.
* uvicorn runs exactly one worker: SQLite in WAL mode has one writer.
* ``--build`` runs npm on the absolute :data:`WEB_DIR`, so the working directory
  does not matter: ``install`` when ``web/node_modules`` is absent, then
  ``run build``. npm is looked up in the absolute ``PATH`` entries only, never in
  the working directory (:func:`find_npm`). A missing npm, or an npm command that
  fails, exits 2 (``CC_E_CONFIG``).
* ``--demo`` serves synthetic data (``server/demo.py``) from a fresh
  ``%TEMP%\\checklist-care-demo\\<pid>-<8hex>``. It first sweeps every sibling
  directory whose pid is no longer alive, and removes its own directory when the
  server stops. Liveness on Windows is ``OpenProcess`` + ``GetExitCodeProcess``
  (ctypes), where ``STILL_ACTIVE`` (259) means alive. Never ``os.kill(pid, 0)``
  there: on Windows it terminates the process.

Two seams start child work, and tests replace them: :func:`run_command` (each npm
command) and :func:`run_server` (uvicorn); a third, :func:`find_npm`, finds the
program. :func:`serve` looks them up as module globals at call time, so
``monkeypatch.setattr(run, "run_server", fake)`` reaches it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import uvicorn
from fastapi import FastAPI

from checklist_care.errors import EXIT_OK, ConfigError
from checklist_care.processes import NEW_SESSION, NO_WINDOW, kill_tree
from checklist_care.server import BUILD_SCRIPT, HOST, WEB_DIR, base_url, demo
from checklist_care.server.app import create_app

NPM: Final = "npm"
"""The program ``--build`` runs, found by :func:`find_npm`."""

NPM_INSTALL_ARGS: Final = ("install", "--prefer-offline", "--no-audit", "--no-fund")
"""``npm --prefix <WEB_DIR> install …``: the cache first, no audit or funding output."""

WORKERS: Final = 1
"""uvicorn workers. More than one would put several writers on one SQLite file."""

DEMO_DIR_NAME: Final = "checklist-care-demo"
"""The folder under the system temp directory that holds the demo data directories."""

DEMO_ENTRY_PATTERN: Final = re.compile(r"(?P<pid>[0-9]+)-[0-9a-f]{8}")
"""A demo data directory name, ``<pid>-<8hex>``, matched with ``fullmatch``. The sweep
never touches an entry that does not match."""

PID_MAX: Final = 2**32 - 1 if sys.platform == "win32" else 2**31 - 1
"""The largest process id: a ``DWORD`` on Windows, a signed 32-bit ``pid_t`` on POSIX.
A larger number cannot be a process (ctypes would silently truncate it to another
pid), so :func:`pid_alive` says False and the sweep leaves such a directory alone."""

STILL_ACTIVE: Final = 259
"""``GetExitCodeProcess``'s exit code of a process that is still running."""

_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_ERROR_ACCESS_DENIED: Final = 5


def _err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# --- npm ------------------------------------------------------------------------------


def npm_commands(npm: str, *, install: bool, web_dir: Path = WEB_DIR) -> list[list[str]]:
    """The npm command lines of ``--build``: ``install`` (when asked), then ``run build``."""

    prefix = [npm, "--prefix", str(web_dir)]
    commands = [[*prefix, *NPM_INSTALL_ARGS]] if install else []
    commands.append([*prefix, "run", BUILD_SCRIPT])
    return commands


def run_command(command: Sequence[str]) -> int:
    """Run ``command`` without a console window and return its exit code.

    The child's output (stdout and stderr together) is decoded as UTF-8 and copied
    to stderr line by line, so stdout stays clean. An interrupt kills the whole
    process tree: npm is a ``.cmd`` whose node children do the work.
    """

    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
            start_new_session=NEW_SESSION,
        )
    except OSError as error:
        raise ConfigError(f"cannot start {command[0]}: {error}") from error
    try:
        output = process.stdout
        if output is not None:
            for raw in output:
                sys.stderr.write(raw.decode("utf-8", errors="replace"))
                sys.stderr.flush()
        return process.wait()
    except BaseException:
        kill_tree(process)
        raise


def find_npm(search_path: str | None = None) -> str | None:
    """The absolute path of ``npm`` in the first ``PATH`` directory that has it, or None.

    Only absolute ``PATH`` entries are searched, never the working directory: on
    Windows a bare ``shutil.which("npm")`` looks in the current directory first, so
    an ``npm.cmd`` planted in whatever folder the operator starts from would run.
    Each entry is passed to ``shutil.which`` as ``<entry>/npm``; a name with a
    directory part is looked up in that directory only (``PATHEXT`` still applies).
    """

    entries = os.environ.get("PATH", "") if search_path is None else search_path
    for entry in entries.split(os.pathsep):
        directory = entry.strip().strip('"')
        if not directory or not os.path.isabs(directory):
            continue
        found = shutil.which(os.path.join(directory, NPM))
        if found is not None and os.path.isabs(found):
            return found
    return None


def build_web(web_dir: Path = WEB_DIR) -> None:
    """``--build``: run :func:`npm_commands` on ``web_dir`` through :func:`run_command`."""

    npm = find_npm()
    if npm is None:
        raise ConfigError(
            "npm is not on PATH; install Node.js 24 with npm 11, or start without --build"
        )
    install = not (web_dir / "node_modules").is_dir()
    for command in npm_commands(npm, install=install, web_dir=web_dir):
        shown = subprocess.list2cmdline(command)
        _err(f"build: {shown}")
        code = run_command(command)
        if code != 0:
            raise ConfigError(f"`{shown}` exited {code}; see its output above")


# --- demo data directories --------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """True while the process ``pid`` runs.

    Windows: ``OpenProcess`` + ``GetExitCodeProcess``; a process that exists but
    cannot be opened (access denied) counts as alive, so its directory is kept. A
    number outside ``1..PID_MAX``, or one the OS refuses as a pid (OverflowError,
    ValueError), is not a process: False.
    """

    if not 0 < pid <= PID_MAX:
        return False
    try:
        return _os_pid_alive(pid)
    except (OverflowError, ValueError):
        return False


def _os_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return bool(ctypes.get_last_error() == _ERROR_ACCESS_DENIED)
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # unknown: keep the directory
            return bool(exit_code.value == STILL_ACTIVE)
        finally:
            kernel32.CloseHandle(handle)
    # POSIX only: signal 0 checks that the process exists and sends nothing.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def demo_root() -> Path:
    """``<system temp>/checklist-care-demo``, looked up at call time."""

    return Path(tempfile.gettempdir()) / DEMO_DIR_NAME


def demo_dir_pid(name: str) -> int | None:
    """The pid of a ``<pid>-<8hex>`` demo directory name, or None when ``name`` has
    another shape or its number cannot be a pid (outside ``1..PID_MAX``)."""

    match = DEMO_ENTRY_PATTERN.fullmatch(name)
    if match is None:
        return None
    try:
        pid = int(match["pid"])
    except ValueError:  # more digits than int() converts
        return None
    return pid if 0 < pid <= PID_MAX else None


def sweep_stale_demo_dirs(root: Path) -> list[Path]:
    """Remove each ``<pid>-<8hex>`` directory under ``root`` whose pid is not alive.

    Returns the removed directories. Entries with another name, links, junctions,
    files, names whose number cannot be a pid (not this program's) and the
    directories of live processes are left alone; a directory that cannot be removed
    is reported on stderr and kept.
    """

    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    removed: list[Path] = []
    for entry in entries:
        pid = demo_dir_pid(entry.name)
        if pid is None or entry.is_symlink() or entry.is_junction() or not entry.is_dir():
            continue
        if pid == os.getpid() or pid_alive(pid):
            continue
        try:
            shutil.rmtree(entry)
        except OSError as error:
            _err(f"warning: cannot remove stale demo directory {entry}: {error}")
            continue
        removed.append(entry)
    return removed


def create_demo_dir(root: Path | None = None) -> Path:
    """Sweep ``root`` (default :func:`demo_root`), then create this process's
    ``<pid>-<8hex>`` directory in it."""

    base = demo_root() if root is None else root
    try:
        base.mkdir(parents=True, exist_ok=True)
        for stale in sweep_stale_demo_dirs(base):
            _err(f"demo: removed stale {stale}")
        target = base / f"{os.getpid()}-{secrets.token_hex(4)}"
        target.mkdir()
    except OSError as error:
        raise ConfigError(f"cannot create a demo directory in {base}: {error}") from error
    return target


def remove_demo_dir(path: Path) -> None:
    """Remove this server's demo directory; a failure is reported, and the next
    ``--demo`` sweep removes it."""

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        _err(f"warning: cannot remove demo directory {path}: {error}")


# --- uvicorn --------------------------------------------------------------------------


def server_config(app: FastAPI, port: int) -> uvicorn.Config:
    """The uvicorn configuration: :data:`HOST`, ``port``, :data:`WORKERS` worker."""

    return uvicorn.Config(
        app, host=HOST, port=port, workers=WORKERS, lifespan="on", log_level="info"
    )


def run_server(config: uvicorn.Config) -> None:
    """Run uvicorn until it stops. A server that never started (the port is taken,
    or startup failed) is a ``ConfigError``; Ctrl+C is a normal stop."""

    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        return
    except SystemExit as exit_request:
        raise ConfigError(
            f"the server stopped during startup (exit {exit_request.code}); is another "
            f"program using port {config.port}? See the log above"
        ) from None
    if not server.started:
        raise ConfigError("the server did not start; see the log above")


# --- serve ----------------------------------------------------------------------------


def serve(db_path: Path, *, port: int, build: bool, demo_mode: bool) -> int:
    """``checklist-care serve``: build if asked, then serve the database at ``db_path``,
    or with ``demo_mode`` a fresh demo database instead, on ``127.0.0.1:<port>``."""

    if build:
        build_web()
    demo_dir: Path | None = None
    if demo_mode:
        demo_dir = create_demo_dir()
        _err(f"demo data_dir: {demo_dir}")
    try:
        served = db_path if demo_dir is None else demo.populate(demo_dir)
        app = create_app(served)
        _err(f"serving {base_url(port)}/ (Ctrl+C stops it)")
        run_server(server_config(app, port))
    finally:
        if demo_dir is not None:
            remove_demo_dir(demo_dir)
    return EXIT_OK
