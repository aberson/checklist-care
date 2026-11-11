"""``checklist-care fetch '<wikipedia-url>'`` through the production entry point ``cli.main``.

The seam is ``wiki.urlopen``. It is replaced by ``OpenerDirector.open`` of a real urllib
opener whose HTTPS handler answers from :class:`FakeWikipedia` instead of the network.
So the request ``wiki.py`` builds (URL, query parameters, headers) goes through urllib's
own request processing, where the default ``Python-urllib/3.x`` user agent would be
added, and an HTTP 429 or 503 comes back as urllib's own ``HTTPError``.
``wiki.sleep`` records the retry waits instead of sleeping.

The article text is ``tests/fixtures/packet_wiki_synthetic.json`` (self-written), served
with a reference tail that ``fetch`` must cut: the packet ``fetch`` writes must equal the
fixture byte for byte.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import socket
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)
from urllib.response import addinfourl

import pytest

from checklist_care import USER_AGENT, cli, paths
from checklist_care.db import connection
from checklist_care.sources import packet, wiki, youtube

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PACKET_FIXTURE = FIXTURES / "packet_wiki_synthetic.json"

BOOK = "The Happiness Hypothesis"
REF = "wiki:en:The_Happiness_Hypothesis"
CANONICAL = "https://en.wikipedia.org/wiki/The_Happiness_Hypothesis"
DESKTOP = CANONICAL
MOBILE = "https://en.m.wikipedia.org/wiki/The_Happiness_Hypothesis"
REDIRECT = "https://en.wikipedia.org/wiki/Happiness_Hypothesis"
API = "https://en.wikipedia.org/w/api.php"
LASTREVID = 1300000001
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-08T12:00:00Z"

FIXED_PARAMS = {
    "action": "query",
    "format": "json",
    "formatversion": "2",
    "redirects": "1",
    "prop": "extracts|info|pageprops",
    "explaintext": "1",
    "inprop": "url",
    "ppprop": "disambiguation",
}
"""The verified request of plan §4.3, apart from ``titles``."""

# Self-written lines in the plain-text shape of a reference tail.
TAIL = (
    "\n\n\n== See also ==\nA synthetic related article\n"
    "\n\n\n== References ==\n"
    "\n\n\n== Further reading ==\nA synthetic book list line\n"
    "\n\n\n== External links ==\nA synthetic link line\n"
)

DISAMBIGUATION = "Happiness Hypothesis (disambiguation)"


def fixture_packet() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(PACKET_FIXTURE.read_bytes().decode("utf-8"))
    return payload


def book_page() -> dict[str, Any]:
    """The formatversion=2 page object of the book article (``inprop=url`` fields too)."""

    return {
        "pageid": 1001,
        "ns": 0,
        "title": BOOK,
        "contentmodel": "wikitext",
        "pagelanguage": "en",
        "pagelanguagehtmlcode": "en",
        "pagelanguagedir": "ltr",
        "touched": "2026-11-01T00:00:00Z",
        "lastrevid": LASTREVID,
        "length": 24000,
        "fullurl": CANONICAL,
        "editurl": f"{CANONICAL.replace('/wiki/', '/w/index.php?title=')}&action=edit",
        "canonicalurl": CANONICAL,
        "extract": fixture_packet()["text"] + TAIL,
    }


def other_pages() -> dict[str, dict[str, Any]]:
    """Pages ``fetch`` must refuse, by the title the API resolves to."""

    return {
        DISAMBIGUATION: {
            "pageid": 1002,
            "ns": 0,
            "title": DISAMBIGUATION,
            "lastrevid": 7,
            "canonicalurl": "https://en.wikipedia.org/wiki/Happiness_Hypothesis_(disambiguation)",
            "extract": "Happiness Hypothesis may refer to:\n\nA synthetic book\nA synthetic film",
            "pageprops": {"disambiguation": ""},
        },
        "Special:Random": {"ns": -1, "title": "Special:Random", "special": True},
        "Talk:": {
            "title": "Talk:",
            "invalidreason": "The requested page title is empty or contains only the name of "
            "a namespace.",
            "invalid": True,
        },
    }


@dataclass
class Reply:
    """One canned HTTP answer, served before the site's normal ones."""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Sent:
    url: str
    headers: dict[str, str]
    timeout: float | None


