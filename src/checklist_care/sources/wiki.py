"""Wikipedia sources (plan §4.3): article URL → title, one Action API request, packet.

**URL input.** ``https://<lang>.wikipedia.org/wiki/<Title>`` (the path is decoded with
``urllib.parse.unquote``, not ``unquote_plus``, so ``C++`` keeps its pluses),
``/w/index.php?title=<Title>``, the mobile ``<lang>.m.wikipedia.org`` host and a
``#fragment``, with or without a scheme. Another host, or a Wikipedia URL that names no
article, raises :class:`FetchError` (exit 4).

**Request.** One title per request (two full extracts in one request trigger an
``exlimit`` warning and drop the second page's extract)::

    GET https://<lang>.wikipedia.org/w/api.php?action=query&format=json&formatversion=2
        &redirects=1&titles=<T>&prop=extracts|info|pageprops&explaintext=1&inprop=url
        &ppprop=disambiguation

It always carries ``User-Agent: checklist-care/<version> (+<PROJECT_URL>)`` (a missing
user agent, or Python's default ``Python-urllib/3.x``, gets HTTP 403) and
``Accept-Encoding: gzip``; a gzip answer is decompressed here, since ``urllib`` does
not. On HTTP 429 or 503 it sleeps ``int(Retry-After or 5)`` seconds and retries once,
then fails. Wikimedia asks for concurrency 1 and fewer than 5 requests per second;
one ``fetch`` sends one request, or two with the retry.

**Seams.** ``urlopen`` and ``sleep`` are module globals looked up at call time, so
``monkeypatch.setattr(wiki, "urlopen", fake)`` replaces only the transport: the URL,
its query parameters and the headers are still built here.

**Answer.** ``query.pages[0]`` gives ``extract`` (the packet text), ``title``,
``canonicalurl`` (``source.url`` and the ref's title) and ``lastrevid``
(``source.revision``). A missing, invalid or special (``ns: -1``) page and a
disambiguation page (``pageprops.disambiguation``) exit 4. The ref is
``wiki:<lang>:<canonical title, underscores>`` from ``canonicalurl``, after redirects,
so every URL form of one article (desktop, mobile, a redirecting title) is one source.

**Text shape.** Sections arrive as ``\\n\\n\\n== H ==\\n`` (subsections ``=== H ===``).
The trailing run of ``See also``, ``References``, ``Notes``, ``Citations``,
``Sources``, ``Bibliography``, ``Further reading``, ``External links`` and
``Footnotes`` sections is cut, and every remaining heading becomes a packet section
(``heading``, ``level`` = the number of ``=``, ``offset`` = where its heading line
starts in the packet text).

Everything in the answer is untrusted data: it is stored, never followed, and only
bounded single-line excerpts of it reach an error message.
"""

from __future__ import annotations

import json
import re
import unicodedata
import zlib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from http.client import HTTPException
from time import sleep
from typing import Any, Final
from urllib.error import HTTPError
from urllib.parse import SplitResult, parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from checklist_care import USER_AGENT
from checklist_care.errors import FetchError, one_line
from checklist_care.limits import SOURCE_KIND_WIKI
from checklist_care.sources.packet import Packet, Section
from checklist_care.sources.text import normalize_text

# --- hosts and titles ----------------------------------------------------------------

WIKIPEDIA_DOMAIN: Final = "wikipedia.org"
MOBILE_LABEL: Final = "m"
NON_LANGUAGE_LABELS: Final = frozenset({"www", MOBILE_LABEL})
"""Subdomains of ``wikipedia.org`` that name no language edition."""

LANGUAGE_PATTERN: Final = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
"""A language edition's subdomain (``en``, ``simple``, ``zh-min-nan``), matched with
``fullmatch``."""
LANGUAGE_MAX: Final = 24

ARTICLE_PATH: Final = "/wiki/"
INDEX_PATH: Final = "/w/index.php"
API_PATH: Final = "/w/api.php"
SCHEMES: Final = ("http", "https")

TITLE_MAX_BYTES: Final = 255
"""MediaWiki's limit on a title, in UTF-8 bytes."""
TITLE_SEPARATOR: Final = "|"
"""Separates titles in ``titles=``; one request asks for exactly one title."""
TITLE_FORBIDDEN: Final = frozenset("#<>[]{}" + TITLE_SEPARATOR)
"""Characters MediaWiki never allows in a title."""

