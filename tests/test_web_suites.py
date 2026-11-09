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

The print suite starts its own demo server on :data:`PRINT_SUITE_PORT`
(``web/playwright.config.ts``). The port must be free before the suite runs, and
free again afterwards: Playwright must have torn the server's process tree down.

The production build (``npm run build``, which ``serve --build`` runs) type-checks only
what ships, ``web/tsconfig.json``: it never reads ``web/e2e/``, the Playwright config or
Playwright's packages, so it succeeds in a checkout whose ``node_modules`` predates them.
``npm run typecheck`` also checks ``web/tsconfig.e2e.json``, so the gate still
type-checks every print-suite file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from checklist_care import processes
from checklist_care.server import HOST

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PACKAGE_JSON = WEB / "package.json"
SERVER_PACKAGE = ROOT / "src" / "checklist_care" / "server"
PRINT_DIR = WEB / "src" / "print"
PLAYWRIGHT_CONFIG = WEB / "playwright.config.ts"
E2E_DIR = WEB / "e2e"
SHIPPED_ENTRY = WEB / "src" / "main.tsx"

PRINT_SUITE_PACKAGES = frozenset({"@playwright", "playwright", "playwright-core"})
"""The ``node_modules`` packages only the print suite needs: ``@playwright/test`` and the
two packages it brings in."""

SUITE_TIMEOUT_S = 600.0
OUTPUT_TAIL_LINES = 60

PRINT_SUITE_PORT = 8452
"""The port of the print suite's demo server (plan §2): Playwright's ``webServer``."""

PORT_RELEASE_S = 15.0
"""How long the print suite's port may stay taken after the suite ends."""


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

    code, text = _run_npm(["run", script])
    tail = "\n".join(text.splitlines()[-OUTPUT_TAIL_LINES:])
    return code, tail


