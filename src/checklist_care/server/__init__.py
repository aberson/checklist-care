"""The local server (plan §5, §6): the ``/api`` routes and the built React app.

This module holds only the address and location constants, so a module that
needs one (``proposal.REVIEW_URL``) imports them without loading FastAPI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

HOST: Final = "127.0.0.1"
"""The only address the server binds: loopback, by construction (there is no ``--host``)."""

ALLOWED_HOSTS: Final = (HOST, "localhost")
"""The only ``Host`` header names a request may carry (any port). A page that rebinds its
own DNS name to 127.0.0.1 still sends that name, so the server refuses it with 400
instead of answering the browser with local data. The Vite dev proxy forwards the
browser's ``127.0.0.1:5450`` unchanged (``changeOrigin: false``), which passes."""

DAILY_PORT: Final = 8450
"""The default ``serve --port``: the daily server."""

API_PREFIX: Final = "/api"
"""Every API route is under this path (plan §6)."""

WEB_DIR: Final = Path(__file__).resolve().parents[3] / "web"
"""The checkout's ``web/`` app, absolute: ``src/checklist_care/server/`` is three levels
below the checkout root, so the path does not depend on the working directory."""

DIST_DIR: Final = WEB_DIR / "dist"
"""Where ``npm --prefix web run build`` writes the built app that the server serves."""

BUILD_SCRIPT: Final = "build"
"""The ``web/package.json`` script that writes :data:`DIST_DIR`."""

BUILD_COMMAND: Final = f"npm --prefix web run {BUILD_SCRIPT}"
"""The build command as the operator types it in the checkout root."""


def base_url(port: int = DAILY_PORT) -> str:
    """``http://127.0.0.1:<port>``, the root URL of a server on ``port``."""

    return f"http://{HOST}:{port}"
