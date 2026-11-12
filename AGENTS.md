# checklist-care

## Project overview

checklist-care turns life-advice sources into printable room checklists. Its one project skill,
`checklist-ingest`, runs in Claude Code or Codex. It reads a YouTube link, a book's Wikipedia
page, or pasted text; for a Wikipedia page it also investigates the book on the web. It proposes
tips, each with a name, a one-line *what to do* and a plain-language reason, sorted into four
buckets: daily checklist, occasional activity, optional, or left out.

A deterministic CLI validates those proposals and stores them in a local SQLite DB. A local React
UI reviews and approves tips, manages tips, bucket rubrics and lists (with each list's cue), and
prints dense one-line-per-item A4 sheets per room and audience, plus one activities sheet.

Code is public. Data is private, outside the repo, at `%LOCALAPPDATA%\checklist-care`.

The canonical plan is [docs/plans/master_plan.md](docs/plans/master_plan.md). Its
`## 1. What This Is` section states the goal.

## Stack summary

| Layer | Tool |
|---|---|
| Runtime | Python `>=3.12` via uv; argparse CLI `checklist-care` |
| API | FastAPI + uvicorn, single worker, always `127.0.0.1`, daily `--port 8450` |
| DB | stdlib sqlite3, WAL, `schema.sql` versioned by `PRAGMA user_version` |
| Sources | youtube-transcript-api (transcripts), yt-dlp (chapters), Wikipedia Action API (stdlib) |
| UI | React 19.2.8 + Vite 8.2.2 + TypeScript 7.0.2; dev server (:5450 -> :8450); hash routing |
| Tests | pytest (also runs vitest, tsc and Playwright through `tests/test_web_suites.py`); runtime reviewers `--port 8451`; Playwright `--port 8452` |
| Skill | `skills/checklist-ingest/core.md` + `skills/_shared/proposal-contract.md`; adapters `.claude/skills/checklist-ingest/SKILL.md` and `.agents/skills/checklist-ingest/SKILL.md` |

## Commands

Build gates are the commands `/build-step` runs. Shell for build gates: bash (Git Bash on
Windows). Every gate command is valid from build Step 1 onward.

- Dependency install: `if [ -f pyproject.toml ]; then uv sync; fi; if [ -f web/package.json ]; then npm --prefix web install; fi`
- Primary test command, the full DONE gate (it also runs the web suites): `uv run pytest`
- Lint: `uv run ruff check .`
- Format check (a DONE-gate suite): `uv run ruff format --check .`
- Typecheck: `uv run mypy src`

Run and operate (not gates):

```powershell
uv run checklist-care init
uv run checklist-care status
uv run checklist-care serve --build --port 8450
uv run checklist-care backup
uv run checklist-care --data-dir "<eval data dir>" eval score --labels eval/labels/in_sample.json
npm --prefix web run dev
uv run pytest -m live
uv run python scripts/smoke.py --live
npx --yes playwright@1.63.0 install chromium
```

**Using the skill:**

- **Claude Code:** start `claude` inside the checkout, then run
  `/checklist-ingest <youtube-url | wikipedia-url | file path>`, or paste text after
  `/checklist-ingest`.
- **Codex:** start it inside the checkout with the launch line in `## Codex usage` (manual step
  M3 confirms that line). Then ask it to use the checklist-ingest skill on the input.
- **Editing:** change the skill only in `skills/`. The two `SKILL.md` adapters stay thin, and
  `tests/test_skill_packages.py` checks them.

**Build recovery:** if `/build-phase` Step 6 (the first runtime-review round) ends with a
readiness timeout, run `npm --prefix web install` once in this checkout, then
`/build-phase --resume 6`.

**Restoring a backup:** stop the server, then copy
`%LOCALAPPDATA%\checklist-care\backups\<file>.db` over
`%LOCALAPPDATA%\checklist-care\checklist-care.db`.

## Directory layout

```text
checklist-care/
  AGENTS.md  CLAUDE.md (@AGENTS.md pointer)  README.md  LICENSE
  pyproject.toml  uv.lock  .gitignore  .gitattributes
  docs/plans/master_plan.md     # canonical plan with ### Step N: blocks
  src/checklist_care/           # cli, limits, paths, context, proposal, placement, printing, processes, errors, strict_json, db/, sources/, store/, server/, scoring/
  web/                          # React app (dist/ and node_modules/ gitignored); e2e/ Playwright
  skills/                       # canonical skill core + shared proposal contract
  .claude/skills/               # Claude adapter (thin)
  .agents/skills/               # Codex adapter (thin)
  scripts/smoke.py              # end-to-end smoke gate (--offline / --live)
  eval/                         # labels/ (ids, chapter titles, expectations), anchors/ (self-written)
  tests/                        # unit/, integration/, fixtures/ (synthetic only), test_skill_packages.py, test_web_suites.py
```

## Architecture summary

**Skill.** The skill runs `checklist-care fetch` to get a source packet, then
`checklist-care context` for the limits, bucket rubrics, list cues, existing tips and the packet
hash. It writes a proposal JSON and submits it with `checklist-care propose`. It never writes the
DB directly, and never reads `eval/`, `docs/` or `tests/`.

**CLI and core.**

- `fetch` resolves the source ref, refuses already-reviewed sources (exit 3), and writes the
  packet under `<data-dir>/packets/`.
- `propose` validates every field against `limits.py` and the placement rules, then imports the
  tips as `pending` in one transaction. Any error exits 5 and writes nothing. An empty proposal
  is valid.
- `limits.py` is the single source of every text limit and enum.

**Data.** SQLite at `<data-dir>/checklist-care.db`, with the tables `bucket`, `source`, `tip`,
`list` and `list_item`.

- Triggers back up the placement rules: list items only for bucket-1 tips, and kids lists only
  for kids+ tips.
- The data dir comes from `--data-dir`, then `CHECKLIST_CARE_DATA_DIR`, then
  `%LOCALAPPDATA%\checklist-care`.
- A data dir inside a git checkout is refused (`CC_E_DATA_IN_REPO`).
- Every command prints `data_dir: <path>` as its first stderr line.

**Server and UI.**

- `checklist-care serve` always binds `127.0.0.1` and serves `/api/*` plus the built React app.
- Pages: Review inbox (Approve / Left out), Library (including "Add my own tip"), Lists (CRUD,
  cues, order, unplaced panel), Buckets (rubrics), and the A4 print views `#/print/list/:id` and
  `#/print/activities`.
- Only approved tips print.

**Acceptance.** `checklist-care eval score --labels <file>` grades the DB rows the skill stored
(never a transcript) against a committed labels file in `eval/labels/`. It exits 0 on PASS (or
`REPORTED (degraded)`), 1 when a hard criterion fails, and 6 (`CC_EVAL_ABORT`) when the input
cannot give a verdict. It needs `--data-dir` or `CHECKLIST_CARE_DATA_DIR`.

## Current state

Phase 1 complete — the v1 build (Steps 1–16): ingest from YouTube, pasted text and text files,
and Wikipedia (with book investigation); the review inbox, library, rubric and list editing; A4
print sheets; and `eval score`. The full gate is green (1535 tests). Operator acceptance is open:
the manual steps M1–M6 in the plan, starting with M1.

## Environment requirements

- Windows 11 is the primary platform, with paths defaulting to `%LOCALAPPDATA%`. Operator
  commands are written for PowerShell 5.1.
- Python 3.12+ through uv (3.14.3 installed); Node `>=24.14.0 <25` and npm `>=11 <12`.
- Network access to youtube.com and wikipedia.org for ingest. No API keys or secrets.
- Claude Code and/or Codex, signed in. Wikipedia investigation needs the host's web search:
  Codex needs `--search`, and without it the skill runs degraded.
- Playwright Chromium for the print tests, installed once:
  `npx --yes playwright@1.63.0 install chromium`.
- Private data stays outside the repo. Never commit packets, transcripts, DB files or backups.

## Codex usage

Codex finds `checklist-ingest` in `.agents/skills` with no install step. Its default
`workspace-write` sandbox blocks network access and writes outside the checkout, and the skill
needs both. Start Codex inside the checkout with this launch line (PowerShell):

```powershell
codex --add-dir "<data dir>" --add-dir "$env:LOCALAPPDATA\uv" -c sandbox_workspace_write.network_access=true --search
```

- `<data dir>` is the private data directory: the `data_dir` that `uv run checklist-care status`
  prints. It is `$env:CHECKLIST_CARE_DATA_DIR` when that is set, else
  `$env:LOCALAPPDATA\checklist-care`. The skill writes its draft proposals there, and the CLI
  writes the packets and the DB.
- `--add-dir "$env:LOCALAPPDATA\uv"` lets every `uv run` write uv's cache.
- `-c sandbox_workspace_write.network_access=true` lets `checklist-care fetch` reach YouTube and
  Wikipedia.
- `--search` turns on live web search, which the Wikipedia book investigation uses. Without it,
  that investigation runs degraded.

With the default data directory the line is:

```powershell
codex --add-dir "$env:LOCALAPPDATA\checklist-care" --add-dir "$env:LOCALAPPDATA\uv" -c sandbox_workspace_write.network_access=true --search
```

Then ask: "Use the checklist-ingest skill on <youtube-url>". Start a fresh Codex session for each
source, so duplicates are found through `checklist-care context`, not through chat memory.

`--add-dir`, `-c` and `--search` are in codex-cli 0.147.0's `--help` (checked 2026-11-08). The
`sandbox_workspace_write.network_access` key is not verified yet: manual step M3 confirms it,
seen as no approval prompt for the network or for writes to the data dir. If M3 finds that a
different line is needed, record the working line here.
