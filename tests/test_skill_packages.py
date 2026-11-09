"""The ``checklist-ingest`` skill package (plan §4.6) and its contract with the CLI.

No code imports the four skill files, so these tests keep them honest:

* the two thin host adapters: exact frontmatter keys, one shared description, the
  adapter skeleton, and every repository path they name resolving;
* the core and the shared contract against the real CLI: every
  ``checklist-care <subcommand>`` and ``--flag`` they mention appears in that
  subcommand's real ``--help`` output (``cli.main`` in-process), every exit number
  and error code exists in ``errors.py``, and every ``fetch`` example single-quotes
  its argument;
* the contract's example proposal, given the Step 3 fixture's ``source_ref`` and
  ``packet_sha256`` and the live ``skill_sha256`` that ``context`` prints (the
  fixture's own ``skill_sha256`` is a placeholder), passes ``proposal.validate()``
  in-process against a database built through ``init`` and ``fetch``;
* the Codex launch line in AGENTS.md ``## Codex usage``;
* no labelled in-sample chapter title (plan Appendix B) anywhere under ``skills/``,
  ``.claude/skills/`` or ``.agents/skills/``: the acceptance eval depends on the
  skill never seeing them.

Every extractor also runs on a known-bad sample, so a check that silently matches
nothing fails instead of passing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from youtube_transcript_api import FetchedTranscript, FetchedTranscriptSnippet

from checklist_care import cli, context, errors, limits, paths, proposal
from checklist_care.db import connection
from checklist_care.sources import youtube

ROOT = context.REPO_ROOT

CORE = "skills/checklist-ingest/core.md"
CONTRACT = "skills/_shared/proposal-contract.md"
CLAUDE_ADAPTER = ".claude/skills/checklist-ingest/SKILL.md"
CODEX_ADAPTER = ".agents/skills/checklist-ingest/SKILL.md"
ADAPTERS = (CLAUDE_ADAPTER, CODEX_ADAPTER)
SKILL_ROOTS = ("skills", ".claude/skills", ".agents/skills")

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PACKET_FIXTURE = FIXTURES / "packet_youtube_synthetic.json"
PROPOSAL_FIXTURE = FIXTURES / "proposal_valid.json"
FIXTURE_FETCHED_AT = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
"""The packet fixture's ``fetched_at``: with it, ``fetch`` rewrites the fixture byte for
byte, so its ``packet_sha256`` is the fixture proposal's."""

LOAD_LINE = (
    "Load `skills/checklist-ingest/core.md` and `skills/_shared/proposal-contract.md` "
    "(repository root) in full before acting."
)
"""The adapters' first instruction (plan §4.6), compared with line breaks collapsed."""

LAUNCH_LINE = (
    'codex --add-dir "<data dir>" --add-dir "$env:LOCALAPPDATA\\uv" '
    "-c sandbox_workspace_write.network_access=true --search"
)
"""The Codex launch line of plan §4.6, which AGENTS.md ``## Codex usage`` records."""

LABELLED_TITLES = (
    "Drink Water Before Coffee",
    "The Three-Second Mirror Check",
    "Sit Up Straight, You're Not Dead Yet",
    "Fire Your Morning Brain",
    "The Accountability Text",
    "Tracing Maps",
    "Your Brain Is A Raisin",
)
"""The operator's labelled example names, plan Appendix B (all verbatim chapter titles).
Step 15 moves the labels into ``eval/labels/``; until then the plan is their only home."""

EVAL_VIDEO_IDS = ("LpeDuC2sQ-w", "RPK-xw3SrVw", "rgntlzYeaRM", "VbGMWtr0AJI")
"""The four evaluation videos (plan Appendix B); the skill's examples use made-up ids."""


def read(relative: str) -> str:
    data = (ROOT / relative).read_bytes()
    return data.decode("utf-8")


def collapsed(text: str) -> str:
    return " ".join(text.split())


