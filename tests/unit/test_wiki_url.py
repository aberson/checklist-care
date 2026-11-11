"""Wikipedia URL → title, the API request and the ref (plan §4.3, §3.2)."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest

from checklist_care import PROJECT_URL, USER_AGENT, __version__, paths
from checklist_care.errors import EXIT_FETCH, FetchError
from checklist_care.sources import wiki

BOOK = "The Happiness Hypothesis"

ACCEPTED = [
    # /wiki/<Title>, with a fragment, a query or a trailing slash in the host
    ("https://en.wikipedia.org/wiki/The_Happiness_Hypothesis", "en", BOOK),
    ("https://en.wikipedia.org/wiki/The_Happiness_Hypothesis#Reception", "en", BOOK),
    ("https://en.wikipedia.org/wiki/The_Happiness_Hypothesis?oldid=12345", "en", BOOK),
    ("https://en.wikipedia.org/wiki/The%20Happiness%20Hypothesis", "en", BOOK),
    ("http://en.wikipedia.org/wiki/The_Happiness_Hypothesis", "en", BOOK),
    ("https://en.wikipedia.org./wiki/The_Happiness_Hypothesis", "en", BOOK),
    # the mobile host
    ("https://en.m.wikipedia.org/wiki/The_Happiness_Hypothesis", "en", BOOK),
    ("https://en.m.wikipedia.org/wiki/The_Happiness_Hypothesis#Ch.2", "en", BOOK),
    # index.php?title= (a query string, where + is a space)
    ("https://en.wikipedia.org/w/index.php?title=The_Happiness_Hypothesis", "en", BOOK),
    (
        "https://en.wikipedia.org/w/index.php?title=The+Happiness+Hypothesis&action=history",
        "en",
        BOOK,
    ),
    (
        "https://en.m.wikipedia.org/w/index.php?action=edit&title=The_Happiness_Hypothesis",
        "en",
        BOOK,
    ),
    # case, white space, no scheme
    ("HTTPS://EN.WIKIPEDIA.ORG/wiki/The_Happiness_Hypothesis", "en", BOOK),
    ("  https://en.wikipedia.org/wiki/The_Happiness_Hypothesis\n", "en", BOOK),
    ("en.wikipedia.org/wiki/The_Happiness_Hypothesis", "en", BOOK),
    ("en.m.wikipedia.org/wiki/The_Happiness_Hypothesis", "en", BOOK),
    # unquote, not unquote_plus: a + in the path stays a plus
    ("https://en.wikipedia.org/wiki/C++", "en", "C++"),
    ("https://en.wikipedia.org/wiki/C%2B%2B", "en", "C++"),
    ("https://en.wikipedia.org/wiki/Caf%C3%A9_society", "en", "Café society"),
    ("https://en.wikipedia.org/wiki/AC/DC", "en", "AC/DC"),
    ("https://en.wikipedia.org/wiki/What%3F_(film)", "en", "What? (film)"),
    ("https://en.wikipedia.org/wiki/AT%26T", "en", "AT&T"),
    # other language editions
    ("https://de.wikipedia.org/wiki/Gl%C3%BCck", "de", "Glück"),
    ("https://simple.m.wikipedia.org/wiki/Happiness", "simple", "Happiness"),
    ("https://zh-min-nan.wikipedia.org/wiki/Khoai-lo%CC%8Dk", "zh-min-nan", "Khoai-lo̍k"),
]

REJECTED = [
    # not a language edition of Wikipedia
    "https://www.wikipedia.org/wiki/The_Happiness_Hypothesis",
    "https://wikipedia.org/wiki/The_Happiness_Hypothesis",
    "https://m.wikipedia.org/wiki/The_Happiness_Hypothesis",
    "https://en.m.m.wikipedia.org/wiki/The_Happiness_Hypothesis",
    "https://en.wiktionary.org/wiki/happiness",
    "https://en.wikipedia.org.evil.example/wiki/The_Happiness_Hypothesis",
    "https://notwikipedia.org/wiki/The_Happiness_Hypothesis",
    "https://example.com/wiki/The_Happiness_Hypothesis",
    "https://www.youtube.com/watch?v=LpeDuC2sQ-w",
    "ftp://en.wikipedia.org/wiki/The_Happiness_Hypothesis",
    "https://[bad-ipv6/wiki/The_Happiness_Hypothesis",
    # no article named
    "https://en.wikipedia.org/",
    "https://en.wikipedia.org/wiki/",
    "https://en.wikipedia.org/wiki/_",
    "https://en.wikipedia.org/wiki/%20%20",
    "https://en.wikipedia.org/w/index.php?search=happiness",
    "https://en.wikipedia.org/w/index.php?title=",
    "https://en.wikipedia.org/w/api.php?titles=The_Happiness_Hypothesis",
    "https://en.wikipedia.org/The_Happiness_Hypothesis",
    # not a possible title
    "https://en.wikipedia.org/wiki/A%7CB",  # | would ask for two titles
    "https://en.wikipedia.org/w/index.php?title=A|B",
    "https://en.wikipedia.org/wiki/A%23B",
    "https://en.wikipedia.org/wiki/%5BA%5D",
    "https://en.wikipedia.org/wiki/A%3CB%3E",
    "https://en.wikipedia.org/wiki/A%0AB",
    "https://en.wikipedia.org/wiki/%FF",  # not UTF-8
    "https://en.wikipedia.org/w/index.php?title=%FF",
    "https://en.wikipedia.org/wiki/" + "x" * 256,
    "https://en.wikipedia.org/wiki/" + "%C3%A9" * 128,  # 256 UTF-8 bytes
    "",
    "   ",
]


@pytest.mark.parametrize(("url", "lang", "title"), ACCEPTED)
def test_accepted_forms_give_the_language_and_title(url: str, lang: str, title: str) -> None:
    assert wiki.parse_wiki_url(url) == wiki.WikiTitle(lang=lang, title=title)


@pytest.mark.parametrize("url", REJECTED)
def test_rejected_forms_raise_a_fetch_error(url: str) -> None:
    with pytest.raises(FetchError) as caught:
        wiki.parse_wiki_url(url)
    assert caught.value.exit_code == EXIT_FETCH
    assert caught.value.line().startswith("CC_E_FETCH: not a supported Wikipedia article URL")


def test_a_255_byte_title_is_accepted() -> None:
    assert wiki.parse_wiki_url("https://en.wikipedia.org/wiki/" + "x" * 255).title == "x" * 255


def test_rejection_message_is_one_bounded_line() -> None:
    with pytest.raises(FetchError) as caught:
        wiki.parse_wiki_url("https://en.wikipedia.org/\n" + "x" * 1000 + "\x1b[31m")
    line = caught.value.line()
    assert "\n" not in line and "\x1b" not in line
    assert len(line) < 600


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://en.wikipedia.org/wiki/X", True),
        ("https://en.m.wikipedia.org/wiki/X", True),
        ("en.wikipedia.org/wiki/X", True),
        ("https://www.wikipedia.org/", True),  # gets the Wikipedia rejection
        ("https://wikipedia.org", True),
        ("https://en.wikipedia.org/w/index.php?search=x", True),
        ("https://en.wikipedia.org.evil.example/wiki/X", False),
        ("https://notwikipedia.org/wiki/X", False),
        ("https://www.youtube.com/watch?v=LpeDuC2sQ-w", False),
        ("LpeDuC2sQ-w", False),
        ("https://[bad-ipv6/wiki/X", False),
        ("", False),
    ],
)
def test_is_wikipedia_url_picks_the_source_kind_by_host(url: str, expected: bool) -> None:
    assert wiki.is_wikipedia_url(url) is expected


def test_every_form_of_the_book_asks_one_api_request() -> None:
    forms = [url for url, _, title in ACCEPTED if title == BOOK]
    assert {wiki.api_url(wiki.parse_wiki_url(url)) for url in forms} == {
        wiki.api_url(wiki.WikiTitle("en", BOOK))
    }


# --- the request -----------------------------------------------------------------------


def test_api_url_is_the_verified_request() -> None:
    assert wiki.api_url(wiki.WikiTitle("en", BOOK)) == (
        "https://en.wikipedia.org/w/api.php?action=query&format=json&formatversion=2"
        "&redirects=1&titles=The%20Happiness%20Hypothesis&prop=extracts|info|pageprops"
        "&explaintext=1&inprop=url&ppprop=disambiguation"
    )


def test_api_url_encodes_the_title_as_one_parameter() -> None:
    url = wiki.api_url(wiki.WikiTitle("de", "Café & Co? 50%/x+y=z"))
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "de.wikipedia.org", "/w/api.php")
    pairs = parse_qsl(parts.query, strict_parsing=True)
    assert [value for key, value in pairs if key == "titles"] == ["Café & Co? 50%/x+y=z"]
    assert [key for key, _ in pairs] == [
        "action",
        "format",
        "formatversion",
        "redirects",
        "titles",
        "prop",
        "explaintext",
        "inprop",
        "ppprop",
    ]


def test_api_url_refuses_two_titles() -> None:
    with pytest.raises(ValueError, match="one title"):
        wiki.api_url(wiki.WikiTitle("en", "A|B"))


def test_the_request_carries_the_project_user_agent_and_gzip() -> None:
    request = wiki.make_request(wiki.api_url(wiki.WikiTitle("en", BOOK)))

    assert request.get_method() == "GET"
    assert request.get_header("User-agent") == USER_AGENT
    assert request.get_header("Accept-encoding") == "gzip"
    assert USER_AGENT == f"checklist-care/{__version__} (+{PROJECT_URL})"
    assert "Python-urllib" not in USER_AGENT
    assert "@" not in USER_AGENT  # never an email address


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("1", 1),
        ("0", 0),
        (" 7 ", 7),
        ("120", 120),
        (None, 5),
        ("", 5),
        ("Wed, 11 Nov 2026 18:00:00 GMT", 5),  # an HTTP date
        ("-1", 5),
        ("1.5", 5),
        ("٣", 5),  # a non-ASCII digit
    ],
)
def test_retry_after_is_whole_seconds_or_5(value: str | None, seconds: int) -> None:
    assert wiki.retry_after_seconds(value) == seconds


# --- the ref ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("canonical_url", "ref"),
    [
        (
            "https://en.wikipedia.org/wiki/The_Happiness_Hypothesis",
            "wiki:en:The_Happiness_Hypothesis",
        ),
        ("https://en.wikipedia.org/wiki/Caf%C3%A9_society", "wiki:en:Café_society"),
        ("https://en.wikipedia.org/wiki/What%3F_(film)", "wiki:en:What?_(film)"),
        ("https://en.wikipedia.org/wiki/AC/DC", "wiki:en:AC/DC"),
        ("https://de.wikipedia.org/wiki/Gl%C3%BCck", "wiki:de:Glück"),
    ],
)
def test_canonical_ref_takes_the_title_from_canonicalurl(canonical_url: str, ref: str) -> None:
    assert wiki.canonical_ref(canonical_url) == ref


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://en.m.wikipedia.org/wiki/The_Happiness_Hypothesis",  # never a mobile URL
        "https://en.wikipedia.org.evil.example/wiki/The_Happiness_Hypothesis",
        "https://en.wikipedia.org/w/index.php?title=The_Happiness_Hypothesis",
        "https://en.wikipedia.org/wiki/",
        "https://en.wikipedia.org/wiki/%FF",
        "javascript:alert(1)",
        "",
    ],
)
def test_canonical_ref_refuses_other_urls(canonical_url: str) -> None:
    with pytest.raises(ValueError):
        wiki.canonical_ref(canonical_url)


def test_the_book_ref_and_its_packet_name() -> None:
    ref = wiki.source_ref("en", "The_Happiness_Hypothesis")
    assert ref == "wiki:en:The_Happiness_Hypothesis"
    assert paths.source_key(ref).startswith("wiki-en_The_Happiness_Hypothesis-")