@dataclass
class FakeWikipedia:
    """Wikipedia's Action API for the book article, behind urllib."""

    pages: dict[str, dict[str, Any]] = field(default_factory=other_pages)
    redirects: dict[str, str] = field(
        default_factory=lambda: {"Happiness Hypothesis": BOOK, "Happiness hypothesis": BOOK}
    )
    use_gzip: bool = True
    replies: list[Reply | BaseException] = field(default_factory=list)
    sent: list[Sent] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    on_request: Callable[[], None] | None = None
    edit: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        self.pages.setdefault(BOOK, book_page())

    def document(self, url: str) -> dict[str, Any]:
        """The API answer, after checking that the request is the verified one."""

        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == API
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        titles = [value for key, value in pairs if key == "titles"]
        assert len(titles) == 1 and "|" not in titles[0]  # one title per request
        assert {key: value for key, value in pairs if key != "titles"} == FIXED_PARAMS
        assert len(pairs) == len(FIXED_PARAMS) + 1

        title = titles[0]
        query: dict[str, Any] = {}
        resolved = title.replace("_", " ")
        if resolved != title:
            query["normalized"] = [{"fromencoded": False, "from": title, "to": resolved}]
        if resolved in self.redirects:
            query["redirects"] = [{"from": resolved, "to": self.redirects[resolved]}]
            resolved = self.redirects[resolved]
        page = self.pages.get(resolved, {"ns": 0, "title": resolved, "missing": True})
        query["pages"] = [json.loads(json.dumps(page))]
        document: dict[str, Any] = {"batchcomplete": True, "query": query}
        if self.edit is not None:
            self.edit(document)
        return document

    def answer(self, request: Request) -> addinfourl:
        self.sent.append(Sent(request.full_url, dict(request.header_items()), request.timeout))
        if self.on_request is not None:
            self.on_request()
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, BaseException):
            raise reply
        if reply is None:
            # Raw UTF-8, as formatversion=2 sends it. A lone surrogate (only a test puts
            # one in) becomes the text \ud800, which is that JSON escape.
            text = json.dumps(self.document(request.full_url), ensure_ascii=False)
            body = text.encode("utf-8", "backslashreplace")
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if self.use_gzip:
                body = gzip.compress(body)
                headers["Content-Encoding"] = "gzip"
            reply = Reply(200, body, headers)
        message = Message()
        for name, value in reply.headers.items():
            message[name] = value
        response = addinfourl(io.BytesIO(reply.body), message, request.full_url, reply.status)
        response.msg = "OK" if reply.status == 200 else "Error"
        return response


class FakeTransport(HTTPSHandler):
    """urllib's HTTPS handler, answering from the fake site instead of a socket."""

    def __init__(self, site: FakeWikipedia) -> None:
        super().__init__()
        self.site = site

    def https_open(self, req: Request) -> addinfourl:
        return self.site.answer(req)


class NoPlainHTTP(HTTPHandler):
    def http_open(self, req: Request) -> addinfourl:
        raise AssertionError(f"plain HTTP request to {req.full_url}")


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(connection, "utc_now", lambda: NOW)

    def no_youtube(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a Wikipedia fetch must not touch the YouTube seams")

    monkeypatch.setattr(youtube, "make_transcript_client", no_youtube)
    monkeypatch.setattr(youtube, "run_ytdlp", no_youtube)


@pytest.fixture(autouse=True)
def site(monkeypatch: pytest.MonkeyPatch) -> FakeWikipedia:
    """Every test talks to the fake site; none reaches the network."""

    fake = FakeWikipedia()
    opener: OpenerDirector = build_opener(ProxyHandler({}), FakeTransport(fake), NoPlainHTTP())
    monkeypatch.setattr(wiki, "urlopen", opener.open)
    monkeypatch.setattr(wiki, "sleep", fake.sleeps.append)
    return fake


@pytest.fixture
def data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    target = tmp_path / "data"
    code, _, err = run(capsys, "--data-dir", str(target), "init")
    assert code == 0, err
    return target


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, list[str]]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err.splitlines()


