# checklist-care

Turn life-advice sources into short, printable habit checklists for the rooms of a home.

You give it a YouTube link, a book's Wikipedia page, or pasted text. One project skill,
`checklist-ingest`, runs in **Claude Code** or **OpenAI Codex** and splits the source into tips.
Each tip has a catchy name, a one-line *what to do* and a plain-language reason. The skill
proposes a bucket for each tip: daily checklist, occasional activity, optional, or left out. It
also proposes an audience (kids+ or adults) and the room lists the tip belongs on. For a
Wikipedia page it also investigates the book on the web, because the article alone doesn't
contain the tips.

A deterministic CLI does all fetching, validation and storage in a local SQLite database. A local
React app is the review inbox and workbench. There you approve or edit what the skill proposed,
manage tips, bucket rubrics and lists, add your own basics, and print dense one-line-per-item A4
sheets. There is one sheet per room and audience (for example "Coffee Machine — Adults"), plus one
"Good to do now and then" activities sheet.

**Phase 1 complete** — issues #1–16 closed. The whole v1 plan is built: YouTube, pasted-text and
Wikipedia ingest (with book investigation), the review inbox, library, rubric and list editing, A4
print sheets, and the `eval score` acceptance scorer. 1535 tests passing, 0 type errors, 0 lint
violations. Operator acceptance (manual steps M1–M6 in
[docs/plans/master_plan.md](docs/plans/master_plan.md)) is still open.

## Public code, private data

The code in this repository is public. Your data is not in it:

- The database, fetched transcripts and pages, drafts and backups live in
  `%LOCALAPPDATA%\checklist-care`. Override that with `--data-dir` or `CHECKLIST_CARE_DATA_DIR`.
- Every command refuses a data directory inside a git checkout (`CC_E_DATA_IN_REPO`).
- `.gitignore` blocks database files, and test fixtures are synthetic.
- The server only listens on `127.0.0.1`; there is no `--host` option and no login.

## Stack

| Layer | Tool | Why |
|---|---|---|
| Runtime and CLI | Python `>=3.12` with uv; argparse CLI `checklist-care` | About ten subcommands need no CLI library |
| API server | FastAPI + uvicorn, one worker, `127.0.0.1` only | SQLite has one writer, so one worker; loopback by construction |
| Database | stdlib `sqlite3`, WAL, one `schema.sql` versioned by `PRAGMA user_version` | Local, single file, easy to back up |
| YouTube | `youtube-transcript-api` (transcripts) and `yt-dlp` (chapters) | Chapter titles become tip names and timing windows |
| Wikipedia | MediaWiki Action API via stdlib `urllib` | One title per request, with a project User-Agent |
| Frontend | React 19, Vite 8, TypeScript 7; hash routing | Hash URLs need no server fallback |
| Tests | pytest (also runs vitest, the TypeScript check and Playwright print tests), ruff, mypy | One declared test command covers every suite |
| Skill | `skills/checklist-ingest/core.md` plus thin Claude and Codex adapters | One canonical core; adapters stay thin and are checked by tests |

## Prerequisites

- Windows 11 (primary platform); PowerShell for the commands below.
- Python 3.12+ through [uv](https://docs.astral.sh/uv/).
- Node `>=24.14.0 <25` and npm `>=11 <12`.
- Claude Code and/or Codex, signed in. The Wikipedia investigation uses the host's web search;
  Codex needs `--search`, and without it the skill runs in degraded mode.
- Network access to youtube.com and wikipedia.org. No API keys or secrets.

## Setup and first run

1. Clone and install:

   ```powershell
   git clone https://github.com/aberson/checklist-care
   cd checklist-care
   uv sync
   npm --prefix web install
   ```

2. Create your private database and start the app at `http://127.0.0.1:8450`:

   ```powershell
   uv run checklist-care init
   uv run checklist-care serve --build
   ```

3. In a second window, start a host inside the checkout and ingest a source:
   - **Claude Code:** run `claude`, then `/checklist-ingest https://www.youtube.com/watch?v=LpeDuC2sQ-w`.
   - **Codex:** start it with the launch line in AGENTS.md `## Codex usage`, then ask it to use the
     checklist-ingest skill on the URL.

4. Review at `http://127.0.0.1:8450/#/review`, approve the tips, then print from **Lists**. Print
   on A4 at 100% scale, with browser headers and footers turned off.

5. Back up any time with `uv run checklist-care backup`. To restore, stop the server and copy the
   backup `.db` file back over `checklist-care.db` in the data directory.

## Key design decisions

- **The skill judges; the CLI does I/O.** The model's only output is a proposal file. Fetching,
  validation and every database write are tested code, so a bad answer is rejected field by field.
- **Nothing prints until you approve it.** Proposals land as pending; print views read only
  approved tips.
- **Chapter titles are names; the action line carries the meaning.** A printed row is
  `☐ Name — what to do`, at most 80 characters, about 48 rows per A4 page at 12pt.
- **Placement by cue.** A daily tip goes on the list for where you are when the action is due.
  Each list's cue and each bucket's rubric are editable, and the skill reads them on every ingest.
- **Duplicates.** The same action at the same cue is a duplicate; the earlier tip wins and the
  later one is left out. Near matches are flagged for you to decide.
- **Acceptance is measured.** `checklist-care eval score` grades what the skill stored in the
  database against labelled examples, on both hosts, and aborts rather than grading doubtful input.

## Project structure

```text
checklist-care/
  AGENTS.md  CLAUDE.md          # instructions for coding agents (CLAUDE.md points at AGENTS.md)
  docs/plans/master_plan.md     # the plan: design, contracts and build steps
  src/checklist_care/           # CLI, limits, data dir, DB, sources, proposal validation, server, scoring
  web/                          # React app and Playwright print tests
  skills/                       # canonical skill core + shared proposal contract
  .claude/skills/  .agents/skills/   # thin Claude Code and Codex adapters
  scripts/smoke.py              # end-to-end smoke gate
  eval/                         # labels and the garbage anchor for acceptance scoring
  tests/                        # unit, integration and skill-package tests
```

## Development

Gate commands (run from the checkout):

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

`uv run pytest` is the full gate; it also runs the web type check, vitest and the Playwright print
tests. Live network checks are separate: `uv run pytest -m live`, and the end-to-end smoke gate
`uv run python scripts/smoke.py --live`.

## License

MIT — see [LICENSE](LICENSE).
