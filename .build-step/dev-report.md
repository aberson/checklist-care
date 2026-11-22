# Step 1 dev report: package, private data directory and database

Verdict: DONE (round 3). Every new file is untracked in the worktree (18 files, 2583 lines
including `uv.lock`). Nothing was committed or staged.

## Round 3: review findings addressed

| Finding | Change |
|---|---|
| N4 | `limits.py` now defines each enum member by name (`AUDIENCE_KIDS_PLUS/KIDS/ADULTS`, `BUCKET_DAILY/OCCASIONAL/OPTIONAL/LEFT_OUT`, `TIP_STATUS_PENDING/APPROVED`, `SOURCE_KIND_*`, `SOURCE_MODE_*`), and builds every enum tuple from those members. Values and order are unchanged. `seed.py` imports the members for list audiences, bucket ids, and the manual source's kind and mode. `MANUAL_SOURCE_KIND` and `MANUAL_SOURCE_MODE` are gone. `MANUAL_SOURCE_REF = "manual"` stays: it is a §3.1 source ref, not an enum value, and a comment now says so. A sweep of the other production modules found one more restatement, `cli.py`'s `WHERE status = 'pending'`, which now binds `TIP_STATUS_PENDING` as a parameter. The JSON field name `counts.pending` is a payload key and stays |
| N4 guard | New `test_production_module_names_enum_members_instead_of_literals` in `tests/unit/test_limits.py`, parametrized over every production `.py` except `limits.py` (7 cases). It reads each module's AST and fails on a string literal that equals an enum value or holds one SQL-quoted (`'pending'`). Payload keys (dict keys and subscripts) and the `MANUAL_SOURCE_REF` value are exempt. `is` cannot do this job: CPython interns the short strings, so a restated `"kids"` is the same object. `test_restated_enum_literal_detector_flags_literals` is a red anchor that the detector catches both forbidden shapes and spares the exempt ones |
| N5 | Deleted `test_list_item_for_a_bucket_2_tip_raises`. Its count assertion moved into `test_list_item_for_any_non_daily_tip_raises`, now with ids `bucket-2` / `bucket-3` / `bucket-4`. Criterion 8 maps to `test_list_item_for_any_non_daily_tip_raises[bucket-2]`, which also carries a comment |
| N6 | Deleted the `USER_AGENT ==` f-string equality. The test now asserts the literal `PROJECT_URL == "https://github.com/aberson/checklist-care"`, and keeps the `PROJECT_URL` regex and the no-`@` check |
| N7 | `test_limit_values_match_the_plan` keeps exact values for all seven numeric limits. Each is a §4.2 table contract: name, action, line and reason caps, print capacity, and proposal caps, which also appear in the §4.5 context payload. No other test in this step fixes them, and a one-line docstring says so. Deleted `test_enum_values_match_the_plan`, since the drift tests already pin each enum against schema.sql. Those drift tests compare tuple to tuple (a list would not compare equal), so the immutability guarantee that round 1 leaned on still holds. Also removed the two `seed.MANUAL_SOURCE_*` lines from `test_seeds_use_only_enum_values`, because those attributes no longer exist and the manual source's kind and mode are asserted against the DB in `test_init_creates_the_seeded_database`. The drift tests were not touched |

**Advisory findings:**
- **F8 (fixed, small and in scope):** the zero-byte DB case was raised in both rounds.
  `cli._has_database()` now treats a zero-byte file as no database. status, `status --json` and
  backup exit 2 with `CC_E_CONFIG: no database at …` and leave the file empty. init on a
  zero-byte file seeds it and reports `result: created`. Four new tests cover this.
- **F9 (fixed):** `PRAGMAS` now runs `busy_timeout=5000` first, before `journal_mode=WAL`, with
  a comment. The pragma-value test still passes.
- **F10 (no change):** the alternative is a non-strict stdout error handler, which would silently
  change JSON bytes. That is a design decision, not a trivial fix. Lone-surrogate paths are
  pathological on Windows.
- **F11 (no change):** a symlink check on `backups/` and the `.db` adds new behavior, and the
  attack needs local control of the private data dir.

**Mutation checks this round** (each on the live file, then restored; `cmp` confirmed identical):

| Mutation | Result |
|---|---|
| `seed.py` list 2 audience back to the literal `"adults"` | guard `[db/seed.py]` fails: `line 82: 'adults'` |
| `cli.py` pending count back to `status = 'pending'` | guard `[cli.py]` fails: `line 152: …'pending'` |
| `_has_database` back to `is_file()` only | the 4 zero-byte tests fail |