def fetch(
    capsys: pytest.CaptureFixture[str], data_dir: Path, url: str
) -> tuple[int, str, list[str]]:
    return run(capsys, "--data-dir", str(data_dir), "fetch", url)


def packet_file(data_dir: Path) -> Path:
    return data_dir / "packets" / f"{paths.source_key(REF)}.json"


def packet_names(data_dir: Path) -> list[str]:
    return sorted(p.name for p in (data_dir / "packets").iterdir())


def source_rows(data_dir: Path) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM source WHERE kind <> 'manual' ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def mark_reviewed(data_dir: Path, ref: str, when: str = "2026-11-08T13:00:00Z") -> None:
    with closing(sqlite3.connect(data_dir / "checklist-care.db")) as conn:
        conn.execute("UPDATE source SET reviewed_at = ? WHERE ref = ?", (when, ref))
        conn.commit()


def sent_titles(site: FakeWikipedia) -> list[str]:
    return [dict(parse_qsl(urlsplit(sent.url).query))["titles"] for sent in site.sent]


def assert_refused(
    code: int, out: str, err: list[str], data_dir: Path, *, starts: str, exit_code: int = 4
) -> None:
    """``exit_code`` with one error line starting ``starts``, and nothing written."""

    assert code == exit_code, err
    assert out == ""
    assert len(err) == 2, err
    assert err[1].startswith(starts), err[1]
    assert packet_names(data_dir) == []
    assert source_rows(data_dir) == []


# --- a valid article -------------------------------------------------------------------


