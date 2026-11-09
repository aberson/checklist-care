"""Child processes: no console window, and whole-tree kills.

Used by the yt-dlp runner (``sources/youtube.py``) and by the npm build of
``checklist-care serve --build`` (``server/run.py``). On Windows every child is
started with ``CREATE_NO_WINDOW``, and a kill goes to the whole process tree:
``npm`` is a ``.cmd`` whose children do the work, and the venv ``python.exe`` is
a launcher whose child is the real interpreter, so killing only the direct child
leaves the descendants running and the output pipes open.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress
from typing import Final

NO_WINDOW: Final = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
"""``creationflags`` for every child process: no console window on Windows, 0 elsewhere."""

NEW_SESSION: Final = os.name != "nt"
"""``start_new_session`` for every child process, so :func:`kill_tree` can kill its
process group outside Windows."""

KILL_WAIT_S: Final = 10.0
"""How long to wait for a killed process tree to release its pipes."""


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the process and every descendant, then reap it."""

    if sys.platform == "win32":
        if process.poll() is None:
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                    timeout=KILL_WAIT_S,
                    check=False,
                )
    else:
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
    with suppress(OSError):
        process.kill()
    with suppress(subprocess.SubprocessError, ValueError, OSError):
        process.communicate(timeout=KILL_WAIT_S)