**Process note.** `ruff format` was once run in write mode, not check mode, on `seed.py` and
`limits.py`, both files touched in this round. It re-wrapped the two longest `ListSeed(...)`
calls. Every later format run was `--check`.

## Gate (bash, round 3, after `unset VIRTUAL_ENV`)

| Command | Result |
|---|---|
| `uv run mypy src` | Success: no issues found in 8 source files |
| `uv run ruff check .` | All checks passed! |
| `uv run ruff format --check .` | 17 files already formatted |
| `uv run pytest` | 128 passed, 1 skipped in 2.72s |

118 in round 2, minus 2 deleted (the folded bucket-2 test and the enum restatement test), plus
7 guard cases, 1 detector anchor and 4 zero-byte tests, gives 128. Logs are in
`.build-step/gate-*.log`. The one skip is unchanged: symlink creation on Windows.

---

## Round 2: review findings addressed

Only two test files changed. No production code changed.

| Finding | Change |
|---|---|
| N1 | Deleted `test_line_max_is_tighter_than_name_plus_action` from `tests/unit/test_limits.py` |
| N2 | Deleted the parametrized `test_enums_are_immutable_tuples` (6 cases) from `tests/unit/test_limits.py`. `test_enum_values_match_the_plan` already pins each enum by exact tuple equality |
| N3 | `tests/unit/test_paths.py` no longer re-derives the digest. The `_h8` helper and the `hashlib` import are gone. Every `source_key` assertion now compares against a hard-coded literal key, e.g. `youtube-LpeDuC2sQ-w-64034214` and `wiki-en_a_b_c_d_e-f4f4daf8`. Each `<h8>` was computed once as `sha256(ref as UTF-8)[:8]` outside production, then checked against production's output. A non-ASCII example (`wiki:en:Ünïcödé_Tïtle` -> `wiki-en__n_c_d__T_tle-ff7f8b67`) was added, so a change of encoding also fails |

**N3 red anchor.** Production's `ref.encode("utf-8", ...)` was changed to `"utf-16-le"` on the
live file, and `pytest tests/unit/test_paths.py -k source_key` was run: 6 failed, 9 passed. The
file was then restored from a backup, and `cmp` confirmed it was identical.

**Advisory findings F1-F7: no change.** Each one would add behavior, or is marked "keep" or "no
action":
- F1: a cross-process race on the backup name. The in-process same-second case is already
  covered by the `-N` suffix and its test. Atomic create plus cleanup of a partial file would be
  new behavior.
- F2: ROLLBACK when COMMIT raises. Current callers close the connection.
- F3: the 32-bit `h8` is the plan's §3.1 contract.
- F4: a zero-byte DB file passes `_require_database`.
- F5: the git refusal belongs in `connect()` for the future web server. That is out of this
  step's scope.
- F6 and F7: keep, or no action.

## Gate (bash, round 2, after `unset VIRTUAL_ENV`)

| Command | Result |
|---|---|
| `uv run mypy src` | Success: no issues found in 8 source files |
| `uv run ruff check .` | All checks passed! |
| `uv run ruff format --check .` | 17 files already formatted |
| `uv run pytest` | 118 passed, 1 skipped in 3.05s |

124 in round 1 - 7 deleted (1 for N1, 6 for N2) + 1 new non-ASCII example = 118. Logs are in
`.build-step/gate-*.log`.

---

## Round 1 report (kept for reference)

## Gate (round 1, bash, after a bare `uv sync`)

| Command | Result |
|---|---|
| `uv run mypy src` | Success: no issues found in 8 source files |
| `uv run ruff check .` | All checks passed! |
| `uv run ruff format --check .` | 16 files already formatted |
| `uv run pytest` | 124 passed, 1 skipped in 2.45s (round 1; see the round 2 gate above) |
| `uv run pytest -m live` | exit 5, 125 deselected. No live tests exist yet; they start at Step 2. The marker is registered and `--strict-markers` is on |

- The one skip is `test_symlink_into_a_checkout_is_refused`. Creating a symlink on Windows needs
  Developer Mode or admin rights.
- uv chose CPython 3.13.12 for `.venv`. The suite was also run on 3.14 and 3.12 in scratch venvs
  (`UV_PROJECT_ENVIRONMENT`, `--frozen`, `-W error`): both gave 124 passed, 1 skipped, with no
  ResourceWarnings.
- `uv build --wheel` (into the scratchpad) packs `checklist_care/db/schema.sql`, the LICENSE and
  the `checklist-care` entry point.

## Files (18 new, 2468 lines)

All 16 files in **Produces** are present. Two more were needed:

- `src/checklist_care/db/__init__.py`: makes `db` a regular package, so that
  `importlib.resources` and mypy work.
- `tests/unit/test_errors.py`: tests the Appendix C codes and exit numbers in `errors.py`.

## Done-when mapping

1. **init creates the seeded DB.** `test_init_creates_the_seeded_database` checks:
   - the 4 buckets, with titles and rubrics;
   - the 7 lists, matching `(id, place, audience, cue)` exactly as in §3.4;
   - the manual source (`kind`, `ref`, `mode` = manual; `title` = `My own tips`; `reviewed_at`
     NULL);
   - `user_version` 1, and all 5 subdirectories.
2. **Running init twice changes nothing.** `test_init_twice_changes_nothing` compares
   `iterdump()` before and after. The clock is moved forward a year between runs, which proves
   `updated_at` is not rewritten. `test_init_keeps_operator_edits` shows that edited cues and
   rubrics survive a re-init.
3. **Both `--data-dir` positions work.** `test_data_dir_works_before_or_after_the_subcommand` is
   parametrized over init, status, `status --json` and backup, each before and after the
   subcommand. The env var, the flag-over-env order and the default (patched to tmp) each have
   their own test.
4. **status --json shape.** `test_status_json_prints_the_documented_shape` asserts the whole
   payload with exact equality:
   `{data_dir, db_path, schema_version, counts:{sources, tips_by_bucket{"1".."4"}, pending, lists}}`.
5. **Backup.** `test_backup_writes_a_db_with_the_same_rows`:
   - writes `backups/checklist-care-YYYYMMDD-HHMMSS.db`;
   - its `iterdump()` equals the live DB's, including an emoji tip and a placement;
   - it opens through `connect()` with `user_version` 1.

   `test_two_backups_in_the_same_second_both_survive` covers two backups in one second (the
   second gets a `-2` suffix).
6. **A data dir inside a git checkout exits 2.**
   - `test_data_dir_inside_a_git_checkout_exits_2` covers init, status and backup, with `.git` as
     a directory and as a file. Each exits 2, `err[1]` starts with `CC_E_DATA_IN_REPO:`, and
     nothing is created inside the checkout.
   - `test_data_dir_inside_this_checkout_via_env_exits_2` uses this worktree's real `.git` file.
