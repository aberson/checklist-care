"""The web suites inside the one declared test command (plan §8, §11).

``uv run pytest`` is the full DONE gate, so it also runs, through npm:

* ``npm --prefix web run typecheck`` and ``npm --prefix web run test`` (vitest),
  once ``web/package.json`` exists;
* ``npm --prefix web run test:print`` (Playwright), once that script exists.

Each must exit 0. Two states fail and never skip: ``src/checklist_care/server/``
without ``web/package.json``, and ``web/src/print/`` without a ``test:print``
script. Every child runs without a console window, its output is decoded as
UTF-8, and a timeout kills its whole process tree (npm is a ``.cmd`` whose node
children do the work).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from checklist_care import processes

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PACKAGE_JSON = WEB / "package.json"
SERVER_PACKAGE = ROOT / "src" / "checklist_care" / "server"
PRINT_DIR = WEB / "src" / "print"

SUITE_TIMEOUT_S = 600.0
OUTPUT_TAIL_LINES = 60


def _scripts() -> dict[str, Any]:
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    scripts = package.get("scripts", {})
    assert isinstance(scripts, dict), "web/package.json `scripts` is not an object"
    return scripts


def _require_web_app() -> None:
    """Fail when the server exists without the web app; skip only before both exist."""

    if PACKAGE_JSON.is_file():
        return
    if SERVER_PACKAGE.is_dir():
        pytest.fail(
            f"{SERVER_PACKAGE} exists but web/package.json does not: "
            "the server serves the web app, so the web suites must exist and pass"
        )
    pytest.skip("no web app yet (web/package.json is created with the server)")


def _run_npm_script(script: str) -> tuple[int, str]:
    """Run ``npm --prefix web run <script>``; its exit code and its output tail."""

    npm = shutil.which("npm")
    assert npm is not None, "npm is not on PATH; install Node.js 24 with npm 11"
    assert (WEB / "node_modules").is_dir(), (
        "web/node_modules is missing: run `npm --prefix web install`"
    )
    env = dict(os.environ)
    env.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    process = subprocess.Popen(
        [npm, "--prefix", str(WEB), "run", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=ROOT,
        env=env,
        creationflags=processes.NO_WINDOW,
        start_new_session=processes.NEW_SESSION,
    )
    try:
        output, _ = process.communicate(timeout=SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        processes.kill_tree(process)
        pytest.fail(f"`npm --prefix web run {script}` took over {SUITE_TIMEOUT_S:g} s; killed")
    except BaseException:
        processes.kill_tree(process)
        raise
    text = output.decode("utf-8", errors="replace")
    tail = "\n".join(text.splitlines()[-OUTPUT_TAIL_LINES:])
    return process.returncode, tail


def test_the_web_app_exists_once_the_server_does() -> None:
    _require_web_app()
    scripts = _scripts()
    missing = [name for name in ("dev", "build", "typecheck", "test") if name not in scripts]
    assert missing == [], f"web/package.json lacks the scripts {missing}"


@pytest.mark.parametrize("script", ["typecheck", "test"])
def test_web_suite_passes(script: str) -> None:
    _require_web_app()
    assert script in _scripts(), f"web/package.json has no `{script}` script"

    code, tail = _run_npm_script(script)

    assert code == 0, f"`npm --prefix web run {script}` exited {code}:\n{tail}"


def test_print_suite_passes() -> None:
    _require_web_app()
    has_script = "test:print" in _scripts()
    if PRINT_DIR.exists() and not has_script:
        pytest.fail(
            "web/src/print/ exists but web/package.json has no `test:print` script: "
            "the print views must have their Playwright suite"
        )
    if not has_script:
        pytest.skip("no print suite yet (web/src/print/ and `test:print` come together)")

    code, tail = _run_npm_script("test:print")

    assert code == 0, f"`npm --prefix web run test:print` exited {code}:\n{tail}"


# --- red anchors: the two must-fail states and a real failing exit code -----------------


def _point_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, scripts: dict[str, str] | None
) -> None:
    """Point this module's paths at a fake checkout with a server package, and with a
    ``web/package.json`` holding ``scripts`` (none when ``scripts`` is None)."""

    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    package_json = web / "package.json"
    if scripts is not None:
        package_json.write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
    server_package = tmp_path / "src" / "checklist_care" / "server"
    server_package.mkdir(parents=True)
    this_module = sys.modules[__name__]
    monkeypatch.setattr(this_module, "PACKAGE_JSON", package_json)
    monkeypatch.setattr(this_module, "SERVER_PACKAGE", server_package)
    monkeypatch.setattr(this_module, "PRINT_DIR", web / "src" / "print")


def test_a_server_without_a_web_app_fails_not_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_at(monkeypatch, tmp_path, scripts=None)
    with pytest.raises(pytest.fail.Exception, match="web/package.json does not"):
        _require_web_app()


def test_print_views_without_a_print_suite_fail_not_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_at(monkeypatch, tmp_path, scripts={"test": "vitest run"})
    with pytest.raises(pytest.skip.Exception):
        test_print_suite_passes()
    (tmp_path / "web" / "src" / "print").mkdir()
    with pytest.raises(pytest.fail.Exception, match="no `test:print` script"):
        test_print_suite_passes()


def test_a_failing_npm_script_reports_its_exit_code() -> None:
    _require_web_app()
    code, tail = _run_npm_script("checklist-care-no-such-script")
    assert code != 0
    assert "checklist-care-no-such-script" in tail