def sections(text: str, level: str = "## ") -> dict[str, str]:
    """Each ``level`` heading's title mapped to its body, up to the next such heading.
    Fenced code blocks are skipped when looking for headings."""

    found: dict[str, str] = {}
    title: str | None = None
    body: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith(level):
            if title is not None:
                found[title] = "\n".join(body)
            title, body = line[len(level) :].strip(), []
            assert title not in found, f"heading {title!r} appears twice"
        elif title is not None:
            body.append(line)
    if title is not None:
        found[title] = "\n".join(body)
    return found


# --- frontmatter ---------------------------------------------------------------------


def frontmatter(text: str) -> dict[str, str]:
    """The ``---`` block at the top: one ``key: value`` per line, values as strings."""

    lines = text.split("\n")
    assert lines[0] == "---", "the file must start with a `---` frontmatter line"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, raw = line.partition(":")
        assert separator and key and key == key.strip(), f"not a `key: value` line: {line!r}"
        assert key not in fields, f"frontmatter key {key!r} appears twice"
        fields[key] = scalar(raw.strip())
    return fields


def scalar(raw: str) -> str:
    """A one-line YAML scalar: double-quoted (JSON escapes), single-quoted or plain."""

    if raw.startswith('"'):
        value = json.loads(raw)
        assert isinstance(value, str)
        return value
    if raw.startswith("'"):
        assert len(raw) >= 2 and raw.endswith("'"), f"unclosed quote: {raw!r}"
        return raw[1:-1].replace("''", "'")
    # A plain scalar must stay one plain string in YAML.
    assert raw, "empty value"
    assert ": " not in raw and " #" not in raw, f"quote this value: {raw!r}"
    assert raw[0] not in "[]{}&*!|>%@`", f"quote this value: {raw!r}"
    return raw


def test_the_skill_files_are_the_fingerprinted_four() -> None:
    assert (CORE, CONTRACT, CLAUDE_ADAPTER, CODEX_ADAPTER) == context.SKILL_FILES
    for relative in context.SKILL_FILES:
        assert (ROOT / relative).is_file(), relative


@pytest.mark.parametrize("relative", context.SKILL_FILES)
def test_skill_files_are_utf8_lf_without_bom(relative: str) -> None:
    data = (ROOT / relative).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    data.decode("utf-8")


def test_claude_adapter_frontmatter_keys() -> None:
    fields = frontmatter(read(CLAUDE_ADAPTER))
    assert set(fields) == {"name", "description", "user-invocable", "argument-hint"}
    assert fields["name"] == "checklist-ingest"
    assert fields["user-invocable"] == "true"
    assert fields["argument-hint"].strip()


def test_codex_adapter_frontmatter_keys() -> None:
    fields = frontmatter(read(CODEX_ADAPTER))
    assert set(fields) == {"name", "description"}
    assert fields["name"] == "checklist-ingest"


def test_both_adapters_carry_the_same_description() -> None:
    claude = frontmatter(read(CLAUDE_ADAPTER))["description"]
    codex = frontmatter(read(CODEX_ADAPTER))["description"]
    assert claude == codex
    assert len(claude) > 40  # a real description, not a stub


def test_the_frontmatter_parser_rejects_bad_blocks() -> None:
    with pytest.raises(AssertionError):
        frontmatter("name: no opening line\n")
    with pytest.raises(AssertionError):
        frontmatter("---\nname: a\nname: b\n---\n")
    with pytest.raises(AssertionError):
        frontmatter("---\ndescription: two: colons make YAML a mapping\n---\n")
    assert frontmatter('---\nargument-hint: "<url>"\n---\n') == {"argument-hint": "<url>"}


ADAPTER_SKELETON = {
    CLAUDE_ADAPTER: (
        "# checklist-ingest - Claude Code entry point",
        ("Provider-specific instructions", "Unsupported capabilities", "Output normalization"),
    ),
    CODEX_ADAPTER: (
        "# checklist-ingest - Codex entry point",
        ("Codex invocation differences", "Known Codex limitations", "Output normalization"),
    ),
}