# --- the request ---------------------------------------------------------------------

API_PROP: Final = "extracts|info|pageprops"
"""``prop=`` of the verified request; its ``|`` separators go out unescaped."""

REQUEST_HEADERS: Final = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
"""Sent with every request. Never put an email address in the user agent."""

RETRY_STATUSES: Final = frozenset({429, 503})
RETRY_AFTER_DEFAULT_S: Final = 5
"""The wait when a 429 or 503 has no usable ``Retry-After`` (plan §4.3)."""
RETRY_AFTER_MAX_S: Final = 60
"""The longest ``Retry-After`` this command waits out; a longer one fails at once
rather than leave the operator watching a silent command for minutes."""
ATTEMPTS: Final = 2
"""The first request and one retry."""

HTTP_TIMEOUT_S: Final = 30.0
RESPONSE_MAX_BYTES: Final = 16 * 1024 * 1024
"""The largest answer read, compressed or not. One article's plain text is far smaller;
the cap only stops a runaway or decompression-bomb answer."""
_READ_CHUNK: Final = 64 * 1024
_GZIP_WBITS: Final = zlib.MAX_WBITS | 16
_IDENTITY_ENCODINGS: Final = ("", "identity")

# --- the answer and the text -----------------------------------------------------------

TAIL_HEADINGS: Final = (
    "See also",
    "References",
    "Notes",
    "Citations",
    "Sources",
    "Bibliography",
    "Further reading",
    "External links",
    "Footnotes",
)
"""Sections cut from the end of an article: lists of links and sources, no content."""
_TAIL_KEYS: Final = frozenset(heading.casefold() for heading in TAIL_HEADINGS)

HEADING_PATTERN: Final = re.compile(
    r"^(?P<marks>={2,6})(?!=)[ \t]*(?P<heading>[^\n]*?)[ \t]*(?<!=)(?P=marks)[ \t]*$",
    re.MULTILINE,
)
"""One heading line of a plain-text extract, ``== H ==`` to ``====== H ======``, with
the same number of ``=`` on both sides."""

NO_SECTIONS_WARNING: Final = "the article has no section headings; split the text by content"

_INPUT_ECHO_MAX: Final = 200
_DETAIL_MAX: Final = 200
_API_WARNINGS_MAX: Final = 5

_PAGE_FIELDS: Final[dict[str, Any]] = {
    "title": None,
    "canonicalurl": None,
    "extract": None,
    "lastrevid": None,
}
"""The page fields an article needs, None when the answer lacks one. They are read as
keys, as payload field names: ``extract`` is the API's field, not the ``extract`` mode."""


@dataclass(frozen=True)
class WikiTitle:
    """The article a URL names: its language edition and its title as given."""

    lang: str
    title: str
    """Decoded, with underscores as spaces; the API normalizes and follows redirects."""

    @property
    def host(self) -> str:
        """The desktop host, ``<lang>.wikipedia.org``; every request goes there."""

        return f"{self.lang}.{WIKIPEDIA_DOMAIN}"


@dataclass(frozen=True)
class Article:
    """What the packet and the ``source`` row take from one API answer."""

    ref: str
    title: str
    url: str
    revision: str
    extract: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class FetchedArticle:
    """The article's packet and its ``source.revision`` (the ``lastrevid``), which the
    packet format has no field for."""

    packet: Packet
    revision: str


def _shown(text: str, limit: int = _DETAIL_MAX) -> str:
    """Untrusted text as one bounded line without control characters."""

    printable = "".join(char for char in one_line(text) if char.isprintable())
    return printable[:limit]


# --- URL → title ---------------------------------------------------------------------


def _unsupported(raw: str) -> FetchError:
    return FetchError(
        f"not a supported Wikipedia article URL: '{_shown(raw, _INPUT_ECHO_MAX)}'. Give a "
        "link of the form https://<lang>.wikipedia.org/wiki/<Title>; the mobile "
        "<lang>.m.wikipedia.org host and /w/index.php?title=<Title> work too"
    )


