"""``checklist-care serve`` and the app shell (plan §5, §6) through the production callers.

The HTTP routes are exercised with ``TestClient`` on ``create_app``; the command runs
through ``cli.main`` with the two child-work seams replaced: ``run.run_server``
(uvicorn) and ``run.run_command`` (npm). No test starts a real server. The demo
sweep runs against real child processes, which are killed (whole tree) afterwards.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from checklist_care import __version__, cli, limits, paths, processes, proposal, server
from checklist_care.db import connection, seed
from checklist_care.errors import ConfigError
from checklist_care.server import app as app_module
from checklist_care.server import routes_meta, run
from checklist_care.server.app import create_app

ROOT = Path(__file__).resolve().parents[2]
FAKE_NPM = str(Path("C:/Program Files/nodejs/npm.cmd"))
WEB_CONCURRENCY = "WEB_CONCURRENCY"
"""uvicorn's worker-count environment variable, used when ``Config`` gets no ``workers``."""
LOCAL_URL = "http://127.0.0.1:8450"
"""The base URL of every test client: TestClient's default ``testserver`` Host is not
one the app accepts."""


def local_client(app: FastAPI) -> TestClient:
    """A ``TestClient`` whose requests carry ``Host: 127.0.0.1:8450``, like a browser
    on the daily server."""

    return TestClient(app, base_url=LOCAL_URL)


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real data directory or the real demo folder."""

    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A data directory created by ``checklist-care init``."""

    directory = tmp_path / "data"
    code, _, err = run_cli(capsys, "--data-dir", str(directory), "init")
    assert code == 0, err
    return directory


@pytest.fixture
def db_path(data_dir: Path) -> Path:
    return data_dir / paths.DB_FILENAME


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """An empty build directory: the app is not built."""

    directory = tmp_path / "dist"
    directory.mkdir()
    return directory


@pytest.fixture
def client(db_path: Path, dist: Path) -> Iterator[TestClient]:
    with local_client(create_app(db_path, dist_dir=dist)) as test_client:
        yield test_client


def write_build(dist: Path) -> None:
    (dist / "assets").mkdir()
    (dist / "index.html").write_text("<!doctype html><title>built app</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log('app');", encoding="utf-8")
    (dist / "assets" / "index-abc123.css").write_text("body{margin:0}", encoding="utf-8")


# --- /api/meta and /api/health ---------------------------------------------------------


