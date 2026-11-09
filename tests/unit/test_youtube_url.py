"""YouTube URL → video id (plan §4.3): every accepted form and the rejections."""

from __future__ import annotations

import pytest

from checklist_care import paths
from checklist_care.errors import EXIT_FETCH, FetchError
from checklist_care.sources import youtube

VIDEO = "LpeDuC2sQ-w"
OUT_OF_SAMPLE = "rgntlzYeaRM"

ACCEPTED = [
    # watch?v= with extra parameters in any order
    (f"https://www.youtube.com/watch?v={VIDEO}", VIDEO),
    (f"https://www.youtube.com/watch?v={VIDEO}&t=10s", VIDEO),
    (f"https://www.youtube.com/watch?v={OUT_OF_SAMPLE}&t=377s", OUT_OF_SAMPLE),
    (f"https://www.youtube.com/watch?t=377s&v={OUT_OF_SAMPLE}", OUT_OF_SAMPLE),
    (f"https://www.youtube.com/watch?v={VIDEO}&list=PLabcdefghijk&index=2", VIDEO),
    (f"https://www.youtube.com/watch?feature=share&v={VIDEO}&pp=ygUFaGFiaXQ%3D", VIDEO),
    (f"https://www.youtube.com/watch?v={VIDEO}#comments", VIDEO),
    (f"https://youtube.com/watch?v={VIDEO}", VIDEO),
    (f"http://www.youtube.com/watch?v={VIDEO}", VIDEO),
    (f"https://www.youtube.com:443/watch?v={VIDEO}", VIDEO),
    # short links and path forms
    (f"https://youtu.be/{VIDEO}", VIDEO),
    (f"https://youtu.be/{VIDEO}?si=AbCdEfGh123&t=5", VIDEO),
    (f"https://www.youtube.com/shorts/{VIDEO}", VIDEO),
    (f"https://www.youtube.com/shorts/{VIDEO}/", VIDEO),
    (f"https://www.youtube.com/embed/{VIDEO}?start=10", VIDEO),
    (f"https://www.youtube.com/live/{VIDEO}?feature=share", VIDEO),
    # other hosts
    (f"https://m.youtube.com/watch?v={VIDEO}", VIDEO),
    (f"https://music.youtube.com/watch?v={VIDEO}&feature=share", VIDEO),
    (f"https://www.youtube-nocookie.com/embed/{VIDEO}", VIDEO),
    (f"https://youtube-nocookie.com/embed/{VIDEO}", VIDEO),
    # case, whitespace, no scheme, bare id
    (f"HTTPS://WWW.YOUTUBE.COM/watch?v={VIDEO}", VIDEO),
    (f"  https://www.youtube.com/watch?v={VIDEO}\n", VIDEO),
    (f"www.youtube.com/watch?v={VIDEO}", VIDEO),
    (f"youtu.be/{VIDEO}", VIDEO),
    (VIDEO, VIDEO),
    ("a_B-c0d1E2f", "a_B-c0d1E2f"),
]

REJECTED = [
    "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
    "https://www.youtube.com/@ChillDudeExplains",
    "https://www.youtube.com/@ChillDudeExplains/videos",
    "https://www.youtube.com/c/SomeChannel",
    "https://www.youtube.com/user/SomeUser",
    "https://www.youtube.com/playlist?list=PLabcdefghijk",
    "https://www.youtube.com/",
    "https://www.youtube.com/watch",
    "https://www.youtube.com/watch?v=",
    "https://www.youtube.com/watch?v=short",
    f"https://www.youtube.com/watch?v={VIDEO}X",
    "https://www.youtube.com/watch?v=Lpe%20uC2sQ-w",
    f"https://www.youtube.com/shorts/{VIDEO}/extra",
    f"https://www.youtube.com/feed/{VIDEO}",
    "https://youtu.be/",
    f"https://youtu.be/{VIDEO}/extra",
    f"https://vimeo.com/{VIDEO}",
    f"https://notyoutube.com/watch?v={VIDEO}",
    f"https://youtube.com.evil.example/watch?v={VIDEO}",
    f"https://www.youtube.co/watch?v={VIDEO}",
    "https://en.wikipedia.org/wiki/The_Happiness_Hypothesis",
    f"ftp://www.youtube.com/watch?v={VIDEO}",
    f"javascript:alert('{VIDEO}')",
    "https://[bad-ipv6/watch?v=LpeDuC2sQ-w",
    "",
    "   ",
    VIDEO[:-1],
    f"{VIDEO}!",
    f"{VIDEO}x",
]


@pytest.mark.parametrize(("url", "expected"), ACCEPTED)
def test_accepted_forms_give_the_video_id(url: str, expected: str) -> None:
    assert youtube.parse_video_id(url) == expected


@pytest.mark.parametrize("url", REJECTED)
def test_rejected_forms_raise_a_fetch_error(url: str) -> None:
    with pytest.raises(FetchError) as caught:
        youtube.parse_video_id(url)
    assert caught.value.exit_code == EXIT_FETCH
    assert caught.value.line().startswith("CC_E_FETCH: not a supported YouTube video URL")


def test_case_of_the_id_is_kept() -> None:
    # Ids are case-sensitive; only the host is case-insensitive.
    assert youtube.parse_video_id("https://www.youtube.com/watch?v=lPEdUc2SQ-W") == "lPEdUc2SQ-W"


def test_every_form_of_one_video_gives_one_ref_and_one_packet_name() -> None:
    forms = [url for url, expected in ACCEPTED if expected == VIDEO]
    refs = {youtube.source_ref(youtube.parse_video_id(url)) for url in forms}
    assert refs == {f"youtube:{VIDEO}"}
    assert paths.source_key(f"youtube:{VIDEO}").startswith(f"youtube-{VIDEO}-")


def test_rejection_message_is_one_bounded_line() -> None:
    with pytest.raises(FetchError) as caught:
        youtube.parse_video_id("https://example.com/\n" + "x" * 1000)
    line = caught.value.line()
    assert "\n" not in line
    assert len(line) < 500


def test_watch_url_is_canonical() -> None:
    assert youtube.watch_url(VIDEO) == f"https://www.youtube.com/watch?v={VIDEO}"


def test_start_url_opens_the_video_at_the_tip() -> None:
    ref = youtube.source_ref(VIDEO)
    assert youtube.start_url(ref, 377) == f"https://www.youtube.com/watch?v={VIDEO}&t=377s"
    assert youtube.start_url(ref, 0) == f"{youtube.watch_url(VIDEO)}&t=0s"
    # The URL a fetched id parses back from is the same video.
    assert youtube.parse_video_id(youtube.start_url(ref, 60) or "") == VIDEO


@pytest.mark.parametrize(
    "ref",
    ["manual", "wiki:en:The_Happiness_Hypothesis", "text:sha256:" + "0" * 64, "youtube:short"],
)
def test_start_url_is_none_for_other_refs(ref: str) -> None:
    assert youtube.start_url(ref, 30) is None