def _split(raw: str) -> SplitResult | None:
    text = raw.strip()
    url = text if "://" in text else f"https://{text}"
    try:
        return urlsplit(url)
    except ValueError:  # such as an unclosed IPv6 bracket
        return None


def _hostname(parts: SplitResult) -> str:
    return (parts.hostname or "").rstrip(".")


def is_wikipedia_url(raw: str) -> bool:
    """True when ``raw`` is a URL on ``wikipedia.org`` or one of its subdomains, so
    ``fetch`` treats it as a Wikipedia source (and a malformed one gets the Wikipedia
    rejection, not the YouTube one)."""

    parts = _split(raw)
    if parts is None:
        return False
    host = _hostname(parts)
    return host == WIKIPEDIA_DOMAIN or host.endswith(f".{WIKIPEDIA_DOMAIN}")


def language_of_host(host: str, *, mobile: bool) -> str | None:
    """The language edition of ``<lang>.wikipedia.org`` (and, when ``mobile``, of
    ``<lang>.m.wikipedia.org``), or None for any other host."""

    suffix = f".{WIKIPEDIA_DOMAIN}"
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    mobile_suffix = f".{MOBILE_LABEL}"
    if mobile and label.endswith(mobile_suffix):
        label = label[: -len(mobile_suffix)]
    if (
        label in NON_LANGUAGE_LABELS
        or len(label) > LANGUAGE_MAX
        or not LANGUAGE_PATTERN.fullmatch(label)
    ):
        return None
    return label