7. **source_key.** In `tests/unit/test_paths.py`:
   - the §3.1 examples hold;
   - `: ? / " * < > | \`, whitespace and non-ASCII each become `_`;
   - case is kept, and keys stay unique when compared with `casefold()`;
   - refs that sanitize or truncate to the same body still get unique keys. They are also written
     as files on the real (case-insensitive NTFS) filesystem, and the file count matches.
8. **Triggers.**
   - `test_list_item_for_a_bucket_2_tip_raises`, plus the same check for buckets 2, 3 and 4.
   - `test_adults_tip_on_a_kids_list_raises`.
   - Each update path raises too: moving or repointing a list_item, a placed tip leaving bucket 1,
     a tip on a kids list becoming adults, and a list holding an adults tip becoming kids.
   - Positive controls pass: compatible placements, a reorder, and delete-then-update inside one
     transaction.
9. **Emoji subprocess test.** `test_status_json_round_trips_an_emoji_with_pythonioencoding_unset`
   runs `python -m checklist_care.cli` init, then `status --json`, on a data dir named
   `data-😀-ü`.
   - The child env drops `PYTHONIOENCODING`, `PYTHONUTF8` and `PYTHONLEGACYWINDOWSSTDIO`.
   - It asserts the raw UTF-8 emoji bytes are in stdout, that the JSON parses, and that the first
     stderr line is `data_dir: …`.
   - The console script `checklist-care` gets the same round-trip test.
   - Two variants force `PYTHONIOENCODING=ascii` and `cp1252`, and must also round-trip.
   - A red anchor shows that a plain `print(emoji)` fails under those encodings.
10. **The full Python gate is green** (see above).

## Mutation checks (each run on the live files, then restored; `cmp` against backups confirmed)

| Mutation | Result |
|---|---|
| Remove the stdout UTF-8 reconfigure | Emoji tests fail. The unset-env test fails with `UnicodeEncodeError: 'charmap'`, so it is not vacuous |
| `json.dumps` without `ensure_ascii=False` | Emoji test fails |
| Disable the bucket-1 insert trigger | Bucket-2 trigger test fails |
| Delete the kids-list insert trigger | Adults-on-kids test fails |
| Disable the `.git` marker check | Git refusal test fails |
| Drop the hash from `source_key` | Uniqueness test fails |
| Add an enum value to the schema CHECK | `test_limits` drift guard fails |
| `--data-dir` default `None` instead of `SUPPRESS` | Before/after position test fails |

## Design notes and deliberate choices (for reviewers)

**`cli.main(argv) -> int` returns the exit code.**
- It catches argparse's `SystemExit`, so in-process tests get the 2 for a usage error or the 0
  for `--version`.
- The module ends with `if __name__ == "__main__": sys.exit(main())`, not a bare `main()`. A bare
  call would make `python -m checklist_care.cli` always exit 0.
- The console script wrapper already calls `sys.exit(main())`.

**stderr order.**
- The data dir is resolved, then `data_dir: <path>` is printed, then the git refusal and the
  command run. A refused or failing command therefore still shows which directory it saw first.
- An empty `CHECKLIST_CARE_DATA_DIR` fails during resolution, so its `CC_E_CONFIG` line is the
  first stderr line.
- Argparse usage errors (exit 2) and `--help` / `--version` print no `data_dir:` line.

**status and backup without a DB** exit 2 with `CC_E_CONFIG: no database at …; run
`checklist-care init` first`, and create nothing. A typo'd data dir therefore never yields a
silently empty, unseeded DB.

**`connect()`:**
- Opens in autocommit mode (`isolation_level=None`). Writes go through
  `transaction()` = `BEGIN IMMEDIATE` … COMMIT/ROLLBACK, per §3.6.
- `schema.sql` is split with `sqlite3.complete_statement`, so triggers stay whole. It is applied
  inside one `BEGIN IMMEDIATE` transaction, with `user_version` re-read under the lock, which
  makes two concurrent first opens safe.
- These cases exit 2 with `CC_E_CONFIG` instead of 9:
  - an unknown version (not 0 and not 1);
  - a non-database file (`SQLITE_NOTADB`);
  - a version-0 DB that already holds tables, which is refused rather than adopted.

**One source for the schema version.** `schema.sql` does not contain
`PRAGMA user_version = 1`; `connection.SCHEMA_VERSION` sets it.

**`limits.py` and schema.sql.**
- `limits.py` adds `LINE_SEPARATOR` (`" — "`), `line_length()` and the schema enums
  (`BUCKET_IDS`, `TIP_STATUSES`, `SOURCE_KINDS`, `SOURCE_MODES`), next to the §4.2 constants.
- schema.sql has to spell the enums out in its CHECKs. `test_limits.py` parses every
  `CHECK (col IN (...))` and the bucket `BETWEEN`, and asserts both equal `limits.py`. It also
  asserts that no unmapped enum exists.

**Triggers** (7, all `BEFORE … RAISE(ABORT)`; that raises `sqlite3.IntegrityError`):
- list_item insert and update (`OF list_id, tip_id`): the bucket-1 rule and the kids rule;
- tip `UPDATE OF bucket` and `UPDATE OF audience`;
- list `UPDATE OF audience`.

A missing tip or list is left to the foreign keys. A position-only reorder does not fire them.

**`source_key`:**
- The kind is sanitized and capped at 60, like the body. This is a no-op for valid refs and only
  bounds a malformed one.
- A ref without a body (`manual`) gives `manual-<h8>`, not `manual--<h8>`.

**Seeds** are `INSERT OR IGNORE`. A list's `sort_order` is set to its id (1–7); the plan does not
set it.

**`PROJECT_URL`.**
- `git remote get-url origin` gave `https://github.com/aberson/checklist-care.git`, so
  `PROJECT_URL` is `https://github.com/aberson/checklist-care` (a literal, with a comment).
- `USER_AGENT` is `checklist-care/0.1.0 (+https://github.com/aberson/checklist-care)`.
- The version has one source: hatch reads `__version__` from `__init__.py`, and a test checks
  that `importlib.metadata` agrees.

**Timestamps.** `connection.utc_now()` and `format_timestamp()` produce
`YYYY-MM-DDTHH:MM:SSZ`; `paths.file_stamp()` produces `YYYYMMDD-HHMMSS`, both in UTC. Tests
monkeypatch `connection.utc_now`.

**`.gitignore`** follows the §3.6 list plus `*.db-journal`. **`.gitattributes`** is exactly
`* text=auto eol=lf`. **LICENSE** is MIT, "Copyright (c) 2026 Abraham Robison", as in sibling
repos. New files are written with LF line endings.

**Not changed:** AGENTS.md and README.md still say "no code yet". That is outside this step's
Files list and left for `/repo-update`.

## Pre-existing format debt

None. There were no Python files before this step, and `ruff format --check .` is clean.