def limits_constant_names() -> set[str]:
    """The public names ``limits.py`` assigns at module level, read from its source,
    so the expectation does not come from the code under test."""

    tree = ast.parse(Path(limits.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return {name for name in names if not name.startswith("_")}


def not_module_objects(values: dict[str, object]) -> list[str]:
    """The names whose value is not the ``limits.py`` object itself."""

    return sorted(name for name, value in values.items() if value is not getattr(limits, name))


def test_meta_returns_every_public_limits_name(client: TestClient) -> None:
    names = limits_constant_names()
    assert {"NAME_MAX", "LINE_SEPARATOR", "PRINT_CAPACITY", "AUDIENCES_TIP", "BUCKET_IDS"} <= names

    response = client.get("/api/meta")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"version", "limits", "buckets", "print_capacity"}
    assert set(payload["limits"]) == names
    for name in names:
        # JSON has no tuples: an enum arrives as a list with the same members.
        assert payload["limits"][name] == json.loads(json.dumps(getattr(limits, name))), name
    assert payload["limits"]["LINE_SEPARATOR"] == " — "
    assert payload["print_capacity"] == limits.PRINT_CAPACITY
    assert payload["version"] == __version__
    assert payload["buckets"] == [
        {"id": bucket.id, "title": bucket.title} for bucket in seed.BUCKETS
    ]


def test_meta_bucket_titles_are_the_stored_ones(client: TestClient, db_path: Path) -> None:
    with closing(connection.connect(db_path)) as conn:
        conn.execute("UPDATE bucket SET title = 'Every day' WHERE id = ?", (limits.BUCKET_DAILY,))

    titles = [bucket["title"] for bucket in client.get("/api/meta").json()["buckets"]]

    assert titles[0] == "Every day"
    assert titles[1:] == [bucket.title for bucket in seed.BUCKETS[1:]]


class MarkedInt(int):
    """An int equal to a ``limits.py`` value but never the same object as any other."""


class MarkedStr(str):
    """A str equal to a ``limits.py`` value but never the same object as any other."""


class MarkedTuple(tuple[object, ...]):
    """A tuple equal to a ``limits.py`` value but never the same object as any other."""


def marked(value: object) -> object:
    """A fresh object equal to ``value``, of a subclass of its type.

    CPython caches small ints (``48``, ``1``) and interns short strings, so ``x is
    limits.PRINT_CAPACITY`` also holds for a builder that hard-codes ``48``. A marker is
    a new object no code can restate, so ``is`` on it proves the builder read the
    module attribute.
    """

    if isinstance(value, int):
        return MarkedInt(value)
    if isinstance(value, str):
        return MarkedStr(value)
    assert isinstance(value, tuple), value
    return MarkedTuple(value)


@pytest.fixture
def marked_limits(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Every public ``limits.py`` constant replaced by its marker; returns the markers."""

    markers = {name: marked(getattr(limits, name)) for name in limits_constant_names()}
    for name, marker in markers.items():
        monkeypatch.setattr(limits, name, marker)
    return markers


def test_meta_builder_returns_the_limits_objects_themselves(
    db_path: Path, marked_limits: dict[str, object]
) -> None:
    with closing(connection.connect(db_path)) as conn:
        built = routes_meta.build_meta(conn)

    assert set(built["limits"]) == set(marked_limits) == limits_constant_names()
    for name, marker in marked_limits.items():
        assert built["limits"][name] is marker, name
    assert not_module_objects(built["limits"]) == []
    assert built["print_capacity"] is marked_limits["PRINT_CAPACITY"]


def test_the_identity_check_flags_restated_values(marked_limits: dict[str, object]) -> None:
    # Red anchor: with the markers in place, values a builder could restate (cached
    # small ints, interned strings, rebuilt tuples) equal the limits but fail `is`.
    restated: dict[str, object] = {
        "PRINT_CAPACITY": 48,
        "BUCKET_DAILY": 1,
        "LINE_SEPARATOR": " — ",
        "AUDIENCES_TIP": ("kids+", "adults"),
        "SQLITE_INTEGER_MAX": 2**63 - 1,
    }
    for name, value in restated.items():
        assert value == marked_limits[name], name
    assert not_module_objects(restated) == sorted(restated)
    assert not_module_objects(dict(marked_limits)) == []


def test_health_names_the_version_and_the_data_dir(client: TestClient, data_dir: Path) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": __version__, "data_dir": str(data_dir)}


def test_an_unknown_api_path_is_a_json_not_found(client: TestClient) -> None:
    for path in ("/api/nope", "/api", "/api/meta/extra"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {
            "error": {"code": "CC_E_NOT_FOUND", "message": f"no API route {path}", "fields": []}
        }


def test_the_lifespan_opens_and_closes_the_database(db_path: Path, dist: Path) -> None:
    app = create_app(db_path, dist_dir=dist)
    database = app.state.database
    assert not database.is_open
    with local_client(app) as test_client:
        assert database.is_open
        assert test_client.get("/api/meta").status_code == 200
    assert not database.is_open


def test_the_server_refuses_a_missing_database(tmp_path: Path, dist: Path) -> None:
    missing = tmp_path / "nowhere" / paths.DB_FILENAME
    with pytest.raises(ConfigError, match="run `checklist-care init` first"):
        with local_client(create_app(missing, dist_dir=dist)):
            pass
    assert not missing.exists()


# --- the Host allowlist (DNS rebinding) ----------------------------------------------------

FOREIGN_HOSTS = (
    "rebind.evil.example:8450",
    "rebind.evil.example",
    "127.0.0.1.evil.example:8450",
    "localhost.evil.example",
    "192.168.1.10:8450",
    "0.0.0.0:8450",
    "[::1]:8450",
    "testserver",
    "",
)


@pytest.mark.parametrize("host", FOREIGN_HOSTS)
def test_a_foreign_host_is_refused(host: str, db_path: Path, dist: Path) -> None:
    write_build(dist)
    with local_client(create_app(db_path, dist_dir=dist)) as test_client:
        responses = [
            test_client.get(path, headers={"host": host})
            for path in ("/api/health", "/api/meta", "/", "/assets/index-abc123.js")
        ]

    for response in responses:
        assert response.status_code == 400, response.request.url
        assert response.text == "Invalid host header"
        assert str(db_path.parent) not in response.text


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8450",
        "127.0.0.1:8451",
        "127.0.0.1:8452",
        "127.0.0.1:5450",  # the Vite dev proxy forwards the browser's Host unchanged
        "127.0.0.1",
        "localhost:8450",
        "localhost:5450",
        "LOCALHOST:8452",
        "localhost",
    ],
)
def test_loopback_hosts_on_any_port_are_served(host: str, db_path: Path, dist: Path) -> None:
    with local_client(create_app(db_path, dist_dir=dist)) as test_client:
        health_response = test_client.get("/api/health", headers={"host": host})
        page = test_client.get("/", headers={"host": host})

    assert health_response.status_code == 200, host
    assert health_response.json()["data_dir"] == str(db_path.parent)
    assert page.status_code == 503  # the build-required page: the request was served


def test_the_allowed_hosts_are_the_two_loopback_names() -> None:
    assert server.ALLOWED_HOSTS == (server.HOST, "localhost")


def test_the_host_check_runs_through_the_serve_command(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, int] = {}

    def probe(test_client: TestClient) -> None:
        for host in ("rebind.evil.example:8450", "localhost:8450", "127.0.0.1:8450"):
            seen[host] = test_client.get("/api/health", headers={"host": host}).status_code

    monkeypatch.setattr(run, "run_server", ServerRecorder(probe))

    code, _, err = run_cli(capsys, "--data-dir", str(data_dir), "serve")

    assert code == 0, err
    assert seen == {"rebind.evil.example:8450": 400, "localhost:8450": 200, "127.0.0.1:8450": 200}


VITE_PROXY = re.compile(
    r'^const proxy = \{\s*"/api":\s*\{\s*target:\s*apiTarget,\s*changeOrigin:\s*false\s*\}\s*\};$',
    re.MULTILINE,
)
"""The ``proxy`` object literal in ``vite.config.ts``, as a statement at the start of a
line, so a comment that mentions ``changeOrigin: false`` does not match."""


def test_vite_proxy_keeps_the_browser_host() -> None:
    # The dev proxy must forward the browser's Host (127.0.0.1:5450), which the
    # allowlist accepts; the test above serves that exact Host.
    config = (ROOT / "web" / "vite.config.ts").read_text(encoding="utf-8")

    assert VITE_PROXY.search(config) is not None


# --- the built app and the build-required page ---------------------------------------------


def test_without_a_build_root_is_the_build_required_page(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "npm --prefix web run build" in response.text


def test_the_default_build_directory_is_web_dist() -> None:
    assert server.WEB_DIR == ROOT / "web"
    assert server.DIST_DIR == ROOT / "web" / "dist"
    assert server.WEB_DIR.is_absolute()


def test_the_built_app_is_served(db_path: Path, dist: Path) -> None:
    write_build(dist)
    with local_client(create_app(db_path, dist_dir=dist)) as test_client:
        index = test_client.get("/")
        script = test_client.get("/assets/index-abc123.js")
        style = test_client.get("/assets/index-abc123.css")
        missing = test_client.get("/assets/missing.js")

    assert index.status_code == 200
    assert index.text == "<!doctype html><title>built app</title>"
    assert index.headers["content-type"] == "text/html; charset=utf-8"
    assert index.headers["cache-control"] == "no-cache"
    assert script.status_code == 200
    assert script.headers["content-type"] == "text/javascript; charset=utf-8"
    assert script.headers["x-content-type-options"] == "nosniff"
    assert style.headers["content-type"] == "text/css; charset=utf-8"
    assert missing.status_code == 404


TRAVERSALS = (
    "/%2e%2e/outside.txt",
    "/assets/%2e%2e/%2e%2e/outside.txt",
    "/assets/..%2f..%2foutside.txt",
    "/..%5coutside.txt",
    "/assets/%2e%2e/index.html",
    "/assets/%2e/index-abc123.js",
)


@pytest.mark.parametrize("path", TRAVERSALS)
def test_static_paths_with_dot_segments_are_not_found(
    path: str, tmp_path: Path, db_path: Path, dist: Path
) -> None:
    write_build(dist)
    (tmp_path / "outside.txt").write_text("outside the build", encoding="utf-8")
    with local_client(create_app(db_path, dist_dir=dist)) as test_client:
        response = test_client.get(path)

    assert response.status_code == 404
    assert "outside the build" not in response.text


def test_the_lexical_check_is_what_refuses_dot_segments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_path: Path, dist: Path
) -> None:
    # Red anchor: the request reaches the handler with its `..` segment intact. With
    # the lexical check switched off, a `..` that stays inside dist is served, and only
    # the resolved-path containment still refuses one that leaves it.
    write_build(dist)
    (tmp_path / "outside.txt").write_text("outside the build", encoding="utf-8")
    monkeypatch.setattr(app_module, "is_safe_relative_path", lambda _path: True)
    with local_client(create_app(db_path, dist_dir=dist)) as test_client:
        inside = test_client.get("/assets/%2e%2e/index.html")
        outside = test_client.get("/%2e%2e/outside.txt")

    assert inside.status_code == 200
    assert outside.status_code == 404


@pytest.mark.parametrize(
    ("path", "safe"),
    [
        ("index.html", True),
        ("assets/index-abc123.js", True),
        ("assets\\index-abc123.js", True),
        ("", False),
        ("..", False),
        ("../outside.txt", False),
        ("assets/../index.html", False),
        ("assets\\..\\index.html", False),
        ("./index.html", False),
        ("assets//index.html", False),
        ("assets/", False),
        ("/index.html", False),
        ("\\index.html", False),
        ("C:/Windows/win.ini", False),
        ("C:index.html", False),
        ("index.html:stream", False),
        ("index.html\x00.js", False),
    ],
)
def test_is_safe_relative_path(path: str, safe: bool) -> None:
    assert app_module.is_safe_relative_path(path) is safe


# --- serve through cli.main --------------------------------------------------------------


class ServerRecorder:
    """A ``run.run_server`` stand-in: records the config, optionally probes the app."""

    def __init__(self, probe: Callable[[TestClient], None] | None = None) -> None:
        self.configs: list[uvicorn.Config] = []
        self.probe = probe

    def __call__(self, config: uvicorn.Config) -> None:
        self.configs.append(config)
        if self.probe is not None:
            assert isinstance(config.app, FastAPI)
            with local_client(config.app) as test_client:
                self.probe(test_client)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> ServerRecorder:
    fake = ServerRecorder()
    monkeypatch.setattr(run, "run_server", fake)
    return fake


def test_serve_binds_loopback_with_one_worker(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    fake = ServerRecorder(lambda test_client: seen.append(health(test_client)))
    monkeypatch.setattr(run, "run_server", fake)
    # uvicorn takes its worker count from WEB_CONCURRENCY when none is passed, so this
    # fails if the explicit `workers=` pin is ever dropped.
    monkeypatch.setenv(WEB_CONCURRENCY, "4")

    code, out, err = run_cli(capsys, "--data-dir", str(data_dir), "serve")

    assert code == 0, err
    assert out == ""
    assert err[0] == f"data_dir: {data_dir}"
    assert "serving http://127.0.0.1:8450/ (Ctrl+C stops it)" in err
    config = fake.configs[0]
    assert config.host == "127.0.0.1"
    assert config.workers == 1
    assert config.port == 8450
    assert seen == [{"ok": True, "version": __version__, "data_dir": str(data_dir)}]


def health(test_client: TestClient) -> dict[str, Any]:
    response = test_client.get("/api/health")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def test_serve_takes_the_port_and_never_a_host(
    capsys: pytest.CaptureFixture[str], data_dir: Path, recorder: ServerRecorder
) -> None:
    code, _, err = run_cli(capsys, "--data-dir", str(data_dir), "serve", "--port", "8451")
    assert code == 0, err
    assert (recorder.configs[0].host, recorder.configs[0].port) == ("127.0.0.1", 8451)

    for bad in (["--host", "0.0.0.0"], ["--port", "0"], ["--port", "65536"], ["--port", "x"]):
        code, _, _ = run_cli(capsys, "--data-dir", str(data_dir), "serve", *bad)
        assert code == 2, bad
    assert len(recorder.configs) == 1

    code, out, _ = run_cli(capsys, "serve", "--help")
    assert code == 0 and "--host" not in out and "127.0.0.1" in out


def test_web_concurrency_would_change_an_unpinned_worker_count(
    db_path: Path, dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Red anchor: without an explicit `workers=`, uvicorn reads WEB_CONCURRENCY, so the
    # tests that set it would see 4 workers if run.py stopped pinning one.
    monkeypatch.setenv(WEB_CONCURRENCY, "4")
    unpinned = uvicorn.Config(create_app(db_path, dist_dir=dist), host=server.HOST, port=8452)

    assert unpinned.workers == 4


def test_serve_without_a_database_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, recorder: ServerRecorder
) -> None:
    data = tmp_path / "empty"
    code, _, err = run_cli(capsys, "--data-dir", str(data), "serve")

    assert code == 2
    assert err[-1].startswith("CC_E_CONFIG: no database at ")
    assert err[-1].endswith("run `checklist-care init` first")
    assert recorder.configs == []
    assert not (data / paths.DB_FILENAME).exists()


# --- serve --build ------------------------------------------------------------------------


class CommandRecorder:
    """A ``run.run_command`` stand-in: records each command and returns ``code``."""

    def __init__(self, events: list[str], code: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.events = events
        self.code = code

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(list(command))
        self.events.append("npm")
        return self.code


def fake_find_npm(found: str | None) -> Callable[..., str | None]:
    def find_npm(search_path: str | None = None) -> str | None:
        return found

    return find_npm


@pytest.mark.parametrize("start", ["web", "elsewhere"])
def test_serve_build_passes_the_absolute_web_dir_from_any_cwd(
    start: str,
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    npm = CommandRecorder(events)
    monkeypatch.setattr(run, "run_command", npm)
    monkeypatch.setattr(run, "find_npm", fake_find_npm(FAKE_NPM))
    monkeypatch.setattr(run, "run_server", lambda config: events.append("server"))
    # From web/ the working directory equals WEB_DIR; from a third folder it does not,
    # so a build that used the working directory fails here.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(ROOT / "web" if start == "web" else elsewhere)

    code, _, err = run_cli(capsys, "--data-dir", str(data_dir), "serve", "--build")

    assert code == 0, err
    web_dir = str(ROOT / "web")
    assert run.WEB_DIR == ROOT / "web" and run.WEB_DIR.is_absolute()
    install = not (ROOT / "web" / "node_modules").is_dir()
    assert npm.commands == run.npm_commands(FAKE_NPM, install=install)
    assert npm.commands[-1] == [FAKE_NPM, "--prefix", web_dir, "run", "build"]
    for command in npm.commands:
        assert command[:3] == [FAKE_NPM, "--prefix", web_dir]
    assert events == ["npm"] * len(npm.commands) + ["server"]


CHILD_TIMEOUT_S = 120.0
SERVE_BUILD_IN_CHILD = """\
import json, sys

from checklist_care import cli, server
from checklist_care.server import run

data_dir, npm = sys.argv[1:]
commands = []

def record(command):
    commands.append(list(command))
    return 0

run.run_command = record
run.find_npm = lambda search_path=None: npm
run.run_server = lambda config: None
code = cli.main(["--data-dir", data_dir, "serve", "--build"])
print(json.dumps({
    "code": code,
    "server_file": server.__file__,
    "web_dir": str(server.WEB_DIR),
    "commands": commands,
}))
"""
"""``serve --build`` through ``cli.main`` in a fresh interpreter, with the npm runner and
uvicorn replaced; prints the exit code, ``WEB_DIR`` and the npm command lines as JSON."""


def test_serve_build_started_in_web_passes_the_absolute_web_dir(data_dir: Path) -> None:
    # This process imported checklist_care.server with the checkout root as its working
    # directory, where a cwd-relative WEB_DIR is also right. The child starts in web/,
    # so it imports the server there, as `checklist-care serve --build` run from web/ does.
    web_dir = ROOT / "web"
    child = subprocess.run(
        [sys.executable, "-c", SERVE_BUILD_IN_CHILD, str(data_dir), FAKE_NPM],
        cwd=web_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        creationflags=processes.NO_WINDOW,
        timeout=CHILD_TIMEOUT_S,
    )

    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout.splitlines()[-1])
    assert Path(result["server_file"]) == Path(server.__file__)  # the same code
    assert result["code"] == 0, child.stderr
    assert result["web_dir"] == str(web_dir)
    assert result["commands"], child.stderr
    assert result["commands"][-1] == [FAKE_NPM, "--prefix", str(web_dir), "run", "build"]
    for command in result["commands"]:
        assert command[:3] == [FAKE_NPM, "--prefix", str(web_dir)]


def test_build_installs_only_when_node_modules_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm = CommandRecorder([])
    monkeypatch.setattr(run, "run_command", npm)
    monkeypatch.setattr(run, "find_npm", fake_find_npm(FAKE_NPM))
    web = tmp_path / "web"
    web.mkdir()

    run.build_web(web)
    (web / "node_modules").mkdir()
    run.build_web(web)

    prefix = [FAKE_NPM, "--prefix", str(web)]
    assert npm.commands == [
        [*prefix, "install", "--prefer-offline", "--no-audit", "--no-fund"],
        [*prefix, "run", "build"],
        [*prefix, "run", "build"],
    ]


def test_serve_build_without_npm_exits_2(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: ServerRecorder,
) -> None:
    npm = CommandRecorder([])
    monkeypatch.setattr(run, "run_command", npm)
    monkeypatch.setattr(run, "find_npm", fake_find_npm(None))

    code, _, err = run_cli(capsys, "--data-dir", str(data_dir), "serve", "--build")

    assert code == 2
    assert err[-1].startswith("CC_E_CONFIG: npm is not on PATH")
    assert npm.commands == []
    assert recorder.configs == []


def npm_program(directory: Path) -> Path:
    """A stand-in npm in ``directory``: ``npm.cmd`` on Windows, an executable ``npm``
    elsewhere. It is never run."""

    directory.mkdir(parents=True, exist_ok=True)
    program = directory / ("npm.cmd" if sys.platform == "win32" else "npm")
    program.write_text("exit 9\n", encoding="utf-8")
    program.chmod(0o755)
    return program


@pytest.fixture
def planted_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working directory, holding a planted npm (and one in a relative ``bin``);
    the Windows lookup settings are the defaults, so a bare ``which`` searches it."""

    planted = tmp_path / "planted"
    npm_program(planted)
    npm_program(planted / "bin")
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    return planted


def test_find_npm_searches_only_absolute_path_entries(
    planted_cwd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = npm_program(tmp_path / "nodejs")
    empty = tmp_path / "empty"
    empty.mkdir()
    entries = [".", "bin", "", str(empty), f'"{real.parent}"']
    monkeypatch.setenv("PATH", os.pathsep.join(entries))

    found = run.find_npm()

    assert found is not None and os.path.isabs(found)
    assert Path(found).resolve() == real.resolve()
    assert run.find_npm(os.pathsep.join([".", "bin", str(empty)])) is None
    assert run.find_npm("") is None


def test_a_bare_which_would_find_the_planted_npm(
    planted_cwd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Red anchor: the threat find_npm avoids. On Windows `which` searches the working
    # directory first; anywhere, a relative PATH entry resolves against it.
    planted = planted_cwd.resolve()
    if sys.platform == "win32":
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        bare = shutil.which("npm")
        assert bare is not None and Path(bare).resolve().parent == planted
        monkeypatch.setenv("NoDefaultCurrentDirectoryInExePath", "1")
    monkeypatch.setenv("PATH", "bin")
    relative = shutil.which("npm")
    assert relative is not None and Path(relative).resolve().parent == planted / "bin"


def test_serve_build_never_runs_an_npm_from_the_working_directory(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    planted_cwd: Path,
    tmp_path: Path,
    recorder: ServerRecorder,
) -> None:
    npm = CommandRecorder([])
    monkeypatch.setattr(run, "run_command", npm)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "bin", str(tmp_path / "empty")]))

    code, _, err = run_cli(capsys, "--data-dir", str(data_dir), "serve", "--build")

    assert code == 2
    assert err[-1].startswith("CC_E_CONFIG: npm is not on PATH")
    assert npm.commands == []
    assert recorder.configs == []


def test_a_failed_build_exits_2_and_never_serves(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: ServerRecorder,
) -> None:
    npm = CommandRecorder([], code=1)
    monkeypatch.setattr(run, "run_command", npm)
    monkeypatch.setattr(run, "find_npm", fake_find_npm(FAKE_NPM))

    code, _, err = run_cli(capsys, "--data-dir", str(data_dir), "serve", "--build")

    assert code == 2
    assert err[-1].startswith("CC_E_CONFIG: ")
    assert "exited 1; see its output above" in err[-1]
    assert len(npm.commands) == 1
    assert recorder.configs == []


def test_run_command_copies_utf8_output_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    code = (
        "import sys; sys.stdout.buffer.write('café \\U0001f600\\n'.encode('utf-8')); "
        "sys.stdout.flush(); sys.stderr.write('to stderr\\n'); sys.exit(3)"
    )

    result = run.run_command([sys.executable, "-c", code])

    captured = capsys.readouterr()
    assert result == 3
    assert captured.out == ""
    assert "café \U0001f600" in captured.err
    assert "to stderr" in captured.err


def test_run_command_reports_a_missing_program() -> None:
    with pytest.raises(ConfigError, match="cannot start checklist-care-no-such-npm-xyz"):
        run.run_command(["checklist-care-no-such-npm-xyz", "run", "build"])


# --- uvicorn wrapper ----------------------------------------------------------------------


class FakeServer:
    """A ``uvicorn.Server`` stand-in for :func:`run.run_server`."""

    outcome: BaseException | None = None
    started_after_run = True

    def __init__(self, config: uvicorn.Config) -> None:
        self.config = config
        self.started = False

    def run(self) -> None:
        self.started = self.started_after_run
        if self.outcome is not None:
            raise self.outcome


def test_run_server_maps_a_failed_start_to_a_config_error(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, dist: Path
) -> None:
    config = run.server_config(create_app(db_path, dist_dir=dist), 8452)
    monkeypatch.setattr(run.uvicorn, "Server", FakeServer)

    run.run_server(config)  # started, then stopped normally

    monkeypatch.setattr(FakeServer, "outcome", KeyboardInterrupt())
    run.run_server(config)  # Ctrl+C is a normal stop

    monkeypatch.setattr(FakeServer, "outcome", SystemExit(1))
    with pytest.raises(ConfigError, match="is another program using port 8452"):
        run.run_server(config)

    monkeypatch.setattr(FakeServer, "outcome", None)
    monkeypatch.setattr(FakeServer, "started_after_run", False)
    with pytest.raises(ConfigError, match="the server did not start"):
        run.run_server(config)


# --- serve --demo -------------------------------------------------------------------------


@pytest.fixture
def live_child() -> Iterator[subprocess.Popen[bytes]]:
    """A running child process; its whole tree is killed in teardown."""

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=processes.NO_WINDOW,
        start_new_session=processes.NEW_SESSION,
    )
    try:
        yield child
    finally:
        processes.kill_tree(child)