def clean_title(raw: str) -> str | None:
    """A decoded title with underscores as spaces, or None when it cannot be a
    MediaWiki title: empty, over 255 UTF-8 bytes, or holding ``# < > [ ] { } |`` or a
    control character."""

    title = raw.replace("_", " ").strip()
    if not title:
        return None
    if any(char in TITLE_FORBIDDEN or unicodedata.category(char) == "Cc" for char in title):
        return None
    try:
        size = len(title.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate
        return None
    return title if size <= TITLE_MAX_BYTES else None


def _raw_title(parts: SplitResult) -> str | None:
    if parts.path.startswith(ARTICLE_PATH):
        return unquote(parts.path[len(ARTICLE_PATH) :], errors="strict")
    if parts.path == INDEX_PATH:
        values = parse_qs(parts.query, errors="strict").get("title")
        return values[0] if values else None
    return None


def parse_wiki_url(raw: str) -> WikiTitle:
    """The language edition and title of a Wikipedia article URL.

    Accepts ``/wiki/<Title>`` (percent-decoded with ``unquote``, so ``+`` stays a
    plus), ``/w/index.php?title=<Title>`` (a query string, where ``+`` is a space), the
    ``<lang>.m.wikipedia.org`` host and a ``#fragment``, over http or https, with or
    without a scheme. Raises :class:`FetchError` (exit 4) for anything else.
    """

    parts = _split(raw)
    if parts is None or parts.scheme.lower() not in SCHEMES:
        raise _unsupported(raw)
    lang = language_of_host(_hostname(parts), mobile=True)
    if lang is None:
        raise _unsupported(raw)
    try:
        title = _raw_title(parts)
    except UnicodeDecodeError as error:  # a percent-escape that is not UTF-8
        raise _unsupported(raw) from error
    cleaned = clean_title(title) if title is not None else None
    if cleaned is None:
        raise _unsupported(raw)
    return WikiTitle(lang=lang, title=cleaned)


def source_ref(lang: str, canonical_title: str) -> str:
    """``wiki:<lang>:<canonical title, underscores>``, the dedupe key (plan §3.2)."""

    return f"{SOURCE_KIND_WIKI}:{lang}:{canonical_title}"


def canonical_ref(canonical_url: str) -> str:
    """The ref of an article from the answer's ``canonicalurl``
    (``https://<lang>.wikipedia.org/wiki/<Title>``): the title percent-decoded, with
    underscores. ``ValueError`` when the URL is not of that form."""

    parts = urlsplit(canonical_url)
    lang = language_of_host(_hostname(parts), mobile=False)
    if parts.scheme.lower() not in SCHEMES or lang is None:
        raise ValueError("canonicalurl is not on a <lang>.wikipedia.org host")
    if not parts.path.startswith(ARTICLE_PATH):
        raise ValueError(f"canonicalurl does not start with {ARTICLE_PATH}")
    title = clean_title(unquote(parts.path[len(ARTICLE_PATH) :], errors="strict"))
    if title is None:
        raise ValueError("canonicalurl names no valid title")
    return source_ref(lang, title.replace(" ", "_"))


# --- the request ---------------------------------------------------------------------


def api_url(target: WikiTitle) -> str:
    """The verified Action API query of plan §4.3 for one title."""

    if TITLE_SEPARATOR in target.title:
        raise ValueError("one request asks for exactly one title")
    query = urlencode(
        [
            ("action", "query"),
            ("format", "json"),
            ("formatversion", "2"),
            ("redirects", "1"),
            ("titles", target.title),
            ("prop", API_PROP),
            ("explaintext", "1"),
            ("inprop", "url"),
            ("ppprop", "disambiguation"),
        ],
        quote_via=quote,
        safe=TITLE_SEPARATOR,
    )
    return f"https://{target.host}{API_PATH}?{query}"


def make_request(url: str) -> Request:
    """A GET request with :data:`REQUEST_HEADERS`. Because the request has its own
    ``User-Agent``, urllib does not add ``Python-urllib/3.x``."""

    return Request(url, headers=dict(REQUEST_HEADERS), method="GET")


def retry_after_seconds(value: str | None) -> int:
    """``int(Retry-After or 5)`` (plan §4.3): whole seconds, or the default when the
    header is missing or not a number of seconds (an HTTP date)."""

    text = (value or "").strip()
    if text.isascii() and text.isdigit():
        return int(text)
    return RETRY_AFTER_DEFAULT_S


def _header(message: Any, name: str) -> str | None:
    if message is None:
        return None
    value = message.get(name)
    return None if value is None else str(value)


def _read_capped(response: Any) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > RESPONSE_MAX_BYTES:
            raise FetchError(
                f"Wikipedia's answer is over {RESPONSE_MAX_BYTES:,} bytes; no article is that "
                "large, so it was not read"
            )
        chunks.append(chunk)


def _gunzip(data: bytes) -> bytes:
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    try:
        body = decompressor.decompress(data, RESPONSE_MAX_BYTES + 1)
    except zlib.error as error:
        raise FetchError(
            f"Wikipedia's gzip answer could not be decompressed ({_shown(str(error))}); "
            "try again later"
        ) from error
    if len(body) > RESPONSE_MAX_BYTES:
        raise FetchError(
            f"Wikipedia's answer is over {RESPONSE_MAX_BYTES:,} bytes once decompressed; no "
            "article is that large, so it was not read"
        )
    if not decompressor.eof:
        raise FetchError("Wikipedia's gzip answer is truncated; try again later")
    return body


def read_body(response: Any) -> bytes:
    """The answer body, at most :data:`RESPONSE_MAX_BYTES`, gunzipped when the server
    sent ``Content-Encoding: gzip``."""

    data = _read_capped(response)
    encoding = (_header(response.headers, "Content-Encoding") or "").strip().lower()
    if encoding == "gzip":
        return _gunzip(data)
    if encoding in _IDENTITY_ENCODINGS:
        return data
    raise FetchError(
        f"Wikipedia answered with Content-Encoding '{_shown(encoding)}', which this "
        "command does not decode"
    )


def http_get(url: str, *, label: str) -> bytes:
    """GET ``url`` with the project headers and return the body.

    HTTP 429 and 503 are retried once after ``Retry-After`` seconds (5 when it is
    missing). Every failure raises :class:`FetchError` naming ``label``: another HTTP
    status, the second 429/503, a ``Retry-After`` over :data:`RETRY_AFTER_MAX_S`, and
    network errors (DNS, refused or reset connections, timeouts, truncated answers).
    """

    request = make_request(url)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                return read_body(response)
        except HTTPError as error:
            status = error.code
            wait = retry_after_seconds(_header(error.headers, "Retry-After"))
            with suppress(Exception):
                error.close()
            if status not in RETRY_STATUSES:
                raise FetchError(
                    f"could not fetch {label}: Wikipedia answered HTTP {status}"
                ) from error
            if attempt == ATTEMPTS:
                raise FetchError(
                    f"could not fetch {label}: Wikipedia answered HTTP {status} again after "
                    "one retry (rate limit or maintenance); try again in a few minutes"
                ) from error
            if wait > RETRY_AFTER_MAX_S:
                raise FetchError(
                    f"could not fetch {label}: Wikipedia answered HTTP {status} and asks to "
                    f"wait {wait} s, longer than the {RETRY_AFTER_MAX_S} s this command "
                    "waits; try again later"
                ) from error
            sleep(wait)
        except (OSError, HTTPException) as error:
            reason = getattr(error, "reason", None)
            detail = _shown(str(reason if reason is not None else error))
            raise FetchError(
                f"could not fetch {label}: {type(error).__name__}"
                f"{f' ({detail})' if detail else ''}; check the network connection"
            ) from error
    raise AssertionError("unreachable: the last attempt returns or raises")


# --- the answer ----------------------------------------------------------------------


def decode_answer(data: bytes) -> dict[str, Any]:
    """The answer body as a JSON object; :class:`FetchError` when it is not one (a
    proxy or captive-portal page, say)."""

    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as error:  # UnicodeDecodeError is a ValueError
        raise FetchError(
            f"Wikipedia's answer is not UTF-8 JSON ({type(error).__name__}); try again later"
        ) from error
    if not isinstance(document, dict):
        raise FetchError("Wikipedia's answer is not a JSON object; try again later")
    return document


def _flag(page: Mapping[str, Any], name: str) -> bool:
    """A formatversion=2 flag (``true`` or absent); an empty string counts as set,
    as formatversion=1 writes it."""

    return page.get(name) not in (None, False)


def _malformed(target: WikiTitle, detail: str) -> FetchError:
    return FetchError(
        f"Wikipedia's answer for '{_shown(target.title)}' is malformed ({detail}); try again later"
    )


def _utf8_text(value: Any) -> str | None:
    """``value`` if it is a string UTF-8 can encode (no lone surrogate), else None."""

    if not isinstance(value, str):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def api_warnings(document: Mapping[str, Any]) -> tuple[str, ...]:
    """The answer's ``warnings`` (``{module: {"warnings": text}}``) as bounded packet
    warnings, so a truncated or partial extract is visible."""

    raw = document.get("warnings")
    if not isinstance(raw, Mapping):
        return ()
    found: list[str] = []
    for module, body in raw.items():
        text = body.get("warnings") if isinstance(body, Mapping) else body
        if isinstance(text, str) and text.strip():
            found.append(f"Wikipedia API warning ({_shown(str(module), 40)}): {_shown(text)}")
    return tuple(found[:_API_WARNINGS_MAX])


def _refuse(page: Mapping[str, Any], target: WikiTitle) -> None:
    """Raise :class:`FetchError` for an invalid, special, missing or disambiguation page."""

    title = page.get("title")
    shown = _shown(title if isinstance(title, str) else target.title)
    if _flag(page, "invalid"):
        reason = page.get("invalidreason")
        detail = f" ({_shown(reason)})" if isinstance(reason, str) and reason.strip() else ""
        raise FetchError(f"'{shown}' is not a valid Wikipedia title{detail}; check the link")
    if _flag(page, "special") or page.get("ns") == -1:
        raise FetchError(
            f"'{shown}' is a special page, not an article; give the link of the book's "
            "Wikipedia article"
        )
    if _flag(page, "missing"):
        raise FetchError(
            f"there is no Wikipedia article '{shown}' on {target.host}; check the link"
        )
    pageprops = page.get("pageprops")
    if isinstance(pageprops, Mapping) and "disambiguation" in pageprops:
        raise FetchError(
            f"'{shown}' is a disambiguation page that lists several articles; give the link "
            "of the book's own article"
        )


def read_article(document: Mapping[str, Any], target: WikiTitle) -> Article:
    """The article in ``query.pages[0]`` of an API answer.

    An API error object, an invalid, special or missing page, a disambiguation page and
    an answer without the expected fields raise :class:`FetchError` (exit 4).
    """

    error = document.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, Mapping) else None
        info = error.get("info") if isinstance(error, Mapping) else None
        detail = ": ".join(_shown(part) for part in (code, info) if isinstance(part, str))
        raise FetchError(
            f"Wikipedia refused the query for '{_shown(target.title)}'"
            f"{f' ({detail})' if detail else ''}; try again later"
        )
    query = document.get("query")
    pages = query.get("pages") if isinstance(query, Mapping) else None
    if not isinstance(pages, list) or not pages:
        if isinstance(query, Mapping) and query.get("interwiki"):
            raise FetchError(
                f"'{_shown(target.title)}' points to another wiki, not a Wikipedia article; "
                "give the link of the book's Wikipedia article"
            )
        raise _malformed(target, "no query.pages")
    if len(pages) != 1 or not isinstance(pages[0], Mapping):
        raise _malformed(target, f"{len(pages)} pages for one title")
    page: Mapping[str, Any] = pages[0]
    _refuse(page, target)

    fields = {**_PAGE_FIELDS, **page}
    title = _utf8_text(fields["title"])
    url = _utf8_text(fields["canonicalurl"])
    extract = _utf8_text(fields["extract"])
    revision = fields["lastrevid"]
    if title is None or not title.strip():
        raise _malformed(target, "no title")
    if url is None:
        raise _malformed(target, "no canonicalurl")
    if extract is None:
        raise _malformed(target, "no extract")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise _malformed(target, "no lastrevid")
    try:
        ref = canonical_ref(url)
    except ValueError as error:  # UnicodeDecodeError (a bad percent-escape) is one
        raise _malformed(target, _shown(str(error))) from error
    return Article(
        ref=ref,
        title=title,
        url=url,
        revision=str(revision),
        extract=extract,
        warnings=api_warnings(document),
    )