@pytest.mark.parametrize("relative", ADAPTERS)
def test_adapters_follow_the_skeleton_and_stay_thin(relative: str) -> None:
    text = read(relative)
    title, headings = ADAPTER_SKELETON[relative]
    body = text.split("\n---\n", 1)[1]
    assert body.lstrip("\n").split("\n", 1)[0] == title
    assert LOAD_LINE in collapsed(text)
    assert tuple(sections(text)) == headings
    # Thin: the rules and the proposal format live in the core and the contract only.
    assert len(text.splitlines()) <= 80
    assert "```" not in text
    assert proposal.PROPOSAL_SCHEMA not in text


REPO_PATH = re.compile(r"(?<![\w./-])((?:skills|\.claude|\.agents)/[\w./-]*[\w/])")
LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")


def named_paths(text: str) -> list[str]:
    """Repository-root paths written out in the text (``skills/…``, ``.claude/…``)."""

    return REPO_PATH.findall(text)


def test_named_paths_finds_paths() -> None:
    sample = "Load `skills/a/core.md` and .agents/skills/x/SKILL.md, not docs/plan.md."
    assert named_paths(sample) == ["skills/a/core.md", ".agents/skills/x/SKILL.md"]


@pytest.mark.parametrize("relative", ADAPTERS)
def test_every_path_an_adapter_names_exists(relative: str) -> None:
    text = read(relative)
    named = named_paths(text)
    assert CORE in named and CONTRACT in named
    missing = [path for path in named if not (ROOT / path).exists()]
    assert missing == []
    base = (ROOT / relative).parent
    for target in LINK_TARGET.findall(text):
        assert (base / target.split("#", 1)[0]).exists(), f"link {target} does not resolve"


# --- the core and the contract against the real CLI ----------------------------------


COMMAND = re.compile(r"\bchecklist-care\s+(?P<sub>[A-Za-z][\w-]*)(?P<rest>[^`\n]*)")
FLAG = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")


def mentioned_commands(text: str) -> list[tuple[str, list[str]]]:
    """Each ``checklist-care <subcommand> …`` mention with the flags on the same command
    (up to the end of its code span or line)."""

    return [(match["sub"], FLAG.findall(match["rest"])) for match in COMMAND.finditer(text)]


def help_output(capsys: pytest.CaptureFixture[str], subcommand: str) -> tuple[int, str]:
    """``checklist-care <subcommand> --help`` through ``cli.main``: exit code and stdout."""

    capsys.readouterr()
    code = cli.main([subcommand, "--help"])
    return code, capsys.readouterr().out


