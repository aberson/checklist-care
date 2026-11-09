"""The yt-dlp runner and metadata reader behind ``run_ytdlp`` (plan §4.3).

``run_json_command`` is exercised with small Python child processes, never the
network. The timeout test spawns a grandchild that holds the output pipe open,
so it only finishes quickly when the whole process tree is killed.
"""

from __future__ import annotations

import sys
import time

import pytest

from checklist_care import USER_AGENT
from checklist_care.sources import youtube
from checklist_care.sources.packet import Chapter, Segment


def test_ytdlp_command_is_the_plan_command_line() -> None:
    url = "https://www.youtube.com/watch?v=LpeDuC2sQ-w"
    assert youtube.ytdlp_command(url) == [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--user-agent",
        USER_AGENT,
        url,
    ]
    assert youtube.YTDLP_TIMEOUT_S == 60


def test_run_ytdlp_passes_the_command_and_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(command: list[str], *, timeout: float) -> dict[str, object]:
        seen.update(command=command, timeout=timeout)
        return {"id": "LpeDuC2sQ-w"}

    monkeypatch.setattr(youtube, "run_json_command", fake)

    assert youtube.run_ytdlp("https://youtu.be/x") == {"id": "LpeDuC2sQ-w"}
    assert seen == {"command": youtube.ytdlp_command("https://youtu.be/x"), "timeout": 60}


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_run_json_command_parses_utf8_json() -> None:
    code = (
        "import json, sys; "
        "sys.stdout.write(json.dumps({'title': 'Caf\\u00e9 \\U0001f600'}, ensure_ascii=False))"
    )
    assert youtube.run_json_command(_python(code), timeout=60) == {"title": "Café \U0001f600"}


def test_run_json_command_nonzero_exit_names_the_last_stderr_line() -> None:
    code = "import sys; sys.stderr.write('first\\nERROR: Video unavailable\\n'); sys.exit(1)"
    with pytest.raises(youtube.YtDlpError, match=r"^exit 1: ERROR: Video unavailable$"):
        youtube.run_json_command(_python(code), timeout=60)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("print('not json')", "output is not JSON: JSONDecodeError: "),
        ("print('[1, 2]')", "output is not a JSON object"),
        # Not JSONDecodeError: RecursionError, and ValueError for an int over 4300 digits.
        (
            "import sys; sys.stdout.write('[' * 100000 + ']' * 100000)",
            "output is not JSON: RecursionError: ",
        ),
        ("print('{\"duration\": ' + '9' * 5000 + '}')", "output is not JSON: ValueError: "),
        ("import sys; sys.stdout.buffer.write(b'\\xff\\xfe')", "output is not UTF-8"),
    ],
)
def test_run_json_command_rejects_bad_output(code: str, message: str) -> None:
    with pytest.raises(youtube.YtDlpError, match=message):
        youtube.run_json_command(_python(code), timeout=60)


def test_run_json_command_missing_program_is_a_ytdlp_error() -> None:
    with pytest.raises(youtube.YtDlpError, match="cannot start yt-dlp"):
        youtube.run_json_command(["checklist-care-no-such-program-xyz"], timeout=60)


def test_timeout_kills_the_whole_process_tree() -> None:
    # The child hands its stdout pipe to a grandchild that sleeps for a minute.
    # Killing only the child would leave the pipe open until the grandchild exits,
    # and communicate() would then wait the full KILL_WAIT_S before giving up
    # (checked by hand: child-only kill took 13 s here, the tree kill 3.3 s).
    grandchild = "import time; time.sleep(60)"
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], "
        "stdout=sys.stdout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)); "
        "print('started', flush=True); time.sleep(60)"
    )
    timeout = 3.0
    started = time.monotonic()
    with pytest.raises(youtube.YtDlpError, match="no result within 3 s"):
        youtube.run_json_command(_python(child), timeout=timeout)
    elapsed = time.monotonic() - started
    assert elapsed < timeout + youtube.KILL_WAIT_S - 2, elapsed


# --- metadata --------------------------------------------------------------------------


def test_read_metadata_floors_chapters_and_rounds_duration_up() -> None:
    info = {
        "title": "Tiny Changes",
        "channel": "Some Channel",
        "duration": 562.4,
        "chapters": [
            {"title": "Intro", "start_time": 0.0, "end_time": 59.9},
            {"title": "Second", "start_time": 59.9, "end_time": 562.4},
        ],
    }
    metadata = youtube.read_metadata(info)
    assert metadata.title == "Tiny Changes"
    assert metadata.channel == "Some Channel"
    assert metadata.duration_s == 563
    assert metadata.chapters == (Chapter("Intro", 0, 59), Chapter("Second", 59, 562))
    assert metadata.warnings == ()


def test_read_metadata_keeps_titles_byte_for_byte() -> None:
    title = "Sit Up Straight, You're Not Dead Yet "
    metadata = youtube.read_metadata(
        {"duration": 10, "chapters": [{"title": title, "start_time": 0, "end_time": 10}]}
    )
    assert metadata.chapters[0].title == title
    assert metadata.duration_s == 10


def test_lone_surrogates_in_metadata_text_become_replacement_characters() -> None:
    metadata = youtube.read_metadata(
        {
            "title": "a\ud800b \U0001f600",
            "channel": "\udfff",
            "duration": 10,
            "chapters": [{"title": "x\ud83d", "start_time": 0, "end_time": 10}],
        }
    )
    assert metadata.title == "a\ufffdb \U0001f600"  # a real astral character survives
    assert metadata.channel == "\ufffd"
    assert metadata.chapters == (Chapter("x\ufffd", 0, 10),)
    assert metadata.warnings == ()


@pytest.mark.parametrize("chapters", [None, []])
def test_no_chapters_is_a_warning(chapters: object) -> None:
    metadata = youtube.read_metadata({"duration": 100, "chapters": chapters})
    assert metadata.chapters == ()
    assert metadata.warnings == ("the video has no chapters; split the transcript by content",)


@pytest.mark.parametrize(
    "chapters",
    [
        "not a list",
        [{"title": "A", "start_time": 0, "end_time": 5}, "not an object"],
        [{"title": "", "start_time": 0, "end_time": 5}],
        [{"title": "A", "start_time": None, "end_time": 5}],
        [{"title": "A", "start_time": 0, "end_time": True}],
        [{"title": "A", "start_time": 9, "end_time": 5}],
        [{"title": "A", "start_time": float("nan"), "end_time": 5}],
        [{"title": "A", "start_time": 0, "end_time": 10**400}],
    ],
)
def test_malformed_chapters_are_all_dropped_with_a_warning(chapters: object) -> None:
    metadata = youtube.read_metadata({"duration": 100, "chapters": chapters})
    assert metadata.chapters == ()
    assert len(metadata.warnings) == 1
    assert metadata.warnings[0].startswith("yt-dlp gave malformed chapters")


@pytest.mark.parametrize("duration", [None, 0, -5, "563", True, float("inf"), 10**400])
def test_unusable_duration_is_none(duration: object) -> None:
    assert youtube.read_metadata({"duration": duration}).duration_s is None


def test_transcript_duration_is_the_ceiling_of_the_last_segment_end() -> None:
    segments = [Segment(0.0, 2.5, "a"), Segment(550.25, 12.5, "b")]
    assert youtube.transcript_duration(segments) == 563
    assert youtube.transcript_duration([Segment(10.0, 5.0, "a")]) == 15
    assert youtube.transcript_duration([]) is None