def test_fetch_writes_the_wiki_packet_and_its_source_row(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert code == 0, err
    target = packet_file(data_dir)
    raw = target.read_bytes()
    expected = fixture_packet()
    assert out.splitlines() == [
        f"packet: {target}",
        f"source_ref: {REF}",
        f"packet_sha256: {hashlib.sha256(raw).hexdigest()}",
        f"counts: {len(expected['text'])}/12",
    ]
    assert err == [f"data_dir: {data_dir}"]
    # The tail is cut and the sections found exactly as the fixture records them.
    assert raw == PACKET_FIXTURE.read_bytes()
    payload = json.loads(raw)
    assert payload["source_ref"] == REF and payload["kind"] == "wiki"
    assert [s["heading"] for s in payload["sections"]][-1] == "Reception"
    assert sum(s["heading"].startswith("Ch.") for s in payload["sections"]) == 11
    assert "See also" not in payload["text"] and "External links" not in payload["text"]

    assert source_rows(data_dir) == [
        {
            "id": 2,
            "kind": "wiki",
            "ref": REF,
            "title": BOOK,
            "url": CANONICAL,
            "revision": str(LASTREVID),
            "mode": "extract",
            "fetched_at": NOW_TEXT,
            "reviewed_at": None,
        }
    ]
    assert [sent.url for sent in site.sent] == [
        f"{API}?action=query&format=json&formatversion=2&redirects=1"
        "&titles=The%20Happiness%20Hypothesis&prop=extracts|info|pageprops&explaintext=1"
        "&inprop=url&ppprop=disambiguation"
    ]
    assert site.sent[0].timeout == wiki.HTTP_TIMEOUT_S
    assert site.sleeps == []


def test_the_request_carries_the_project_user_agent_not_python_urllib(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    code, _, err = fetch(capsys, data_dir, DESKTOP)

    assert code == 0, err
    headers = site.sent[0].headers  # after urllib added its defaults
    assert headers["User-agent"] == USER_AGENT
    assert USER_AGENT.startswith("checklist-care/")
    assert not any("Python-urllib" in value for value in headers.values())
    assert headers["Accept-encoding"] == "gzip"
    assert headers["Host"] == "en.wikipedia.org"


def test_an_uncompressed_answer_gives_the_same_packet(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    site.use_gzip = False

    code, _, err = fetch(capsys, data_dir, DESKTOP)

    assert code == 0, err
    assert packet_file(data_dir).read_bytes() == PACKET_FIXTURE.read_bytes()


# --- every URL form is one source ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "asked"),
    [
        (DESKTOP, BOOK),
        (MOBILE, BOOK),
        (REDIRECT, "Happiness Hypothesis"),
        ("https://en.m.wikipedia.org/wiki/Happiness_hypothesis#Reception", "Happiness hypothesis"),
        (f"{DESKTOP}#Reception", BOOK),
        ("https://en.wikipedia.org/w/index.php?title=The_Happiness_Hypothesis&action=info", BOOK),
        ("en.m.wikipedia.org/wiki/The%20Happiness%20Hypothesis", BOOK),
    ],
)
def test_every_url_form_gives_the_canonical_ref(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    url: str,
    asked: str,
) -> None:
    code, out, err = fetch(capsys, data_dir, url)

    assert code == 0, err
    assert out.splitlines()[:2] == [f"packet: {packet_file(data_dir)}", f"source_ref: {REF}"]
    assert sent_titles(site) == [asked]  # one request, always to the desktop API
    assert packet_file(data_dir).read_bytes() == PACKET_FIXTURE.read_bytes()
    assert source_rows(data_dir)[0]["url"] == CANONICAL


def test_a_mobile_url_and_a_redirect_title_share_one_source_and_packet(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = []
    for index, url in enumerate([DESKTOP, MOBILE, REDIRECT]):
        later = datetime(2026, 11, 9, 8, index, 0, tzinfo=UTC)
        monkeypatch.setattr(connection, "utc_now", lambda later=later: later)
        site.pages[BOOK]["lastrevid"] = LASTREVID + index
        code, out, err = fetch(capsys, data_dir, url)
        assert code == 0, err
        refs.append(out.splitlines()[1])

    assert refs == [f"source_ref: {REF}"] * 3
    assert packet_names(data_dir) == [packet_file(data_dir).name]
    rows = source_rows(data_dir)
    assert len(rows) == 1
    assert (rows[0]["ref"], rows[0]["revision"], rows[0]["fetched_at"]) == (
        REF,
        str(LASTREVID + 2),
        "2026-11-09T08:02:00Z",
    )
    assert sent_titles(site) == [BOOK, BOOK, "Happiness Hypothesis"]


# --- pages that are refused ------------------------------------------------------------


def test_a_disambiguation_page_exits_4_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    code, out, err = fetch(
        capsys, data_dir, "https://en.wikipedia.org/wiki/Happiness_Hypothesis_(disambiguation)"
    )

    assert_refused(
        code,
        out,
        err,
        data_dir,
        starts=f"CC_E_FETCH: '{DISAMBIGUATION}' is a disambiguation page",
    )
    assert "give the link of the book's own article" in err[1]
    assert len(site.sent) == 1


@pytest.mark.parametrize(
    ("url", "starts"),
    [
        (
            "https://en.wikipedia.org/wiki/No_Such_Synthetic_Book",
            "CC_E_FETCH: there is no Wikipedia article 'No Such Synthetic Book' on "
            "en.wikipedia.org",
        ),
        (
            "https://en.wikipedia.org/wiki/Special:Random",
            "CC_E_FETCH: 'Special:Random' is a special page, not an article",
        ),
        (
            "https://en.wikipedia.org/wiki/Talk:",
            "CC_E_FETCH: 'Talk:' is not a valid Wikipedia title (The requested page title is empty",
        ),
    ],
)
def test_missing_special_and_invalid_pages_exit_4(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    url: str,
    starts: str,
) -> None:
    code, out, err = fetch(capsys, data_dir, url)

    assert_refused(code, out, err, data_dir, starts=starts)


def test_an_article_without_text_exits_4(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    site.pages[BOOK]["extract"] = TAIL  # only a reference tail

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code, out, err, data_dir, starts=f"CC_E_FETCH: the Wikipedia article '{BOOK}' has no text"
    )


def test_an_article_without_headings_is_written_with_a_warning(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    site.pages[BOOK]["extract"] = "A synthetic stub article in one paragraph."

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert code == 0, err
    assert err[1:] == [f"warning: {wiki.NO_SECTIONS_WARNING}"]
    assert out.splitlines()[3] == "counts: 42/0"
    payload = json.loads(packet_file(data_dir).read_bytes())
    assert (payload["sections"], payload["warnings"]) == ([], [wiki.NO_SECTIONS_WARNING])


def test_api_warnings_reach_the_packet_and_stderr(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    def warn(document: dict[str, Any]) -> None:
        document["warnings"] = {"extracts": {"warnings": "A synthetic\nwarning \x1b[31mtext."}}

    site.edit = warn

    code, _, err = fetch(capsys, data_dir, DESKTOP)

    assert code == 0, err
    line = "Wikipedia API warning (extracts): A synthetic warning [31mtext."
    assert err[1:] == [f"warning: {line}"]
    assert json.loads(packet_file(data_dir).read_bytes())["warnings"] == [line]


# --- 429/503 retry and HTTP errors -----------------------------------------------------


def test_a_429_with_retry_after_1_retries_once(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    site.replies.append(Reply(429, b"Too many requests", {"Retry-After": "1"}))

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert code == 0, err
    assert site.sleeps == [1]
    assert len(site.sent) == 2
    assert site.sent[0] == site.sent[1]  # the same request, the same user agent
    assert f"source_ref: {REF}" in out.splitlines()
    assert packet_file(data_dir).read_bytes() == PACKET_FIXTURE.read_bytes()


def test_a_503_without_retry_after_waits_5_seconds(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    site.replies.append(Reply(503, b"<html>maintenance</html>"))

    code, _, err = fetch(capsys, data_dir, DESKTOP)

    assert code == 0, err
    assert site.sleeps == [wiki.RETRY_AFTER_DEFAULT_S] == [5]
    assert len(site.sent) == 2


def test_a_second_429_exits_4_after_one_retry(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    site.replies.extend(
        [Reply(429, b"", {"Retry-After": "1"}), Reply(429, b"", {"Retry-After": "1"})]
    )

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code,
        out,
        err,
        data_dir,
        starts=f"CC_E_FETCH: could not fetch '{BOOK}' from en.wikipedia.org: Wikipedia "
        "answered HTTP 429 again after one retry",
    )
    assert site.sleeps == [1]
    assert len(site.sent) == 2


def test_a_retry_after_over_the_limit_exits_4_without_waiting(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    site.replies.append(Reply(503, b"", {"Retry-After": "3600"}))

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(code, out, err, data_dir, starts="CC_E_FETCH: could not fetch ")
    assert "asks to wait 3600 s" in err[1]
    assert site.sleeps == []
    assert len(site.sent) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 500, 502])
def test_other_http_errors_exit_4_without_a_retry(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia, status: int
) -> None:
    site.replies.append(Reply(status, b"error page", {"Retry-After": "1"}))

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code,
        out,
        err,
        data_dir,
        starts=f"CC_E_FETCH: could not fetch '{BOOK}' from en.wikipedia.org: Wikipedia "
        f"answered HTTP {status}",
    )
    assert site.sleeps == []
    assert len(site.sent) == 1


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (URLError(socket.gaierror(11001, "getaddrinfo failed")), "URLError"),
        (URLError(ConnectionRefusedError(10061, "connection refused")), "URLError"),
        (TimeoutError("timed out"), "TimeoutError"),
        (ConnectionResetError(10054, "reset by peer"), "ConnectionResetError"),
    ],
)
def test_network_errors_exit_4_naming_the_class(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    error: BaseException,
    name: str,
) -> None:
    site.replies.append(error)

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code,
        out,
        err,
        data_dir,
        starts=f"CC_E_FETCH: could not fetch '{BOOK}' from en.wikipedia.org: {name}",
    )
    assert err[1].endswith("check the network connection")
    assert site.sleeps == []


class TimesOut(io.RawIOBase):
    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        raise TimeoutError("The read operation timed out")


def test_a_timeout_while_reading_the_answer_exits_4(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(request: Request, timeout: float | None = None) -> addinfourl:
        site.sent.append(Sent(request.full_url, dict(request.header_items()), timeout))
        return addinfourl(TimesOut(), Message(), request.full_url, 200)

    monkeypatch.setattr(wiki, "urlopen", slow)

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code,
        out,
        err,
        data_dir,
        starts=f"CC_E_FETCH: could not fetch '{BOOK}' from en.wikipedia.org: TimeoutError",
    )


# --- malformed answers -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "starts"),
    [
        (
            Reply(200, b"<html>captive portal</html>", {"Content-Type": "text/html"}),
            "CC_E_FETCH: Wikipedia's answer is not UTF-8 JSON (JSONDecodeError)",
        ),
        (
            Reply(200, b"\xff\xfe{}", {}),
            "CC_E_FETCH: Wikipedia's answer is not UTF-8 JSON (UnicodeDecodeError)",
        ),
        (
            Reply(200, b"[" * 100_000 + b"]" * 100_000, {}),
            "CC_E_FETCH: Wikipedia's answer is not UTF-8 JSON (RecursionError)",
        ),
        (Reply(200, b"[]", {}), "CC_E_FETCH: Wikipedia's answer is not a JSON object"),
        (
            Reply(200, b"not gzip at all", {"Content-Encoding": "gzip"}),
            "CC_E_FETCH: Wikipedia's gzip answer could not be decompressed",
        ),
        (
            Reply(200, gzip.compress(b'{"query": {}}')[:-12], {"Content-Encoding": "gzip"}),
            "CC_E_FETCH: Wikipedia's gzip answer is truncated",
        ),
        (
            Reply(200, b"{}", {"Content-Encoding": "br"}),
            "CC_E_FETCH: Wikipedia answered with Content-Encoding 'br'",
        ),
        (
            Reply(
                200,
                b'{"error": {"code": "maxlag", "info": "Waiting for a synthetic replica"}}',
                {},
            ),
            f"CC_E_FETCH: Wikipedia refused the query for '{BOOK}' (maxlag: Waiting for a "
            "synthetic replica)",
        ),
        (
            Reply(200, b'{"query": {}}', {}),
            f"CC_E_FETCH: Wikipedia's answer for '{BOOK}' is malformed (no query.pages)",
        ),
        (
            Reply(200, b'{"query": {"interwiki": [{"title": "wikt:x", "iw": "wikt"}]}}', {}),
            f"CC_E_FETCH: '{BOOK}' points to another wiki",
        ),
    ],
)
def test_a_malformed_answer_exits_4(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    reply: Reply,
    starts: str,
) -> None:
    site.replies.append(reply)

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(code, out, err, data_dir, starts=starts)
    assert len(err[1]) < 600


@pytest.mark.parametrize(
    ("field_name", "value", "detail"),
    [
        ("extract", None, "no extract"),
        ("extract", "\ud800 lone surrogate", "no extract"),
        ("canonicalurl", None, "no canonicalurl"),
        ("canonicalurl", "https://evil.example/wiki/X", "canonicalurl is not on a"),
        ("canonicalurl", "https://en.wikipedia.org/w/index.php?title=X", "canonicalurl does"),
        ("lastrevid", None, "no lastrevid"),
        ("lastrevid", True, "no lastrevid"),
        ("lastrevid", "123", "no lastrevid"),
        ("title", "", "no title"),
    ],
)
def test_a_page_without_its_fields_exits_4(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    field_name: str,
    value: Any,
    detail: str,
) -> None:
    if value is None:
        del site.pages[BOOK][field_name]
    else:
        site.pages[BOOK][field_name] = value

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code,
        out,
        err,
        data_dir,
        starts=f"CC_E_FETCH: Wikipedia's answer for '{BOOK}' is malformed ({detail}",
    )