def in_help(flag: str, help_text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def subcommand_helps(capsys: pytest.CaptureFixture[str], names: Iterable[str]) -> dict[str, str]:
    helps: dict[str, str] = {}
    for name in sorted(set(names)):
        code, out = help_output(capsys, name)
        assert code == 0 and out.startswith(f"usage: checklist-care {name} "), (
            f"`checklist-care {name}` is not a subcommand"
        )
        helps[name] = out
    return helps


def test_command_mentions_are_found_and_unknown_ones_fail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sample = "Run `uv run checklist-care frobnicate --zap 1`, then `checklist-care fetch --nope`."
    assert mentioned_commands(sample) == [("frobnicate", ["--zap"]), ("fetch", ["--nope"])]
    code, _ = help_output(capsys, "frobnicate")
    assert code == 2
    code, out = help_output(capsys, "fetch")
    assert code == 0 and not in_help("--nope", out) and in_help("--data-dir", out)


@pytest.mark.parametrize("relative", context.SKILL_FILES)
def test_every_subcommand_and_its_flags_are_in_its_help(
    relative: str, capsys: pytest.CaptureFixture[str]
) -> None:
    mentions = mentioned_commands(read(relative))
    if relative == CORE:
        assert {"status", "fetch", "context", "propose"} <= {sub for sub, _ in mentions}
    helps = subcommand_helps(capsys, (sub for sub, _ in mentions))
    unknown = [
        (sub, flag) for sub, flags in mentions for flag in flags if not in_help(flag, helps[sub])
    ]
    assert unknown == []


@pytest.mark.parametrize("relative", (CORE, CONTRACT))
def test_every_flag_in_the_core_and_contract_is_a_real_option(
    relative: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Flags in prose count too: each must be in the help of a subcommand the file names."""

    text = read(relative)
    mentioned = {sub for sub, _ in mentioned_commands(text)}
    helps = subcommand_helps(capsys, mentioned)
    flags = set(FLAG.findall(text))
    if relative == CORE:
        assert {"--json", "--source-ref"} <= flags
    stray = sorted(flag for flag in flags if not any(in_help(flag, h) for h in helps.values()))
    assert stray == []


EXIT_MENTION = re.compile(r"\bexit(?:s|ed)?\s+(?:code\s+)?(\d+)\b", re.IGNORECASE)
EXIT_WITH_CODE = re.compile(r"\bexit(?:s|ed)?\s+(\d+)\s+\(`(CC_[A-Z_]+)`")
ERROR_CODE = re.compile(r"\bCC_[A-Z_]+\b")


def exit_numbers() -> set[int]:
    return {
        value
        for name, value in vars(errors).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }


def error_codes() -> set[str]:
    return {
        value
        for name, value in vars(errors).items()
        if name.startswith("CODE_") and isinstance(value, str)
    }


def error_classes() -> list[type[errors.CheckError]]:
    found: list[type[errors.CheckError]] = [errors.CheckError]
    for cls in found:
        found.extend(cls.__subclasses__())
    return found


def test_the_exit_scanners_find_mentions() -> None:
    sample = "If it exits 7, or on exit code 5, or exit 3 (`CC_E_FETCH`), see CC_E_NOPE."
    assert EXIT_MENTION.findall(sample) == ["7", "5", "3"]
    assert EXIT_WITH_CODE.findall(sample) == [("3", "CC_E_FETCH")]
    assert 7 not in exit_numbers() and "CC_E_NOPE" not in error_codes()
    pairs = {(cls.exit_code, cls.code) for cls in error_classes()}
    assert (3, errors.CODE_FETCH) not in pairs and (errors.EXIT_FETCH, errors.CODE_FETCH) in pairs


@pytest.mark.parametrize("relative", (CORE, CONTRACT))
def test_every_exit_code_mentioned_exists_in_errors(relative: str) -> None:
    text = read(relative)
    numbers = {int(number) for number in EXIT_MENTION.findall(text)}
    codes = set(ERROR_CODE.findall(text))
    if relative == CORE:
        assert {2, 3, 4, 5} <= numbers
        assert {errors.CODE_ALREADY_REVIEWED, errors.CODE_FETCH, errors.CODE_PROPOSAL} <= codes
    assert sorted(numbers - exit_numbers()) == []
    assert sorted(codes - error_codes()) == []
    # Where the text pairs a number with a code, errors.py pairs them the same way.
    pairs = {(cls.exit_code, cls.code) for cls in error_classes()}
    for number, code in EXIT_WITH_CODE.findall(text):
        assert (int(number), code) in pairs, f"exit {number} is not {code} in errors.py"


CODE_SPAN = re.compile(r"```[^\n]*\n(?P<block>.*?)```|`(?P<inline>[^`\n]+)`", re.DOTALL)
FETCH_CALL = re.compile(r"(?<![\w-])fetch\b(?P<rest>[^\n`]*)")
TOKEN = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")
SINGLE_QUOTED = re.compile(r"'[^']+'")


def fetch_examples(text: str) -> list[str]:
    """The argument part of every ``fetch`` example: in a code span or block, or after
    ``checklist-care fetch`` anywhere. A bare `fetch` (no arguments) is a mention."""

    rests: list[str] = []
    for span in CODE_SPAN.finditer(text):
        code = span["block"] if span["block"] is not None else span["inline"]
        rests.extend(match["rest"] for match in FETCH_CALL.finditer(code))
    rests.extend(match["rest"] for match in COMMAND.finditer(text) if match["sub"] == "fetch")
    return [rest for rest in rests if rest.strip()]


def unquoted_fetch_examples(text: str) -> list[str]:
    bad: list[str] = []
    for rest in fetch_examples(text):
        arguments = [token for token in TOKEN.findall(rest) if not token.startswith("-")]
        if not arguments or not all(SINGLE_QUOTED.fullmatch(arg) for arg in arguments):
            bad.append(rest.strip())
    return bad


def test_the_fetch_quote_check_flags_unquoted_examples() -> None:
    sample = (
        "Run `uv run checklist-care fetch https://youtu.be/x?a=1&b=2`, or\n"
        '```text\nuv run checklist-care fetch "https://youtu.be/x"\n```\n'
        "but `checklist-care fetch '<url>'` is right, and the `fetch` step is a mention."
    )
    assert fetch_examples(sample).count(" '<url>'") == 2
    assert sorted(unquoted_fetch_examples(sample)) == [
        '"https://youtu.be/x"',
        '"https://youtu.be/x"',
        "https://youtu.be/x?a=1&b=2",
        "https://youtu.be/x?a=1&b=2",
    ]


def test_every_fetch_example_in_the_core_single_quotes_its_argument() -> None:
    text = read(CORE)
    assert fetch_examples(text)  # the core does show how to run fetch
    assert unquoted_fetch_examples(text) == []
    assert unquoted_fetch_examples(read(CONTRACT)) == []


# --- the contract's example proposal --------------------------------------------------


def fixture_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_bytes().decode("utf-8"))
    return payload


class FixtureClient:
    """The transcript seam: the fixture's segments as the library's own objects."""

    def fetch(self, video_id: str, languages: Iterable[str] = ("en",)) -> FetchedTranscript:
        segments = fixture_json(PACKET_FIXTURE)["transcript"]["segments"]
        return FetchedTranscript(
            snippets=[
                FetchedTranscriptSnippet(
                    text=seg["text"], start=seg["start"], duration=seg["duration"]
                )
                for seg in segments
            ],
            video_id=video_id,
            language="English (auto-generated)",
            language_code="en",
            is_generated=True,
        )


def fixture_ytdlp(url: str) -> dict[str, Any]:
    """The yt-dlp seam: the fixture video's title, channel, duration and chapters."""

    packet = fixture_json(PACKET_FIXTURE)
    return {
        "title": packet["title"],
        "channel": packet["channel"],
        "duration": packet["duration_s"],
        "chapters": [
            {
                "title": chapter["title"],
                "start_time": float(chapter["start_s"]),
                "end_time": float(chapter["end_s"]),
            }
            for chapter in packet["chapters"]
        ],
    }


@pytest.fixture
def fetched_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    """A data dir after ``init`` and ``fetch`` of the Step 3 fixture video, run through
    ``cli.main`` with the two network seams faked."""

    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(connection, "utc_now", lambda: FIXTURE_FETCHED_AT)
    monkeypatch.setattr(youtube, "make_transcript_client", FixtureClient)
    monkeypatch.setattr(youtube, "run_ytdlp", fixture_ytdlp)
    target = tmp_path / "data"
    assert cli.main(["--data-dir", str(target), "init"]) == 0
    ref = fixture_json(PROPOSAL_FIXTURE)["source_ref"]
    assert cli.main(["--data-dir", str(target), "fetch", ref.split(":", 1)[1]]) == 0
    capsys.readouterr()
    return target


def contract_example() -> dict[str, Any]:
    """The one ``checklist-care.proposal/1`` JSON block of the shared contract."""

    blocks = re.findall(r"```json\n(.*?)```", read(CONTRACT), re.DOTALL)
    examples = [json.loads(block) for block in blocks if proposal.PROPOSAL_SCHEMA in block]
    assert len(examples) == 1, "the contract must hold exactly one example proposal"
    example: dict[str, Any] = examples[0]
    return example


def test_the_example_proposal_shows_every_field() -> None:
    example = contract_example()
    assert set(example) == set(proposal.ENVELOPE_FIELDS)
    assert example["tips"], "the example shows at least one tip"
    for tip in example["tips"]:
        assert set(tip) == set(proposal.TIP_FIELDS)
    assert example["source_ref"].split(":", 1)[1] not in EVAL_VIDEO_IDS


def test_the_contract_example_proposal_validates(
    fetched_data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = fixture_json(PROPOSAL_FIXTURE)
    code = cli.main(
        ["--data-dir", str(fetched_data_dir), "context", "--source-ref", fixture["source_ref"]]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["source"]["packet_sha256"] == fixture["packet_sha256"]  # coherent fixture
    assert printed["skill_sha256"] == context.skill_sha256()  # the real skill files

    document = contract_example()
    document["source_ref"] = fixture["source_ref"]
    document["packet_sha256"] = fixture["packet_sha256"]
    document["skill_sha256"] = printed["skill_sha256"]
    with closing(connection.connect(fetched_data_dir / paths.DB_FILENAME)) as conn:
        valid = proposal.validate(document, conn, fetched_data_dir)
    assert len(valid.tips) == len(document["tips"])
    assert valid.mode == limits.SOURCE_MODE_EXTRACT


def test_the_example_is_rejected_unchanged(
    fetched_data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red anchor: without the substitutions, validate() refuses the placeholders."""

    with closing(connection.connect(fetched_data_dir / paths.DB_FILENAME)) as conn:
        with pytest.raises(errors.ProposalInvalidError) as rejected:
            proposal.validate(contract_example(), conn, fetched_data_dir)
    fields = {problem.field for problem in rejected.value.problems}
    assert {"source_ref", "packet_sha256", "skill_sha256"} <= fields


# --- the Codex launch line -----------------------------------------------------------


def test_agents_md_codex_usage_holds_the_launch_line() -> None:
    agents = sections(read("AGENTS.md"))
    assert "Codex usage" in agents
    assert LAUNCH_LINE in agents["Codex usage"].split("\n")


def test_the_codex_adapter_states_the_sandbox_needs() -> None:
    text = read(CODEX_ADAPTER)
    for needed in ("--add-dir", "network_access", "--search"):
        assert needed in text
    limitations = collapsed(sections(text)["Known Codex limitations"])
    assert "AGENTS.md `## Codex usage`" in limitations


# --- no labelled in-sample chapter title in the skill --------------------------------


def normalized(text: str) -> str:
    """Casefolded, typographic apostrophes made ASCII, whitespace collapsed."""

    return collapsed(text.replace("’", "'").replace("‘", "'").casefold())


def leaked_titles(text: str, titles: Iterable[str]) -> list[str]:
    haystack = normalized(text)
    return [title for title in titles if normalized(title) in haystack]


def skill_tree_files() -> list[Path]:
    return sorted(
        path for root in SKILL_ROOTS if (ROOT / root).is_dir() for path in (ROOT / root).rglob("*")
    )


def test_the_leak_check_matches_loosely() -> None:
    sample = "Name it fire  your morning\nBRAIN, or Sit Up Straight, You’re Not Dead Yet."
    assert leaked_titles(sample, LABELLED_TITLES) == [
        "Sit Up Straight, You're Not Dead Yet",
        "Fire Your Morning Brain",
    ]


def test_no_labelled_in_sample_title_appears_in_the_skill() -> None:
    files = [path for path in skill_tree_files() if path.is_file()]
    assert {ROOT / relative for relative in context.SKILL_FILES} <= set(files)
    # Appendix B's labelled names, plus every chapter title of the in-sample video
    # (the Step 3 fixture carries the real titles), plus the evaluation video ids.
    titles = [*LABELLED_TITLES, *(c["title"] for c in fixture_json(PACKET_FIXTURE)["chapters"])]
    assert set(LABELLED_TITLES) & set(titles[len(LABELLED_TITLES) :])  # the fixture is in-sample
    leaks = {
        str(path.relative_to(ROOT)): leaked_titles(path.read_text(encoding="utf-8"), titles)
        + [video for video in EVAL_VIDEO_IDS if video in path.read_text(encoding="utf-8")]
        for path in files
    }
    assert {path: found for path, found in leaks.items() if found} == {}