def _run_npm(arguments: list[str]) -> tuple[int, str]:
    """Run ``npm --prefix web <arguments>``; its exit code and its whole output."""

    command = " ".join(["npm --prefix web", *arguments])
    npm = shutil.which("npm")
    assert npm is not None, "npm is not on PATH; install Node.js 24 with npm 11"
    assert (WEB / "node_modules").is_dir(), (
        "web/node_modules is missing: run `npm --prefix web install`"
    )
    env = dict(os.environ)
    env.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    process = subprocess.Popen(
        [npm, "--prefix", str(WEB), *arguments],
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
        pytest.fail(f"`{command}` took over {SUITE_TIMEOUT_S:g} s; killed")
    except BaseException:
        processes.kill_tree(process)
        raise
    return process.returncode, output.decode("utf-8", errors="replace")


def _tsc_projects(script: str) -> list[Path]:
    """The tsconfig of each ``tsc`` command in the npm ``script``: its ``-p``/``--project``
    value, or ``tsconfig.json`` (tsc's default), relative to ``web/``."""

    projects: list[Path] = []
    for command in str(_scripts()[script]).split("&&"):
        words = command.split()
        if not words or words[0] != "tsc":
            continue
        project = "tsconfig.json"
        for flag, value in zip(words, words[1:], strict=False):
            if flag in ("-p", "--project"):
                project = value
        projects.append(WEB / project)
    return projects


def _checked_files(*projects: Path) -> set[Path]:
    """Every file ``tsc -p <project>`` reads, over ``projects`` (``tsc --listFilesOnly``)."""

    files: set[Path] = set()
    for project in projects:
        arguments = ["exec", "--no", "--", "tsc", "--listFilesOnly", "-p", str(project)]
        code, output = _run_npm(arguments)
        assert code == 0, f"`tsc --listFilesOnly -p {project}` exited {code}:\n{output[-4000:]}"
        listed = (Path(line.strip()) for line in output.splitlines() if line.strip())
        files.update(path.resolve() for path in listed if path.is_file())
    return files


def _print_suite_only(files: set[Path]) -> list[Path]:
    """The files among ``files`` that exist only for the print suite: ``web/e2e/``, the
    Playwright config, and the :data:`PRINT_SUITE_PACKAGES` in ``node_modules``."""

    e2e = E2E_DIR.resolve()
    config = PLAYWRIGHT_CONFIG.resolve()

    def in_print_suite_package(path: Path) -> bool:
        parts = path.parts
        return any(
            parts[index] == "node_modules" and parts[index + 1] in PRINT_SUITE_PACKAGES
            for index in range(len(parts) - 1)
        )

    return sorted(
        path
        for path in files
        if path == config or path.is_relative_to(e2e) or in_print_suite_package(path)
    )


def _port_listening(port: int) -> bool:
    """True while something accepts connections on ``127.0.0.1:<port>``."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((HOST, port)) == 0


def _wait_for_port_release(port: int, timeout_s: float = PORT_RELEASE_S) -> bool:
    """Wait until nothing listens on ``port``; False if it is still taken at the deadline."""

    deadline = time.monotonic() + timeout_s
    while _port_listening(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


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
    assert not _port_listening(PRINT_SUITE_PORT), (
        f"{HOST}:{PRINT_SUITE_PORT} is taken; the print suite starts its own demo server there"
    )

    code, tail = _run_npm_script("test:print")

    assert code == 0, f"`npm --prefix web run test:print` exited {code}:\n{tail}"
    assert _wait_for_port_release(PRINT_SUITE_PORT), (
        f"the print suite's demo server still listens on {HOST}:{PRINT_SUITE_PORT} "
        f"{PORT_RELEASE_S:g} s after the suite ended: its process tree was not torn down"
    )


PRINT_SERVER_COMMAND = ("uv", "run", "checklist-care", "serve", "--demo", "--port")
"""Plan §5's ``webServer`` command, before its port. The bundle comes from
``pretest:print`` (``npm run build``), so the command does not pass ``--build``."""


def _config_value(config: str, key: str) -> str | None:
    """The string literal (quoted or a template) after ``<key>:`` in the config, if any."""

    found = re.search(rf"\b{re.escape(key)}\s*:\s*([`\"'])(.*?)\1", config, re.DOTALL)
    return None if found is None else found.group(2)


def _web_server_problems(config: str) -> list[str]:
    """What keeps ``config`` (the text of ``web/playwright.config.ts``) from starting its own
    plan §5 demo server; empty when nothing does. Only the load-bearing values are matched,
    so layout, quoting and option order may change freely."""

    problems: list[str] = []
    constants = {
        name: found.group(2)
        for name, found in (
            (name, re.search(rf"\bconst\s+{name}\s*=\s*([`\"']?)([^`\"';\s]+)\1", config))
            for name in ("loopback", "port")
        )
        if found is not None
    }
    constants["baseURL"] = f"http://{constants.get('loopback')}:{constants.get('port')}"

    def resolved(key: str) -> str | None:
        value = _config_value(config, key)
        if value is None:
            return None
        return re.sub(r"\$\{(\w+)\}", lambda name: constants.get(name.group(1), "?"), value)

    if constants.get("loopback") != HOST:
        problems.append(f"the server's host is {constants.get('loopback')!r}, not {HOST!r}")
    if constants.get("port") != str(PRINT_SUITE_PORT):
        problems.append(f"the server's port is {constants.get('port')!r}, not {PRINT_SUITE_PORT}")
    command = (resolved("command") or "").split()
    expected = [*PRINT_SERVER_COMMAND, str(PRINT_SUITE_PORT)]
    if command != expected:
        problems.append(f"the webServer command is {command}, not {expected}")
    if _config_value(config, "cwd") != "..":
        problems.append("the webServer does not start from the checkout root (cwd: '..')")
    if resolved("url") != f"http://{HOST}:{PRINT_SUITE_PORT}/api/health":
        problems.append(f"the webServer waits for {resolved('url')!r}, not /api/health")
    if re.search(r"\breuseExistingServer\s*:\s*false\b", config) is None:
        problems.append("the webServer may reuse a running server (reuseExistingServer)")
    return problems


def test_the_print_suite_starts_its_own_demo_server() -> None:
    _require_web_app()
    if not PLAYWRIGHT_CONFIG.is_file():
        pytest.skip("no print suite yet")
    scripts = _scripts()

    assert _web_server_problems(PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")) == []
    assert scripts.get("pretest:print") == "npm run build"
    assert scripts.get("test:print") == "playwright test"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("serve --demo", "serve --build --demo"), "the webServer command is"),
        (("reuseExistingServer: false", "reuseExistingServer: true"), "reuseExistingServer"),
        (("const port = 8452", "const port = 8450"), "the server's port is"),
        (("/api/health", "/"), "/api/health"),
        (('cwd: ".."', 'cwd: "."'), "checkout root"),
    ],
)
def test_a_web_server_config_off_plan_is_caught(change: tuple[str, str], problem: str) -> None:
    """Red anchor: each load-bearing value, changed in the real config, is reported, while
    reformatting it (other quotes, other spacing) is not."""

    _require_web_app()
    if not PLAYWRIGHT_CONFIG.is_file():
        pytest.skip("no print suite yet")
    config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    old, new = change
    assert old in config
    reformatted = config.replace('cwd: ".."', "cwd :  '..'").replace(
        "reuseExistingServer: false", "reuseExistingServer:false"
    )
    assert _web_server_problems(reformatted) == []

    problems = _web_server_problems(config.replace(old, new))

    assert any(problem in line for line in problems), problems


# --- the production build checks only what ships; the typecheck checks the rest --------
#
# `serve --build` runs `npm install` only when web/node_modules is absent (plan §5), so it
# builds in checkouts whose node_modules predates the print suite's devDependencies. The
# build's tsc must therefore never read web/e2e/, web/playwright.config.ts or Playwright
# itself; `npm run typecheck` (run by test_web_suite_passes) type-checks those through
# web/tsconfig.e2e.json.


def test_the_production_build_reads_nothing_of_the_print_suite() -> None:
    _require_web_app()
    projects = _tsc_projects("build")
    assert projects, "web/package.json `build` runs no tsc: the shipped app is not type-checked"

    build_files = _checked_files(*projects)

    assert SHIPPED_ENTRY.resolve() in build_files, "the build's tsc does not read src/main.tsx"
    assert _print_suite_only(build_files) == [], (
        "the production build's tsc reads print-suite-only files, so `serve --build` fails "
        "in a checkout whose web/node_modules has no @playwright/test"
    )


def test_the_typecheck_covers_the_print_suite_and_everything_the_build_checks() -> None:
    _require_web_app()
    if not PLAYWRIGHT_CONFIG.is_file():
        pytest.skip("no print suite yet")
    build_files = _checked_files(*_tsc_projects("build"))
    typecheck_files = _checked_files(*_tsc_projects("typecheck"))
    suite_files = {path.resolve() for path in E2E_DIR.rglob("*.ts")}
    assert suite_files, "web/e2e/ holds no .ts files"
    suite_files.add(PLAYWRIGHT_CONFIG.resolve())
    own_build_files = {path for path in build_files if "node_modules" not in path.parts}

    assert sorted(suite_files - typecheck_files) == []
    assert sorted(own_build_files - typecheck_files) == []
    assert any("node_modules" in path.parts for path in _print_suite_only(typecheck_files)), (
        "the typecheck reads web/e2e/ but never reaches @playwright/test's types"
    )


def test_a_build_project_that_includes_the_print_suite_is_caught(tmp_path: Path) -> None:
    """Red anchor: round 1's shape (web/tsconfig.json including e2e/ and the Playwright
    config) is what the build check above rejects."""

    _require_web_app()
    if not PLAYWRIGHT_CONFIG.is_file():
        pytest.skip("no print suite yet")
    included = [WEB / "src", E2E_DIR, WEB / "vite.config.ts", PLAYWRIGHT_CONFIG]
    anchor = tmp_path / "tsconfig.json"
    anchor.write_text(
        json.dumps(
            {
                "extends": (WEB / "tsconfig.json").as_posix(),
                # vite/client resolves from web/, not from tmp_path; listing needs no types.
                "compilerOptions": {"types": []},
                "include": [path.as_posix() for path in included],
            }
        ),
        encoding="utf-8",
    )

    caught = _print_suite_only(_checked_files(anchor))

    assert PLAYWRIGHT_CONFIG.resolve() in caught
    assert any(path.is_relative_to(E2E_DIR.resolve()) for path in caught)
    assert any("node_modules" in path.parts for path in caught)


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


def test_the_port_check_sees_a_listening_server() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((HOST, 0))
        server.listen()
        port = int(server.getsockname()[1])
        assert _port_listening(port)
        assert not _wait_for_port_release(port, timeout_s=0.5)
    assert _wait_for_port_release(port)


def test_a_failing_npm_script_reports_its_exit_code() -> None:
    _require_web_app()
    code, tail = _run_npm_script("checklist-care-no-such-script")
    assert code != 0
    assert "checklist-care-no-such-script" in tail