def test_an_oversized_answer_exits_4(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wiki, "RESPONSE_MAX_BYTES", 1000)
    site.use_gzip = False

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code, out, err, data_dir, starts="CC_E_FETCH: Wikipedia's answer is over 1,000 bytes"
    )


def test_a_gzip_answer_that_inflates_past_the_cap_exits_4(
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
    site: FakeWikipedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wiki, "RESPONSE_MAX_BYTES", 5000)
    bomb = gzip.compress(b'{"query": {"pages": [' + b" " * 100_000 + b"]}}")
    assert len(bomb) < 5000
    site.replies.append(Reply(200, bomb, {"Content-Encoding": "gzip"}))

    code, out, err = fetch(capsys, data_dir, DESKTOP)

    assert_refused(
        code,
        out,
        err,
        data_dir,
        starts="CC_E_FETCH: Wikipedia's answer is over 5,000 bytes once decompressed",
    )


# --- reviewed sources ------------------------------------------------------------------


@pytest.mark.parametrize("url", [DESKTOP, MOBILE, REDIRECT])
def test_a_reviewed_article_exits_3_for_every_url_form(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia, url: str
) -> None:
    assert fetch(capsys, data_dir, DESKTOP)[0] == 0
    mark_reviewed(data_dir, REF)
    before_rows = source_rows(data_dir)
    before_packet = packet_file(data_dir).read_bytes()
    site.pages[BOOK]["lastrevid"] = LASTREVID + 1
    site.pages[BOOK]["extract"] = "A changed synthetic article."

    code, out, err = fetch(capsys, data_dir, url)

    assert code == 3
    assert out == ""
    assert err[1].startswith(f"CC_ALREADY_REVIEWED: {REF} was already reviewed at ")
    assert len(err) == 2
    assert source_rows(data_dir) == before_rows
    assert packet_file(data_dir).read_bytes() == before_packet
    assert packet_names(data_dir) == [packet_file(data_dir).name]
    # The canonical title comes only from the answer, so one request was made.
    assert len(site.sent) == 2