@pytest.fixture
def dead_pid() -> Iterator[int]:
    """The pid of a child that has exited. The Popen object keeps its handle open, so
    Windows cannot give the pid to a new process while the test runs."""

    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=processes.NO_WINDOW,
    )
    child.wait(timeout=60)
    yield child.pid
    del child


def make_demo_dir(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / paths.DB_FILENAME).write_bytes(b"demo")
    return directory


def assert_still_running(child: subprocess.Popen[bytes]) -> None:
    """``child`` has not exited: waiting for it times out. Deterministic where a
    ``poll()`` right after a kill could still see the dying process as running."""

    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(timeout=0.5)


def test_pid_alive(live_child: subprocess.Popen[bytes], dead_pid: int) -> None:
    assert run.pid_alive(os.getpid())
    assert run.pid_alive(live_child.pid)
    assert not run.pid_alive(dead_pid)
    assert not run.pid_alive(0)
    assert not run.pid_alive(-1)
    assert_still_running(live_child)


def test_pid_alive_refuses_numbers_that_cannot_be_pids(
    live_child: subprocess.Popen[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    # ctypes would truncate 2**32 + pid to pid (a DWORD), so the live child must not
    # make the larger number look alive; POSIX os.kill would raise OverflowError.
    assert run.PID_MAX in (2**32 - 1, 2**31 - 1)
    for number in (run.PID_MAX + 1, 2**32 + live_child.pid, 2**64, 10**30):
        assert run.pid_alive(number) is False, number

    def refuse(pid: int) -> bool:
        raise OverflowError("signed integer is greater than maximum")

    monkeypatch.setattr(run, "_os_pid_alive", refuse)
    assert run.pid_alive(live_child.pid) is False
    assert run.pid_alive(1) is False
    assert_still_running(live_child)


def test_the_demo_sweep_keeps_directories_whose_number_cannot_be_a_pid(
    tmp_path: Path, dead_pid: int
) -> None:
    root = tmp_path / "demo-root"
    dead = make_demo_dir(root, f"{dead_pid}-0123abcd")
    names = [
        f"{2**32 + dead_pid}-0123abcd",  # truncates to the dead pid in a DWORD
        f"{run.PID_MAX + 1}-0123abcd",
        f"{2**64}-0123abcd",
        f"{'9' * 200}-0123abcd",
        "0-0123abcd",
    ]
    kept = [make_demo_dir(root, name) for name in names]
    assert all(run.DEMO_ENTRY_PATTERN.fullmatch(name) for name in names)

    removed = run.sweep_stale_demo_dirs(root)

    assert removed == [dead]
    assert all(directory.is_dir() for directory in kept)
    assert [run.demo_dir_pid(name) for name in names] == [None] * len(names)
    assert run.demo_dir_pid(f"{dead_pid}-0123abcd") == dead_pid
    assert run.demo_dir_pid(f"{run.PID_MAX}-0123abcd") == run.PID_MAX
    assert run.demo_dir_pid("notes") is None


def test_the_demo_sweep_removes_dead_pids_and_keeps_live_ones(
    tmp_path: Path, live_child: subprocess.Popen[bytes], dead_pid: int
) -> None:
    root = tmp_path / "demo-root"
    dead = make_demo_dir(root, f"{dead_pid}-0123abcd")
    alive = make_demo_dir(root, f"{live_child.pid}-89abcdef")
    own = make_demo_dir(root, f"{os.getpid()}-00ff00ff")
    unrelated = [
        make_demo_dir(root, "notes"),
        make_demo_dir(root, f"{dead_pid}-XYZ"),
        make_demo_dir(root, f"{dead_pid}-0123abcd-extra"),
    ]
    stray_file = root / f"{dead_pid}-fedcba98"
    stray_file.write_text("not a directory", encoding="utf-8")

    removed = run.sweep_stale_demo_dirs(root)

    assert removed == [dead]
    assert not dead.exists()
    assert alive.is_dir() and (alive / paths.DB_FILENAME).is_file()
    assert own.is_dir()
    assert all(directory.is_dir() for directory in unrelated)
    assert stray_file.is_file()
    # The liveness check must not have touched the live process.
    assert_still_running(live_child)


def test_the_demo_sweep_of_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert run.sweep_stale_demo_dirs(tmp_path / "absent") == []


def test_create_demo_dir_sweeps_then_creates_its_own(tmp_path: Path, dead_pid: int) -> None:
    root = tmp_path / "demo-root"
    stale = make_demo_dir(root, f"{dead_pid}-0123abcd")

    created = run.create_demo_dir(root)

    assert not stale.exists()
    assert created.parent == root and created.is_dir()
    assert re.fullmatch(rf"{os.getpid()}-[0-9a-f]{{8}}", created.name)
    assert run.DEMO_ENTRY_PATTERN.fullmatch(created.name)
    assert run.demo_root() == Path(tempfile.gettempdir()) / "checklist-care-demo"


def test_serve_demo_seeds_a_temp_dir_sweeps_stale_ones_and_cleans_up(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dead_pid: int,
    live_child: subprocess.Popen[bytes],
) -> None:
    root = run.demo_root()
    assert root == tmp_path / "temp" / "checklist-care-demo"
    stale = make_demo_dir(root, f"{dead_pid}-0123abcd")
    busy = make_demo_dir(root, f"{live_child.pid}-89abcdef")
    seen: dict[str, Any] = {}

    def probe(test_client: TestClient) -> None:
        seen["health"] = health(test_client)
        seen["meta"] = test_client.get("/api/meta").json()
        seen["dirs"] = sorted(path.name for path in root.iterdir())

    fake = ServerRecorder(probe)
    monkeypatch.setattr(run, "run_server", fake)
    monkeypatch.setenv(WEB_CONCURRENCY, "4")
    # The real data directory has no database: --demo does not need one.
    code, _, err = run_cli(capsys, "serve", "--demo", "--port", "8451")

    assert code == 0, err
    assert err[0].startswith("data_dir: ")
    demo_line = next(line for line in err if line.startswith("demo data_dir: "))
    demo_dir = Path(demo_line.removeprefix("demo data_dir: "))
    assert demo_dir.parent == root
    assert re.fullmatch(rf"{os.getpid()}-[0-9a-f]{{8}}", demo_dir.name)
    assert seen["health"]["data_dir"] == str(demo_dir)
    assert seen["meta"]["buckets"] == [
        {"id": bucket.id, "title": bucket.title} for bucket in seed.BUCKETS
    ]
    assert seen["dirs"] == sorted([busy.name, demo_dir.name])
    config = fake.configs[0]
    assert (config.host, config.port, config.workers) == ("127.0.0.1", 8451, 1)
    # After the server stops: the stale directory was swept, the live one kept, and
    # this server's own directory removed.
    assert not stale.exists()
    assert busy.is_dir()
    assert not demo_dir.exists()
    assert_still_running(live_child)


def test_the_demo_database_is_the_seeded_schema(tmp_path: Path) -> None:
    db = run.demo.populate(tmp_path / "demo")
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == connection.SCHEMA_VERSION
        lists = conn.execute("SELECT count(*) FROM list").fetchone()[0]
        buckets = conn.execute("SELECT count(*) FROM bucket").fetchone()[0]
    assert (lists, buckets) == (len(seed.LISTS) + len(run.demo.DEMO_LISTS), len(seed.BUCKETS))
    for name in paths.SUBDIRECTORIES:
        assert (tmp_path / "demo" / name).is_dir()


# --- shared constants ---------------------------------------------------------------------


def test_the_review_url_is_built_from_the_server_address() -> None:
    assert proposal.REVIEW_URL == f"{server.base_url(server.DAILY_PORT)}/#/review"
    assert proposal.REVIEW_URL == "http://127.0.0.1:8450/#/review"


def test_meta_ts_names_only_real_limits_and_payload_keys(db_path: Path) -> None:
    source = (ROOT / "web" / "src" / "meta.ts").read_text(encoding="utf-8")

    def interface_keys(name: str) -> set[str]:
        match = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.DOTALL)
        assert match is not None, name
        return set(re.findall(r"^\s+(\w+):", match.group(1), re.MULTILINE))

    with closing(connection.connect(db_path)) as conn:
        built = routes_meta.build_meta(conn)
    limit_keys = interface_keys("MetaLimits")
    assert {"NAME_MAX", "LINE_MAX", "PRINT_CAPACITY"} <= limit_keys
    assert limit_keys <= limits_constant_names()
    assert interface_keys("Meta") == set(built)


def test_vite_config_uses_the_server_address() -> None:
    config = (ROOT / "web" / "vite.config.ts").read_text(encoding="utf-8")

    assert f'const loopback = "{server.HOST}";' in config
    assert "const devPort = 5450;" in config
    assert f"const apiTarget = `http://${{loopback}}:{server.DAILY_PORT}`;" in config
    assert config.count("strictPort: true") == 2