# --- text shape ----------------------------------------------------------------------


def parse_sections(text: str) -> tuple[Section, ...]:
    """Every ``== H ==`` heading line of ``text``: its heading, its level (the number of
    ``=``) and the offset where the heading line starts."""

    sections: list[Section] = []
    for match in HEADING_PATTERN.finditer(text):
        heading = match.group("heading").strip()
        if heading:
            sections.append(
                Section(heading=heading, level=len(match.group("marks")), offset=match.start())
            )
    return tuple(sections)


def tail_offset(sections: Sequence[Section]) -> int | None:
    """Where the trailing run of :data:`TAIL_HEADINGS` sections starts, or None.

    Only top-level sections count (subsections go with their parent), and only a run
    at the very end: a ``Notes`` section followed by a content section is kept.
    """

    if not sections:
        return None
    top_level = min(section.level for section in sections)
    start: int | None = None
    for section in reversed(sections):
        if section.level != top_level:
            continue
        if section.heading.casefold() not in _TAIL_KEYS:
            break
        start = section.offset
    return start


def shape_text(extract: str) -> tuple[str, tuple[Section, ...]]:
    """The packet text and sections of a plain-text extract: normalized (NFC, LF), the
    trailing reference sections cut, outer white space stripped."""

    text = normalize_text(extract)
    cut = tail_offset(parse_sections(text))
    if cut is not None:
        text = text[:cut]
    text = text.strip()
    return text, parse_sections(text)


def fetch_article(target: WikiTitle, *, fetched_at: str) -> FetchedArticle:
    """Fetch one article with one API request (two with a 429/503 retry) and build its
    packet (plan §4.4): ``text`` and ``sections`` filled, ``transcript`` and
    ``duration_s`` null, no chapters."""

    label = f"'{_shown(target.title)}' from {target.host}"
    document = decode_answer(http_get(api_url(target), label=label))
    article = read_article(document, target)
    text, sections = shape_text(article.extract)
    if not text:
        raise FetchError(
            f"the Wikipedia article '{_shown(article.title)}' has no text; give the link of "
            "the book's article or paste its text"
        )
    warnings = list(article.warnings)
    if not sections:
        warnings.append(NO_SECTIONS_WARNING)
    packet = Packet(
        source_ref=article.ref,
        kind=SOURCE_KIND_WIKI,
        url=article.url,
        title=article.title,
        channel=None,
        duration_s=None,
        fetched_at=fetched_at,
        transcript=None,
        chapters=(),
        text=text,
        sections=sections,
        warnings=tuple(warnings),
    )
    return FetchedArticle(packet=packet, revision=article.revision)