def test_an_article_reviewed_during_the_fetch_is_refused_and_not_overwritten(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    assert fetch(capsys, data_dir, DESKTOP)[0] == 0
    before_packet = packet_file(data_dir).read_bytes()
    site.pages[BOOK]["extract"] = "A changed synthetic article."
    site.on_request = lambda: mark_reviewed(data_dir, REF)  # `propose` lands meanwhile

    code, out, err = fetch(capsys, data_dir, MOBILE)

    assert code == 3
    assert out == ""
    assert err[1].startswith("CC_ALREADY_REVIEWED: ")
    assert packet_file(data_dir).read_bytes() == before_packet
    assert source_rows(data_dir)[0]["revision"] == str(LASTREVID)


# --- input and setup errors ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://en.wikipedia.org/",
        "https://www.wikipedia.org/",
        "https://en.wikipedia.org/w/index.php?search=happiness",
        "https://en.wikipedia.org/wiki/A%7CB",
    ],
)
def test_a_wikipedia_url_naming_no_article_exits_4_without_a_request(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia, url: str
) -> None:
    code, out, err = fetch(capsys, data_dir, url)

    assert_refused(
        code, out, err, data_dir, starts="CC_E_FETCH: not a supported Wikipedia article URL"
    )
    assert site.sent == []


def test_a_lookalike_host_is_not_wikipedia(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    code, out, err = fetch(
        capsys, data_dir, "https://en.wikipedia.org.evil.example/wiki/The_Happiness_Hypothesis"
    )

    assert_refused(code, out, err, data_dir, starts="CC_E_FETCH: not a supported YouTube video URL")
    assert site.sent == []


def test_fetch_without_a_database_exits_2_without_a_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], site: FakeWikipedia
) -> None:
    target = tmp_path / "data"

    code, out, err = fetch(capsys, target, DESKTOP)

    assert code == 2
    assert out == ""
    assert err[1].startswith("CC_E_CONFIG: no database at ")
    assert not target.exists()
    assert site.sent == []


def test_the_written_packet_reads_back(
    capsys: pytest.CaptureFixture[str], data_dir: Path, site: FakeWikipedia
) -> None:
    assert fetch(capsys, data_dir, MOBILE)[0] == 0

    stored = packet.read_packet(packet_file(data_dir))

    assert stored.source_ref == REF
    assert stored.counts() == f"{len(fixture_packet()['text'])}/12"
    assert stored.transcript is None and stored.duration_s is None and stored.chapters == ()


def test_fetch_help_names_wikipedia(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["fetch", "--help"]) == 0
    assert "Wikipedia article URL" in " ".join(capsys.readouterr().out.split())
