# Step 15 dev report: acceptance scorer and in-sample labels

## What was built

- `src/checklist_care/scoring/score.py` (new package `scoring/`, with `__init__.py`): the
  `eval score` engine.
  - **Reads only production artifacts through production code:** `source_store.all_sources`,
    `tip_store.find_tips`, `list_store.lists_by_id` (one `connection.snapshot`), the cached
    packet through `context.read_source_packet` + `packet.packet_path`, and the archived
    proposal through `proposal.archive_path` + `proposal.decode`. The current skill
    fingerprint is `context.skill_sha256()` (the function `context` and `propose` use).
    IS-7's limits use `proposal.text_problem` / `proposal.line_problem` (the validator's
    rules), not a copy.
  - **Labels** (`checklist-care.labels/1`, Appendix E): strict parser. Unknown keys at any
    level, wrong types (a bool bucket), repeated refs/titles/ids, unknown criterion ids
    (including `IS-9`, an operator verdict, and every Step 16 id), and a criterion whose
    label data is missing (IS-3 with no `"name": "exact"`, IS-4 with no placement
    expectation, IS-5 with no pair, IS-S with no soft group) all abort. JSON is strict
    (repeated keys, NaN, non-UTF-8 abort).
  - **Matching:** `chapter_index` puts each tip in at most one window: a chapter start
    within 5 s after `start_s` (`start - 5 <= start_s < start`, nearest wins) claims it,
    else the `[start_s, end_s)` window. "The tip in chapter X" is the lowest id (a note
    when several), an empty window fails the criterion.
  - **ABORT rules** (exit 6, stable `rule` ids): data dir not explicit; labels unusable;
    no/unreadable database; label list id that does not exist; source missing; source
    unreviewed; packet missing/unreadable/wrong ref/wrong kind/bad chapter shape
    (overlap, unsorted, zero-length, repeated title); YouTube packet with zero segments or
    zero chapters; wiki/text packet with empty text (rule present now, exercised by the unit
    tests; Step 16 adds its integration test); label title not byte-equal to a packet
    chapter title (with a "the packet has ..." hint for near matches such as a curly
    apostrophe); archive missing/unreadable/without hashes; archive `packet_sha256` vs the
    cached packet; mixed `host`; mixed `skill_sha256`; stale `skill_sha256`; operator edits.
  - **Criteria by id** (`CRITERIA` registry): IS-1..IS-8 hard, IS-S soft (reported, never
    gates). Mixed `model` values are reported (`models`, `mixed_models`), never aborted.
  - **Results:** every result, ABORT included, is written to
    `<data-dir>/eval/score-<YYYYMMDD-HHMMSS>.json` (UTF-8, LF, `-2`, `-3`... instead of
    overwriting). An abort raised before the database opens (data dir, labels, database)
    writes nothing, so a non-explicit data dir is never touched.
- `src/checklist_care/cli.py`: `eval score --labels FILE [--json]` (nested subparser,
  `--data-dir` works before or after, via the existing SUPPRESS parent). PASS 0, FAIL 1,
  ABORT 6 with a one-line `CC_EVAL_ABORT` stderr message. Any other `CheckError` raised
  inside the handler becomes `CC_EVAL_ABORT` (exit 6). An unexpected exception inside
  `eval score` prints `CC_EVAL_ABORT: eval score crashed, so there is no verdict;
  CC_E_INTERNAL: unexpected ...` and exits 6 (other commands keep exit 9), per the plan's
  `cli.py` row and Appendix C.
- `eval/labels/in_sample.json`: from Appendix B's operator labels and the chapter titles
  only. Appendix E's sketch plus: the Sit Up Straight chapter (`bucket 1`, exact name; it is
  an operator label in Appendix B and IS-3/IS-4 name it), and a `soft` array carrying IS-S's
  chapter titles and expectations, so the criterion code holds no titles.
- `tests/fixtures/packet_RPK-xw3SrVw_synthetic.json`: real title, channel, duration (554 s)
  and chapter titles/timings from ONE yt-dlp `--dump-single-json` call made while authoring
  (2026-11-11, yt-dlp's default user agent, outside the gate; titles match Appendix F),
  synthetic segments, written through `packet.encode_packet` so `fetch` with fake seams
  reproduces it byte for byte (tested).
- `tests/test_skill_packages.py` (small, outside the Files list): the Step 4 leak check now
  also reads every chapter title named by the committed `in_sample.json` (its docstring
  said "Step 15 moves the labels into eval/labels/").

## Deliberate choices a reviewer should check

1. **Operator-edit abort compares two more fields than the plan lists:** `duplicate_of` and
   `possible_duplicate_of`, besides name, action, bucket, audience and `list_ids`. IS-5 and
   IS-6 read them, and both are PATCH-editable (and `ON DELETE SET NULL` can clear
   `duplicate_of`), so without this an edited DB could pass IS-5/IS-6 on the operator's
   work. Strictly tighter than the plan; documented in the module docstring.
2. **`on_lists: []` means "on no list"**; a non-empty `on_lists` means "each of these is in
   `list_ids`" (plan: "On list X means X is in the tip's list_ids"). `on_any_of` needs one.
3. **IS-6** excludes only the later tip of each labelled pair; `possible_duplicate_of` is
   always allowed (noted).
4. **IS-7 scope** is the labelled sources' tips (list items, kids lists, limits).
5. **IS-1** can only fail on mode or a repeated ref, since missing/unreviewed abort first;
   its garbage run tampers `source.mode`.

## Calibration evidence (tests)

- `tests/integration/test_cli_eval.py` (51 tests): DBs built through `cli.main` init ->
  fetch (fake seams from the fixture packets) -> context -> propose, scored against the
  committed labels.
  - Known-good run A (RPK first) and run B (LpeDuC2sQ-w first) exit 0, every hard IS PASS,
    IS-S PASS. The good runs place one tip 3 s before a boundary (Two-Minute Brain Trick at
    386 s; Drink Water Before Coffee at 293 s), so they pass only with the 5 s lead rule.
  - Garbage via propose, each exits 1 with `failed == [id]`: IS-2 (empty window; 13 tips),
    IS-3, IS-4, IS-5 (both orders), IS-6, IS-8.
  - Garbage via tampering a copytree'd data dir with the archive edited to match: IS-1
    (mode), IS-7 (adults tip on a kids list after dropping the trigger; list item on a
    bucket-3 tip after dropping the trigger; name over NAME_MAX). The original still passes.
  - ABORT exit 6: not explicit (nothing written anywhere), source missing, unreviewed, zero
    segments, zero chapters, label/packet title drift (curly apostrophe), packet changed
    after the proposal, archive missing, mixed host, stale skill (real `skill_sha256()` over
    a monkeypatched `REPO_ROOT` copy of the skill files, edited after propose), mixed skill
    versions, operator edits (name, action, bucket via leave_out, audience, list_ids,
    duplicate_of, deleted tip, added tip; all through the production store), unusable
    labels (6 defects), missing labels, label list id missing, no database, crash.
  - Approving every tip is not an edit (still PASS); env var or flag-after-subcommand work;
    the score file equals the printed JSON and is never overwritten.
- `tests/unit/test_scorer_rules.py` (122 tests): window matching edges, labels schema
  defects, every abort rule function, each criterion pass/fail, report verdict logic.
- `tests/integration/test_live_labels.py` (`@pytest.mark.live`, deselected by default):
  every label title appears byte-exact in the live chapters; each fixture packet's chapters,
  duration and title equal the live metadata (re-checks the fixtures in M1). Note: the
  latter also re-checks the Step 3 LpeDuC2sQ-w fixture's timings, which this step did not
  re-verify live.
- Mutation sweep (outside the gate, restored afterwards): 17 mutations of the scorer and the
  CLI crash exit (lead 0 s, no edit/stale/title/packet-stale/mixed-host aborts, lax IS-1/6/7/8,
  `on_lists []` lax, zero chapters accepted, crash exit 9, non-explicit accepted, highest id
  instead of lowest, 13 tips allowed, unknown id skipped): each turned the suite red.

## Gates (run from the worktree, bash, each judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 38 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 79 files already formatted. No pre-existing
  format debt.
- `uv run pytest` (full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1259 passed, 1 skipped, 6 deselected (the `live` tests, 4 of them new here) in
  274.8 s.

The one yt-dlp call (RPK-xw3SrVw metadata) was made while authoring, outside every gate.
The default gate makes no network request.

# Round 2

Every round-1 Nit is resolved; the FYIs that bear on measurement validity are addressed too.

## NIT 1: every field the scorer reads is checked against the archive

- `score.py`: new `TipFields` (frozen dataclass: name, action, reason, bucket, audience,
  `start_s`, `list_ids`, `duplicate_of`, `possible_duplicate_of`). `EDIT_FIELDS` is now
  derived from it (`tuple(f.name for f in fields(TipFields))`), one source of truth.
  `ScoredTip(TipFields)` adds only the id and is built from a stored `TipDetail` by
  `ScoredTip.of`. Every criterion, `group_by_chapter`, `_Tally.tip`, `expectation_misses`
  and `ScoredSource` now take `ScoredTip`, so a criterion cannot read a stored field that is
  not compared with the archive (mypy rejects it); a field a future criterion needs (Step
  16's `locator`, `evidence_url`) goes into `TipFields` and is compared automatically.
- The archived proposal is read into `TipFields` (`ArchivedTip` is gone): `reason` must be a
  string, `start_s` null or a whole number (bool refused), and the envelope's `mode` is read.
- Sibling audit: the source row's `mode` (read by IS-1) is also compared with the archived
  proposal's `mode` (same `operator-edit` rule, detail "is stored in mode X, but its
  archived proposal is in mode Y"). The IS-1 garbage run now edits the archive's mode to
  match its DB tamper, as plan §4.8 prescribes for tamper runs.
- Tests: unit `test_each_edited_field_aborts` gains `reason`, `start_s` (moved to the next
  window) and `start_s` cleared; `test_the_criteria_see_a_tip_only_through_the_fields_compared_with_the_archive`
  pins `EDIT_FIELDS` and that `ScoredTip`'s fields are exactly `EDIT_FIELDS` + `id`;
  `read_archive` tests for the new fields and their malformed forms. Integration
  (`cli.main`, exit 6, abort detail names the field): `reason` (operator edit through
  `tip_store.update_tip`), `start_s (tampered)` (SQL moves "Stop Overexplaining" into the
  Mirror Check's 5 s lead), `source mode (tampered)`. Every `OPERATOR_EDITS` case now also
  asserts the text naming its field.

## NIT 2: a data dir that cannot be used aborts inside `eval score`

- `cli.py`: data-dir resolution moved into `_data_dir(args)`. Inside `eval score`
  (`set_defaults(eval_abort=True)`, read by `_is_eval`) a `ConfigError` from it (empty
  `--data-dir` or env value, a data dir inside a git checkout, an uninspectable dir) becomes
  `score.data_dir_abort(error)`: `CC_EVAL_ABORT: data-dir-unusable: <original line>`,
  exit 6. Every other subcommand still exits 2. The crash path uses the same `_is_eval`
  (the round-1 `crash_exit` attribute is gone).
- Tests through `cli.main`: empty value via flag and via env (parametrized; exact stderr,
  nothing created); data dir inside a checkout exits 6 while `status` on the same dir
  still exits 2 with `CC_E_DATA_IN_REPO`.

## NIT 3: duplicate tests removed

- Deleted unit `test_the_committed_labels_parse`; its unique assertions (path, the Fire
  chapter's parsed `Expect`, Accountability's `on_lists == ()`, the label list ids) moved
  into integration `test_the_committed_labels_parse_and_name_every_in_sample_criterion`.
- Deleted unit `test_score_files_never_overwrite_each_other`; integration
  `test_the_score_file_holds_the_printed_result` now scores three times and checks `-2`
  and `-3` and that the first file is unchanged.

## NIT 4: one strict JSON helper

- New leaf module `src/checklist_care/strict_json.py`: `loads(text)` with
  `DuplicateKeyError(key)` and `BadConstantError(name)`. `proposal.decode` and
  `score._strict_json` both call it and keep their own error types and messages
  (`ProposalInvalidError` file problems; `LabelsError`). The private copies in both modules
  are gone. Tests: `tests/unit/test_strict_json.py` (4 tests); the existing propose
  integration tests for the repeated key and NaN messages still pass unchanged.

## NIT 5: no `# fmt: skip`

- All pragmas removed. The tip tables are laid out by `ruff format`: plain tips are one
  line or one hugged argument line; the order-dependent tips with keyword arguments are
  variables above the list. A few synthetic action texts were shortened so no plain tip
  explodes to one argument per line (the names, buckets, audiences, start times and lists
  are unchanged; actions are self-written test text, never transcript text).

## FYIs

- Read-only open: new `connection.connect_read_only(path)` (production db layer): URI
  `mode=ro`, no pragma that persists, no schema, `ConfigError` for a `user_version` other
  than `SCHEMA_VERSION`, a non-SQLite file or an open failure. `score.run` uses it and maps
  a `sqlite3.DatabaseError` while collecting to `database-unusable`. Tests: a version-0,
  zero-table, non-empty file aborts 6 and is left byte-identical (round 1 would have
  applied the schema); the known-good runs assert every data-dir file is unchanged. Note:
  on a WAL database SQLite creates its own empty `-wal` and the `-shm` index when missing
  and a read-only connection cannot remove them (the next writable connection does); the
  test allows exactly those two, with the `-wal` empty, and the docstring says so.
- Duplicate source rows: tips are keyed by source row id and the scored tips are those of
  the row that was checked (`ref` is UNIQUE in the schema, so this is defensive).
- Labels size cap: `LABELS_FILE_MAX_BYTES` (1 MiB), read no further (unit test at and
  past the cap).
- Single-letter `s` renamed to `source`. `score.py` stays one module (1526 lines); Step 16
  extends it.

## Round 2 calibration (mutation sweep outside the gate, files restored after)

Each mutation turned its targeted tests red: `start_s` dropped from `EDIT_FIELDS`;
`reason` dropped; the mode comparison disabled; the eval data-dir conversion disabled
(3 tests); the writable `connection.connect` instead of `connect_read_only`; the tips of
another source row scored.

## Round 2 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. No pre-existing
  format debt. (While developing, `ruff format` was applied only to files this step
  created or changed: `scoring/score.py`, `strict_json.py`, `db/connection.py`,
  `proposal.py`, `cli.py` and the three scorer test files.)
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1284 passed, 1 skipped, 6 deselected (the `live` tests) in 281.4 s. No network
  request in the default gate.

# Round 3

Round 2's Style lens Nits are resolved (NIT 1, NIT 2) or declined as plan-mandated
(NIT 3). Everything rounds 1-2 established (read-only DB open, full edit comparison,
exit 6 inside `eval score`, the measurement-validity behavior) is unchanged.

## NIT 1: one decode ladder, shared by `proposal.decode` and the labels reader

- `src/checklist_care/strict_json.py` now owns the whole bytes-to-JSON ladder:
  `decode(data: bytes) -> Any` refuses a UTF-16 byte-order mark, then NUL bytes (both
  before decoding, as `proposal.decode` did), decodes `utf-8-sig`, and parses strictly
  (repeated keys, `NaN`/`Infinity`). Every defect is one classified `JsonFileError(kind,
  message)`; `kind` is one of `UTF16`, `NUL_BYTES`, `NOT_UTF8`, `DUPLICATE_KEY`,
  `BAD_CONSTANT`, `NOT_JSON` (`ENCODING_KINDS` groups the first three), and `message` is
  the shared one-line sentence with no advice. The round-2 public `loads`,
  `DuplicateKeyError` and `BadConstantError` are gone (folded into `decode`); the
  `_UTF16_BOMS` constant moved here from `proposal.py`.
- `proposal.decode` wraps it: `_file_problem(error)` appends `; SAVE_AS_UTF8` to the
  encoding kinds and `; use a finite number or null` to `BAD_CONSTANT`, so every problem
  text is byte-identical to before (the over-long-integer / deep-nesting detail is the
  same `one_line(str(error))[:200] or type name`). Its existing tests
  (`tests/integration/test_cli_propose.py`, `tests/unit/test_proposal_validate.py`) are
  unmodified and green. `proposal.py` no longer imports `json` or `one_line`.
- `score._strict_json` wraps it too: `LabelsError(message)`, plus `; save the labels as
  UTF-8` for the encoding kinds. The labels reader now refuses UTF-16 and NUL-holding
  files before decoding, exactly as proposals are refused. Its over-long-integer /
  deep-nesting message now carries the same detail as the proposal's (was the type name
  only).
- Tests: `tests/unit/test_strict_json.py` rewritten for `decode` (valid JSON with and
  without a UTF-8 BOM; 11 defects, each with its kind and message; and
  `test_the_proposal_and_labels_readers_refuse_the_same_bytes_alike`: for each of the 11
  defects, both readers refuse and both messages start with the shared message).
  `tests/unit/test_scorer_rules.py::test_a_utf16_or_nul_holding_labels_file_is_refused_before_it_is_decoded`:
  the committed labels saved as UTF-16 LE with BOM, UTF-16 BE with BOM, and with one NUL
  byte; exact `LabelsError` text, and `load_labels` aborts with `labels-unusable`.
- Mutation (outside the gate, restored): a labels reader that strips the BOM/NULs
  before the shared decoder turned 7 tests red (the 3 new labels tests and 4 sharing
  cases).

## NIT 2: the score file is published atomically and never over another file

- `score.write_report` writes the bytes to a sibling temporary file
  (`eval/.score-<stamp>.json.<pid>.tmp`, the dot-name pattern of `ArchiveWrite` and
  `write_packet`), then publishes it with `os.link(temporary, target)` under the first
  free name (`score-<stamp>.json`, `-2`, `-3` ...). Unlike `os.replace`, `os.link` fails
  with `FileExistsError` when the name is taken (verified on this Windows host: WinError
  183, target untouched), so the never-overwrite guarantee holds and a reader never sees
  a partial file. Hard links in the data dir are already relied on by `PacketSwap`. The
  temporary name is removed in `finally` (an `OSError` there is suppressed: a leftover
  dot-file is harmless, turning a published result into an abort is not).
- A failed write (`OSError`) is now `ConfigError("cannot write the score file in <eval
  dir>: <error>")`, which `cli._cmd_eval_score` already turns into `CC_EVAL_ABORT:
  CC_E_CONFIG: ...`, exit 6 (round 2 exited 6 too, through the crash path; the message is
  now specific, like `ArchiveWrite`'s "cannot write the archived proposal").
- Tests (through `cli.main`): `test_the_score_file_holds_the_printed_result` keeps the
  `-2`/`-3` and first-file-unchanged checks and now also asserts the eval dir holds
  exactly the three score files (no temporary left). New
  `test_a_failure_while_writing_the_score_file_aborts_and_leaves_no_score_file`: every
  write-mode `io.open` (which `Path.open` / `Path.write_bytes` call) returns a handle that
  writes half the bytes and then raises `ENOSPC`; the run exits 6 with the
  `cannot write the score file` line, prints nothing on stdout, leaves the eval dir empty,
  and the next score gets the final name `score-20261108-120000.json`.
- Mutations (outside the gate, restored): the round-2 direct `open("xb")` write (error
  still wrapped) leaves a truncated `score-20261108-120000.json` and turns the new test
  red; `os.replace` instead of `os.link` turns the `-2`/`-3` test red; no temporary
  cleanup turns both tests red.

## NIT 3: declined (plan-mandated name)

`tests/fixtures/packet_RPK-xw3SrVw_synthetic.json` keeps its name. The plan's Step 15
Produces and Files name this exact file, and Step 16 adds
`packet_rgntlzYeaRM_synthetic.json` and `packet_VbGMWtr0AJI_synthetic.json` by the same
video-id scheme (three YouTube fixtures cannot all be `packet_youtube_synthetic.json`).
Renaming it would break plan conformance; declined for that reason.

## FYIs

- `connection.connect` vs `connect_read_only` error mapping: not aligned, deliberately.
  `connect`'s narrow `SQLITE_NOTADB` mapping is pre-existing behavior other commands rely
  on (outside this step's scope); `connect_read_only` serves only `eval score`, where any
  unreadable database must be a classified `database-unusable` abort rather than the
  crash path.
- `strict_json`'s exception idiom: now a single `JsonFileError(ValueError)` in the leaf
  module, converted by each reader into its own `CheckError`-based error before
  `cli.main` sees it. Kept as a `ValueError`, per the FYI.

## Round 3 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. No pre-existing
  format debt.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1304 passed, 1 skipped, 6 deselected (the `live` tests) in 338.7 s. No network
  request in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded): 13 files
changed, 4315 insertions(+), 65 deletions(-). Round 3 touched `strict_json.py`,
`proposal.py`, `scoring/score.py`, `tests/unit/test_strict_json.py`,
`tests/unit/test_scorer_rules.py` and `tests/integration/test_cli_eval.py`.

# Round 4

One consolidation sweep, as asked: production first (score.py against its neighbors),
then the three scorer test files. The four round-3 Nits are resolved inside it (marked
NIT 1-4 below). Everything rounds 1-3 established is unchanged: read-only DB open, the
full edit comparison, exit 6 inside `eval score`, the strict shared decoder, the atomic
non-overwriting score file and the plan-mandated file names.

## (a) Production sweep

Each re-spelled item now has one owner, and every caller imports it. Behavior and
messages are byte-identical, with the one exception noted below the table.

| # | Re-spelled in score.py (or in this step's strict_json.py) | One owner now | Callers |
|---|---|---|---|
| 1 | `_SHA256` regex (**NIT 4**) | `proposal.is_sha256` (was private `_is_sha256`; its pattern stays private) | propose envelope checks, `score.read_archive` |
| 2 | the `("packet_sha256", "skill_sha256")` field list | `proposal.HASH_FIELDS` | `proposal._check_envelope`, `score.read_archive` |
| 3 | `score._is_int`, identical to `proposal._is_whole` | `strict_json.is_whole` (the leaf both import; `TypeGuard[int]`) | labels parser, archive reader, `proposal._is_id`, `_check_start`, the bucket check |
| 4 | `load_labels`' capped read, the same code as `proposal.read_file` | `strict_json.read_capped(path, max_bytes)` (None when over the cap, `OSError` propagates) | `proposal.read_file`, `score.load_labels`; each still words its own two refusals |
| 5 | IS-7's kids-list and bucket-1 rules, re-implemented | `placement.placement_problems` (the rules `propose` and the API apply; `server/demo.py` already uses it) | `score.invariant_failures` |
| 6 | the temporary name `.<stem>.json.<pid>.tmp` (a third spelling, besides `packet._sibling` and `ArchiveWrite`) | `paths.sibling_path(path, suffix)` | `write_packet`, `PacketSwap` (`.tmp`, `.bak`), `ArchiveWrite.install`, `score.write_report`; names byte-identical |
| 7 | `write_report`'s `-2`, `-3` numbering loop, the same as `paths.backup_path`'s | `paths.numbered_paths(directory, stem, suffix)` | `backup_path`, `write_report` |
| 8 | `ScoredTip.of` re-listing the nine `TipFields` | derived from `EDIT_FIELDS` (`getattr` per field, `list_ids` sorted) | none beyond `ScoredTip.of` itself |
| 9 | `strict_json._UTF16_BOMS` (a bytes literal), equal to `sources/text.UTF16_BOMS` | `strict_json.UTF16_BOMS` (the leaf, now public); `text.py` imports it, and its text-specific note became a comment at the check | `strict_json.decode`, `text.decode_text` |

**Deliberate message change (item 5).** IS-7's two placement failures now carry
`placement.py`'s own texts, prefixed with the tip, the way `propose` reports them. For
example: `#1 'Plain Tip': list 3 (Kids Bathroom — Kids) is a kids list and accepts only
kids+ tips; this tip is adults`. Importing a rule means taking its owner's wording, so
this is the one place where "messages identical" could not hold. Verdicts are unchanged:
the same tips fail IS-7. In one corner case a tip now gets one failure line instead of
two: a non-bucket-1 adults tip on a kids list gets only the bucket line, as `propose`
reports it. No Done-when assertion reads these texts. The unit IS-7 cases now assert the
owner's texts.

Checked and deliberately kept:

- **The `("NAME_MAX", NAME_MAX)` pairs in IS-7.** No owner constant exists to import.
  `proposal.text_problem` takes the `(name, value)` pair at each call site, and
  `proposal._check_tip`, `server/models.py` and `server/demo.py` spell it the same way.
- **`packet_aborts`' `source_ref` check, rather than `context.load_source_packet`.** That
  function re-reads the row outside `collect`'s snapshot and folds missing, unreadable
  and foreign packets into one `ConfigError` with fetch advice. `collect` already uses
  its reader, `context.read_source_packet`, and `packet_path`.
- **The labels `_keys`, `_text` and `_bucket` versus `proposal._fields` and
  `packet._object`.** Their contracts differ (a checker that collects every problem, a
  raiser that refuses unknown keys), so none can be imported without changing its errors.
- **`strict_json._DETAIL_MAX` versus `_DETAIL_MAX` in `wiki.py` and `youtube.py`.** These
  are private truncation caps for error text, not data-shape constants. The strict_json
  one names `proposal.py`'s former literal 200.
- **`text.decode_text`'s NUL check versus `strict_json.decode`'s.** The message differs
  and the text reader has a cp1252 fallback, so they are not one rule. Only the shared
  constant (item 9) moved.
- **Test-module path constants (`ROOT`, `FIXTURES`, the labels path).** The repo
  convention is that each test module defines its own; there is no conftest or shared
  test module.

## (b) Test sweep

Each behavior is now asserted once, at the layer that owns it: shared-decoder defects in
`test_strict_json.py` through both readers; labels-reader behavior (its advice suffix,
its `EvalAbortError` wrapping, every schema defect, every rule sub-case) in
`test_scorer_rules.py`; end-to-end CLI behavior in `test_cli_eval.py`.

| Case | Was asserted in | Now asserted once in |
|---|---|---|
| each decoder defect's kind and one-line message (**NIT 3**) | `test_strict_json::test_each_defect_is_one_classified_error` and the shared-reader test | `test_strict_json::test_each_defect_is_one_classified_error_both_readers_refuse_alike` (kind, message prefix and no newline folded in) |
| truncated, repeated-key, NaN and UTF-16 labels bytes (**NIT 1**) | `test_scorer_rules::test_labels_that_are_not_one_strict_json_object_are_refused` and the shared test | the shared test. The `b"[]"` case (the labels' own shape rule, covered nowhere else) stays as `test_labels_whose_json_is_not_an_object_are_refused` |
| UTF-16 LE, UTF-16 BE and NUL labels files (**NIT 2**) | `test_scorer_rules::test_a_utf16_or_nul_holding_labels_file_is_refused_before_it_is_decoded` (3 cases) and `BAD_FILES` | the defects: the shared test. The labels-only behavior: one case, `test_load_labels_adds_the_labels_advice_and_aborts` (exact `labels-unusable: <path>: ...; save the labels as UTF-8` text) |
| `load_labels` wrapping a defect, and a missing file | `test_load_labels_turns_every_defect_into_an_eval_abort`, the advice test and the integration missing-labels test | wrapping: the advice test. Missing file: integration. Its "lacks claim, sources, criteria" text (covered nowhere else) moved into unit `LABEL_DEFECTS` as "required keys missing" |
| six labels-schema defects end to end | integration `LABEL_DEFECTS` and unit `LABEL_DEFECTS` | unit `LABEL_DEFECTS`. Integration `test_an_unusable_labels_file_aborts_and_writes_nothing` keeps the two `load_labels` refusal paths: a missing file (merged in from `test_a_missing_labels_file_aborts`) and the operator-only id IS-9 (covered nowhere else, now with its text asserted) |
| every label title is a fixture chapter title | integration `test_every_label_title_is_a_chapter_title_of_its_fixture_packet` and both known-good runs (`title_aborts` over the same `chapter_refs()`) | the known-good runs, plus the curly-apostrophe abort test |
| the lowest id scored in a crowded window, with a note | integration `test_several_tips_in_a_window_score_the_lowest_id_with_a_note` and unit `test_is3_scores_the_lowest_id_in_the_window_and_notes_the_others` | unit |
| IS-2 with 13 tips | integration garbage `IS-2-too-many-tips` and unit `test_is2_needs_8_to_12_tips[13]` | unit. The integration IS-2 garbage run is the empty window |
| IS-7: a bucket-3 tip on a list; a name over NAME_MAX | two integration tamper runs and unit `test_is7_fails_on_each_broken_invariant` | unit. The integration IS-7 garbage run is the adults tip on a kids list, which drops a trigger (the plan's reason for tampering) |
| the IS-7 and IS-8 known-good anchors | repeated in every parameter (8 and 3 times) | once each: `test_placed_tips_within_every_limit_keep_is7` (merged with the kids+ case) and the first line of `test_is8_fails_on_subscribe_in_any_case` |

**Unchanged Done-when tests:**

- both known-good runs;
- one garbage run per IS-1 to IS-8 with `failed == [id]` (IS-1 and IS-7 by tampering a
  copy);
- the IS-5 run-B-order garbage run (no other test asserts that order failing);
- every applicable ABORT case exiting 6, with its detail assertions.

No new test was added. The two rewritten unit tests replace deleted duplicates, and the
moved case keeps an otherwise-dropped assertion.

## Calibration: one mutation per new owner (outside the gate, sources restored byte for byte)

Each broken owner turned the tests of both of its callers red:

- **IS-7 without `placement_problems`:** 4 failed (the 3 unit placement cases and the
  integration IS-7 tamper run).
- **`is_whole` accepting a bool:** 5 failed (labels "bucket as a bool", the archive
  `start_s: true`, and propose's `start_s`, `bucket` and `list_ids` true cases).
- **`read_capped` refusing a file exactly at the cap:** 2 failed (the labels cap test and
  `test_proposal_validate::test_read_file_reads_a_file_at_the_size_cap`).
- **`numbered_paths` numbering from -1:** 2 failed (`test_paths::test_backup_path_never_reuses_a_name`
  and the score-file `-2`/`-3` test).
- **`is_sha256` accepting 63 digits: SURVIVED.** No test in the suite refuses a 63-digit
  hash. The gap predates this step: `propose` never tested it, and the scorer's private
  copy inherited it. It is not added here, because the round brief allows no new tests
  beyond de-duplication. FYI for a later step.

## Round 4 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. No pre-existing
  format debt. Check mode only: the one finding while developing (an over-long line in
  `score.py`) was fixed by hand.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1275 passed, 1 skipped, 6 deselected (the `live` tests) in 334.9 s. This is
  round 3's 1304 minus the 29 duplicate cases the sweep removed: 11 + 4 + 2 + 2 unit,
  and 1 + 1 + 1 + 2 + 5 integration. No network request in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed
with a throwaway index): 16 files changed, 4255 insertions(+), 105 deletions(-). Round 4
touched:

- `scoring/score.py`, `strict_json.py`, `proposal.py`, `paths.py`;
- `sources/packet.py` and `sources/text.py` (import-only moves to the new owners);
- `tests/unit/test_strict_json.py`, `tests/unit/test_scorer_rules.py` and
  `tests/integration/test_cli_eval.py`.

# Round 5

Surgical round: the two round-4 Style Nits, the same sibling check on both owners, and
the FYI's OSError wording. No test was added, changed or removed; every behavior from
rounds 1-4 stands. Five source files changed: `db/connection.py`, `strict_json.py`,
`sources/text.py`, `proposal.py` and `scoring/score.py`.

## NIT 1: the busy timeout has one owner

- `connection.py` names it once: `BUSY_TIMEOUT_PRAGMA = "PRAGMA busy_timeout=5000"`.
  `PRAGMAS` starts with it (order and comment unchanged, so `connect` still sets it
  before WAL), and `connect_read_only` executes the same constant. A change to the
  timeout now reaches both connections. The read-only docstring says it is the one
  (per-connection, not persistent) pragma that the reader sets.
- **Sibling check of every other literal in `connection.py`:**
  - `isolation_level=None` and `conn.row_factory = sqlite3.Row` were restated in
    `connect` and `connect_read_only` (the module docstring names both as the contract of
    every connection). Both now come from one private `_open(database, *, uri,
    check_same_thread)`, which both functions call. Error handling is unchanged:
    `connect_read_only` still turns a failed open into `ConfigError`, and `connect` still
    lets it propagate.
  - `SCHEMA_VERSION` is already read from its owner everywhere (the read-only version
    check, `_apply_schema`, `_require_empty_version`).
  - `"PRAGMA user_version"` is read only in `schema_version()`, which `connect_read_only`
    and `_apply_schema` call; the one write (`f"PRAGMA user_version = {SCHEMA_VERSION}"`)
    takes its value from the owner. This is the SQLite pragma name, not a tunable value.
  - `BEGIN IMMEDIATE`, `BEGIN`, `COMMIT` and `ROLLBACK` are SQL statements, each used
    only inside its own helper (`transaction`, `snapshot`).
  - No other module opens a SQLite connection or sets a pragma: `grep sqlite3.connect`,
    `busy_timeout`, `isolation_level` and `row_factory` over `src/checklist_care` hit only
    `_open` and the module docstring.

## NIT 2: one capped-read rung, which text.py now uses

- `strict_json.py` splits the open/read(cap + 1) step out of `read_capped` as
  `read_bounded(path, max_bytes) -> bytes` (the file's bytes, read no further than one
  byte past the cap). `read_capped` is now `read_bounded` plus the None-over-cap check,
  so `proposal.read_file` and `score.load_labels` are unchanged.
- `sources/text.py:read_text_file` reads through `read_bounded`.
  **Why not `read_capped` itself:** the brief requires text.py's existing tests to stay
  green unmodified, and `test_text_normalize::test_read_text_file_reads_at_most_one_byte_over_the_cap`
  spies on `text_from_bytes` and asserts it receives exactly cap + 1 bytes (`seen == [11]`).
  `read_capped` discards those bytes and returns None, so `text_from_bytes` would never
  be called and the test would fail. With `read_bounded`, `text_from_bytes` still owns the
  text-file over-cap refusal (its own wording, exit 4), and the open/read idiom exists
  once, in `strict_json.py`.
- **Sibling grep** (`open("rb")`, `.read(`, `read_bytes`, `read_text` over
  `src/checklist_care`): `strict_json.read_bounded` is now the only open/read(cap + 1).
  The rest are not capped reads of an operator file and are left alone:
  - `sources/wiki.py:_read_capped` reads an HTTP response in chunks and raises
    `FetchError` past `RESPONSE_MAX_BYTES`. It is a network stream, not a path, and the
    file is untouched by this step (step 13 code).
  - `score.read_archive`, `packet.read_packet`/`context.read_source_packet` and
    `context.skill_sha256` use uncapped `read_bytes()` on files checklist-care itself
    wrote (or the repo's skill files). Adding a cap would be a behavior change, so none
    was added. Only `read_archive` is this step's code; the others predate it.
  - `connection.schema_sql` reads the packaged `schema.sql` resource.

## FYI: one OSError wording for every file reader

- New `strict_json.read_error_reason(error) -> str`, next to `read_capped`: one line
  (`one_line`) of `strerror`, else `str(error)`, else the type name. Each reader keeps its
  own sentence around it, per `strict_json.py`'s documented design (the other FYI):
  - `text.read_text_file`: `cannot read the text file '<path>': <Type> (<reason>)`;
  - `proposal.read_file`: `cannot read the proposal file <path>: <reason>`;
  - `score.load_labels`: `labels-unusable: cannot read the labels file <path>: <reason>`;
  - `score.read_archive` (a fourth variant the sibling check found:
    `error.strerror or error`): `cannot read <path>: <reason>`.
- Effect on output: none when the OS gives a `strerror` (every real open/read failure,
  and every test). Only an `OSError` without one changes: proposal and labels show the
  error's text instead of its bare type name, text.py shows the type name instead of an
  empty `()`, and `read_archive` shows the type name instead of an empty string.
- Pre-existing `OSError` messages outside this step (`context.skill_sha256`,
  `paths.reject_git_checkout`, `paths.ensure_layout`, the archive and score-file writers)
  use `{error}` for write or inspect failures, not file reads. They were not changed.

## Round 5 calibration (outside the gate, source restored byte for byte)

- **`read_bounded` reading `max_bytes` instead of `max_bytes + 1`:** 3 failed, one for each
  caller: `test_text_normalize::test_read_text_file_reads_at_most_one_byte_over_the_cap`,
  `test_proposal_validate::test_read_file_rejects_one_byte_over_the_size_cap` and
  `test_scorer_rules::test_a_labels_file_over_the_size_cap_is_refused_unparsed`.
- The writer's busy timeout stays asserted by
  `test_cli_init::test_connection_pragmas` (`PRAGMA busy_timeout == 5000`). The reader's
  busy timeout was not asserted before this round and is not now; it now comes from the
  same constant, so no new test was added (per the brief).
- `read_error_reason`'s no-`strerror` branch is not asserted by any test, as before;
  existing tests pin only the `strerror` case (`FileNotFoundError (`, `cannot read the
  labels file`).

## Round 5 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. No pre-existing
  format debt.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1275 passed, 1 skipped, 6 deselected (the `live` tests) in 435.7 s. This is the
  same count as round 4, since no test was added or removed. No network request in the
  default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed
with a throwaway index): 16 files changed, 4301 insertions(+), 115 deletions(-). Round 4
was 4255(+)/105(-), so round 5 adds a net 46 insertions and 10 deletions. Round 5
touched `db/connection.py`, `strict_json.py`, `sources/text.py`, `proposal.py` and
`scoring/score.py`. It changed no test file.

# Round 6

Scope: the 2026-11-11 scope decision (plan Step 15 block, "Scope decision" bullet). A: every
hunk outside the Step 15 Files list audited, and each one that is not justified under point
1 or 4 reverted. B: the four round-5 findings fixed. C: everything else from rounds 1-5 kept
(read-only DB, the full archive edit comparison, exit 6 inside `eval score`, the strict shared
decoder, the atomic non-overwriting score file, the plan-mandated names, every Done-when test).

## A. Hunk audit of the files outside the Files list

The rule applied to each hunk: it qualifies under (1) only if it exposes or relocates a
**named** symbol (a constant, a function, the body of a named helper) that a line Step 15
adds or changes now imports, or it is a call site that relocation forces. An inline
expression elsewhere that merely duplicates such a symbol is pre-existing duplication (2),
like the plan's own example, `sources/wiki.py:599`. Behavior evidence: every existing test
file is unedited (`git diff HEAD --stat -- tests` lists only `tests/test_skill_packages.py`,
the named additive file), and the full gate below is green.

Line numbers are the hunk's lines in the worktree file (`git diff HEAD -U0`); "score.py" is
`src/checklist_care/scoring/score.py`.

### src/checklist_care/proposal.py (20 hunks, 97 changed lines after the reverts)

| Hunk (new lines) | What | Step 15 line that references the symbol | Verdict | Behavior evidence (existing tests, unedited) |
|---|---|---|---|---|
| -35 (removed) | `import json` | the ladder moved to `strict_json.decode`, called at score.py:420 | keep (1) | `test_cli_propose::test_an_undecodable_file_exits_5_with_a_file_problem` (truncated JSON, repeated key, NaN) |
| 47 | `import ... strict_json` | every relocated symbol below | keep (1) | as for each symbol |
| 49 | `one_line` import dropped (its only use, decode's detail, moved) | score.py:420 | keep (1) | `test_proposal_validate` (whole module) |
| 104-106 | `HASH_FIELDS` exposed (was the literal tuple in `_check_envelope`) | score.py:693 | keep (1) | `test_proposal_validate::test_a_rejected_field_is_the_one_problem[short packet_sha256]`, `test_cli_propose::test_the_unmodified_fixture_fails_only_on_its_placeholder_skill_sha256` |
| -125 (removed) | `_UTF16_BOMS` (moved with the ladder; now `strict_json.UTF16_BOMS`) | strict_json.py:113, reached from score.py:420 | keep (1) | `test_cli_propose::test_a_utf16_proposal_gets_the_save_as_utf8_problem` (3 encodings) |
| 217-219, 221-225, 231-234, -245..-260, 238-240 | `decode` reads through `strict_json.decode`; `_file_problem` re-adds this reader's advice (`; SAVE_AS_UTF8`, `; use a finite number or null`) so every problem text is byte-identical; `_DuplicateKeyError`, `_BadConstantError`, `_unique_keys`, `_no_constant` moved | score.py:420-422 (the labels reader `_strict_json`) | keep (1)/(4) | `test_cli_propose::test_a_utf16_proposal_gets_the_save_as_utf8_problem`, `::test_an_undecodable_file_exits_5_with_a_file_problem`, `test_proposal_validate::test_a_surrogate_pair_escape_decodes_to_one_accepted_character`, `::test_surrogates_are_reported_before_any_other_problem` |
| 251, 253, 255 | `read_file` reads through `strict_json.read_capped`; its reason is `strict_json.read_error_reason` | score.py:497 (`read_capped`), score.py:511 and :682 (`read_error_reason`) | keep (1) | `test_proposal_validate::test_read_file_reads_a_file_at_the_size_cap`, `::test_read_file_rejects_one_byte_over_the_size_cap`, `test_cli_propose::test_a_missing_proposal_file_exits_5`, `::test_a_file_over_the_size_cap_exits_5_without_being_decoded`. `read_error_reason` is now exactly this function's old expression (`error.strerror or type(error).__name__`), see below |
| -378..-381 (removed) | `_is_whole` moved to `strict_json.is_whole` | score.py:304, 311, 384, 401, 636, 649, 651 | keep (1) | `test_proposal_validate::test_a_rejected_field_is_the_one_problem[start_s true]`, `[bucket true]` |
| 484-485, 488, 497 | the `HASH_FIELDS` loop and the `is_sha256` call sites | score.py:693, 695 | keep (1) | `test_cli_propose::test_an_uppercase_packet_sha256_is_accepted`, `::test_the_unmodified_fixture_fails_only_on_its_placeholder_skill_sha256` |
| 520-523 | `_is_sha256` exposed as `is_sha256` (docstring added; the pattern stays private) | score.py:695 | keep (1) | as above |
| 579, 707 | `_check_start` and the bucket check call `strict_json.is_whole` (forced by the move) | score.py:304 ff. | keep (1) | the `[start_s true]` and `[bucket true]` cases above |
| `_is_id` (proposal.py:335-336) | round 4 made it call `strict_json.is_whole` | none: no Step 15 line references `_is_id` | **reverted** to baseline (2) | n/a (byte-identical to HEAD) |
| `ArchiveWrite.install` (proposal.py:852) | round 4 made it call `paths.sibling_path` | none: no Step 15 line references `ArchiveWrite`; `sibling_path` was relocated from `packet._sibling` | **reverted** to baseline (2) | n/a (byte-identical to HEAD) |

### src/checklist_care/db/connection.py

| Hunk (new lines) | What | Step 15 line | Verdict | Behavior evidence |
|---|---|---|---|---|
| 28-30, 35 | `BUSY_TIMEOUT_PRAGMA` exposed; `PRAGMAS` starts with it (order unchanged) | score.py:1534 | keep (1) | `test_cli_init::test_connection_pragmas` (`busy_timeout == 5000`) |
| 125, -123 | `connect` opens through `open_connection` (the `row_factory` line moved into it) | score.py:1530 | keep (1)/(4) | `test_cli_init::test_connection_pragmas`, every store and API test that reads `sqlite3.Row` columns |
| 141-154 | `open_connection` (round 5's private `_open`, now public so score.py can import it): `isolation_level=None` and `sqlite3.Row` | score.py:1530 | keep (1) | as above |
| `connect_read_only` | a new function, not an expose or relocate edit | n/a | **moved** out of connection.py into score.py:1514 (body unchanged; it calls `connection.open_connection`, `connection.BUSY_TIMEOUT_PRAGMA`, `connection.schema_version`, `connection.SCHEMA_VERSION`) | `test_cli_eval::test_a_foreign_database_aborts_and_is_left_as_it_was`, both known-good runs (`assert_left_as_it_was`) |

### src/checklist_care/paths.py

| Hunk (new lines) | What | Step 15 line | Verdict | Behavior evidence |
|---|---|---|---|---|
| 14, 17 | `itertools`, `Iterator` imports for `numbered_paths` | score.py:1477 | keep (1) | as below |
| 62-68 | **new this round:** `DataDir.has_database()`, relocated from `cli._has_database` (its comment is now the docstring). score.py's `run` re-spelled it (`not db_path.is_file() or db_path.stat().st_size == 0`); score.py cannot import cli.py (cli imports score), so the helper moved into the module both import | score.py:1567 (cli.py:337 and 343 call it too) | keep (1) | `test_cli_init::test_commands_without_a_database_exit_2_and_create_nothing`, `::test_commands_on_a_zero_byte_database_exit_2_and_leave_it_empty`, `::test_init_seeds_a_zero_byte_database`, `::test_init_twice_changes_nothing`, `test_cli_eval::test_a_data_dir_without_a_database_aborts_and_creates_nothing` |
| 154-170 | `numbered_paths` (relocated from `backup_path`'s loop) and `sibling_path` (relocated from `packet._sibling`) | score.py:1477, 1474 | keep (1) | `test_paths::test_backup_path_never_reuses_a_name`; `test_packet::test_packet_swap_*` (6 tests), `::test_write_then_read_round_trips` |
| 174-175 | `backup_path` takes the first free name of `numbered_paths` | score.py:1477 | keep (1)/(4) | `test_paths::test_backup_path_never_reuses_a_name` |

### src/checklist_care/sources/packet.py

| Hunk (new lines) | What | Step 15 line | Verdict | Behavior evidence |
|---|---|---|---|---|
| -160..-163 (removed) | `_sibling` relocated to `paths.sibling_path` | score.py:1474 | keep (1) | `test_packet::test_packet_swap_installs_the_new_file_and_leaves_nothing_behind` and the other 5 `test_packet_swap_*` |
| 164, 195-196 | the call sites (`.tmp`, `.bak`; names byte-identical) | score.py:1474 | keep (1) | as above |

### src/checklist_care/sources/text.py

| Hunk (new lines) | What | Step 15 line | Verdict | Behavior evidence |
|---|---|---|---|---|
| -25 (removed), 35 | `import codecs` dropped; `from checklist_care.strict_json import PRIMARY_ENCODING, UTF16_BOMS` | strict_json.py:113, 118 | keep (1) | below |
| -50 (removed) | **new this round (finding 4):** `PRIMARY_ENCODING = "utf-8-sig"` relocated into the leaf (`ENCODINGS` and the decode order unchanged) | strict_json.py:118 (`data.decode(PRIMARY_ENCODING)`) | keep (1) | `test_text_normalize::test_cp1252_and_utf8_with_bom_give_the_same_ref` (asserts `encoding == "utf-8-sig"`), `::test_utf8_wins_when_both_decode`, `::test_the_bom_is_not_part_of_the_text` |
| -61..-67 (removed), 102-103 | `UTF16_BOMS` relocated into the leaf; its text-specific sentence (a cp1252 text that starts with the same two bytes is refused too) stays as a comment at text.py's check | strict_json.py:113 | keep (1) | `test_text_normalize::test_utf16_is_refused_with_save_as_utf8` |
| `read_text_file` | round 5 made it read through `read_bounded` and word its error with `read_error_reason` | none: no Step 15 line references `read_text_file`; its inline read duplicates `proposal.read_file`'s (2), and `read_error_reason` changed its wording when the OS gives no `strerror` (4) | **reverted** to baseline | n/a (byte-identical to HEAD) |

Why relocate rather than import from text.py: `strict_json` is the leaf that both readers
import. `strict_json` importing `sources.text` would invert that, and would cycle as soon as
text.py imports it. Relocation is the decision's named alternative.

### src/checklist_care/context.py (outside the Files list; the round brief allows a (1) hunk here)

| Hunk (new lines) | What | Step 15 line | Verdict | Behavior evidence |
|---|---|---|---|---|
| 104-111 | **new this round (finding 2):** `decode_source_packet(data)` exposed from `read_source_packet`'s body (the sha256 and `decode_packet` of one read); `read_source_packet` calls it, so every other caller behaves as before | score.py:943 | keep (1) | `test_cli_propose::test_a_packet_fetched_again_after_context_makes_the_proposal_stale`, the `context` tests in `test_cli_propose`, `test_cli_eval::test_a_packet_changed_after_the_proposal_aborts` |

### src/checklist_care/strict_json.py (new leaf owner) and src/checklist_care/scoring/__init__.py

Every symbol in `strict_json.py` is either the relocated proposal ladder (`decode`, the six
kinds, `ENCODING_KINDS`, `JsonFileError`, `_unique_keys`, `_no_constant`, `_DETAIL_MAX`) or a
relocated owner (`UTF16_BOMS` and `PRIMARY_ENCODING` from text.py; `is_whole`, `read_capped`
and `read_error_reason` from proposal.py), each referenced at the score.py lines above.
Changed this round: `read_bounded` (used only by text.py's reverted read) folded back into
`read_capped`; `read_error_reason` is now exactly `proposal.read_file`'s old expression
(`error.strerror or type(error).__name__`), so `proposal.read_file` behaves exactly as at
HEAD (round 5's version added a `str(error)` fallback and `one_line`, a behavior change
under (4)); `PRIMARY_ENCODING` added (finding 4). `scoring/__init__.py` is the one-line
package marker.

### tests/test_skill_packages.py

4 hunks (the `score` import, the `IN_SAMPLE_LABELS` constant, the `LABELLED_TITLES`
docstring that said "Step 15 moves the labels", the `titles +=` line): additive coverage
named in decision point 4. No existing assertion changed.

### Existing test assertions

`git diff HEAD --stat -- tests` shows `tests/test_skill_packages.py` only (10 insertions, 2
deletions: one docstring sentence and one comment line). No other existing test file is
modified; `tests/unit/test_strict_json.py` is new. Nothing to restore.

## B. The four round-5 findings

1. **[security] Uncapped archive read: fixed.** `score.read_archive` reads through the new
   `score.read_input(path, PROPOSAL_FILE_MAX_BYTES, "PROPOSAL_FILE_MAX_BYTES")`. A path
   that is not a regular file (`stat.S_ISREG`, checked before anything opens it) or a file
   over the cap (`strict_json.read_capped`, read no further than cap + 1) is an
   `InputFileError`. `read_archive` turns it into `ArchiveError`, which is `archive-unusable`,
   exit 6. The cap is `limits.PROPOSAL_FILE_MAX_BYTES`, the one `propose` reads a proposal
   under. A missing archive still says "no archived proposal at ...".
2. **[security] Uncapped packet read on the eval path: fixed.** `collect` reads the packet
   with `read_input(packet_path, PACKET_FILE_MAX_BYTES, "PACKET_FILE_MAX_BYTES")`, then
   `context.decode_source_packet(data)` (exposed, see A). The sha256 and the parsed packet
   still come from one read, and the decoding is `propose`'s. A refusal is
   `packet-unusable`, exit 6. `context.read_source_packet` and its other callers are
   unchanged.
   - **The cap.** No packet cap existed. `PACKET_FILE_MAX_BYTES` is defined once, in
     score.py next to `LABELS_FILE_MAX_BYTES` (the scorer's other input cap), as
     `= TEXT_FILE_MAX_BYTES`, imported from `sources/text.py` rather than re-spelled. That is
     16 MiB, the ingest cap, equal to `wiki.RESPONSE_MAX_BYTES`. A packet keeps what `fetch`
     took from one source: at most `TEXT_CHARS_MAX` characters of a text file (about 3.2
     MB), one article's plain text from an answer of at most 16 MiB, or one video's
     transcript. So the cap stops a runaway or foreign file, never a packet `fetch` wrote.
     It is not in `sources/packet.py`, because that would add a new symbol to a module the
     decision lets this step edit only to expose or relocate.
   - **Sibling.** The labels file (`--labels`, operator-given) had the same gap for a path
     that is not a regular file (a FIFO would block). `load_labels` now reads through
     `read_input` too. Its over-cap text changed (Step 15's own code): `labels-unusable:
     <path>: it is larger than LABELS_FILE_MAX_BYTES (1048576 bytes)`. The unit test
     matches `larger than LABELS_FILE_MAX_BYTES` and is unchanged.
   - **Tests.** `tests/integration/test_cli_eval.py::test_an_input_file_over_its_cap_or_not_a_regular_file_aborts_unread`
     has 4 cases through `cli.main` on a `propose`-built data dir: {archived proposal,
     cached packet} x {one byte over its cap, a folder}. Each asserts exit 6, exactly that
     abort rule and the refusal text. The oversized files are padded with trailing spaces,
     so the JSON value is the same and only the cap can refuse them: read whole, the archive
     run would PASS and the packet run would abort as `packet-stale` instead. A folder stands
     in for a device or a pipe, since it is portable (Windows has no `mkfifo`).
     `tests/unit/test_scorer_rules.py::test_a_labels_path_that_is_not_a_regular_file_is_refused_unopened`
     asserts the exact `labels-unusable` text.
   - **Calibration** (outside the gate; score.py restored byte for byte and checked with
     `cmp`). With `read_archive` back on `path.read_bytes()`, 2 of the 5 new cases go red.
     With the packet back on an uncapped `read_bytes()`, 2 go red. With the regular-file
     check disabled, 3 go red (both folder cases and the labels case). Logs:
     `.build-step/r6-mut-*.log`.
3. **[test-quality] CLI operator-edit cases duplicated the unit layer: fixed.**
   `OPERATOR_EDITS` keeps 4 cases: `list_ids` (the one field edit, made through
   `list_store.append_item` and read back from the `list_item` table through
   `tip_store.find_tips` and `ScoredTip.of`), `source mode (tampered)`, `a deleted tip` and
   `an added tip`. Removed: name, action, reason, bucket, audience, duplicate_of and start_s
   (each is in `test_scorer_rules::test_each_edited_field_aborts`) and their seven helpers.
   The module docstring says where each edited field is asserted.
4. **[style] `"utf-8-sig"` re-spelled in strict_json.py: fixed.** `PRIMARY_ENCODING` is
   relocated from `sources/text.py` into `strict_json.py`. text.py imports it, and
   `ENCODINGS` and every text.py behavior are unchanged. `strict_json.decode` uses it.

## Declined (decision point 2): follow-ups for a later step

Pre-existing duplication that no Step 15 line references:

- `sources/wiki.py:599`: an inline whole-number test vs `strict_json.is_whole`.
- `proposal._is_id` (proposal.py:335-336): the same inline whole-number test.
- `proposal.ArchiveWrite.install` (proposal.py:852): an inline `.<name>.<pid>.tmp` vs
  `paths.sibling_path`.
- `sources/text.read_text_file`: an inline regular-file check and capped read vs
  `strict_json.read_capped` and `score.read_input`'s check. Its `OSError` wording
  (`strerror or str(error)`, `one_line`, a type-name prefix) differs from
  `strict_json.read_error_reason`, so unifying it is a behavior change.
- `server/database.py:46`: an inline non-empty-database test vs `paths.DataDir.has_database`.
- `context.read_source_packet`, `packet.read_packet` and `packet.file_sha256` read packets
  uncapped on the `propose` and `context` paths (the round brief scoped the cap to the eval
  path).
- Carried from round 4: no test refuses a 63-digit `sha256` (`proposal.is_sha256`).

## Round 6 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. No pre-existing
  format debt. Check mode only; the two findings while developing (an over-long docstring
  line in score.py, one assertion in test_scorer_rules.py) were fixed by hand.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1273 passed, 1 skipped, 6 deselected (the `live` tests) in 444.6 s. That is round
  5's 1275, minus the 7 CLI operator-edit cases removed (finding 3), plus the 4 new
  input-file cases and the 1 new labels case. No network request in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed
with a throwaway index): 17 files changed, 4368 insertions(+), 116 deletions(-). Round 6
touched score.py, cli.py, connection.py, paths.py, proposal.py, text.py, context.py,
strict_json.py, test_cli_eval.py and test_scorer_rules.py.

# Round 7

Scope: the round-6 Block and the six Nits, plus the requested sweep of this step's test
files. Everything rounds 1-6 established is kept: the read-only DB, the full archive edit
comparison, exit 6 inside `eval score`, the strict shared decoder, capped regular-file reads
through `score.read_input`, the atomic non-overwriting score file, the plan-mandated names,
every Done-when test and the round-6 scope audit. No hunk outside the files named below
changed; no pre-existing test file was edited (`tests/test_skill_packages.py` is unchanged
this round).

## BLOCK: IS-3 must be able to fail for each of its four chapters

**The label.** The committed `eval/labels/in_sample.json` already carried `"name": "exact"`
on the Sit Up Straight chapter (line 13:
`{"title": "Sit Up Straight, You're Not Dead Yet", "expect": {"bucket": 1, "name": "exact"}}`;
the file is unchanged since 11:19 today, round 1, sha256 `e333e419...`, and round 1's report
says so at line 47). Parsed through `score.load_labels`, all four labelled chapters have
`exact_name=True`. The text quoted in the finding (`{"bucket": 1}}`, line 15) is not in the
file. So the label needed no edit; the coverage gap the finding names was real.

**The coverage (fixed).** Plan §4.8 names four IS-3 chapters and four IS-4 placements. Now:

- `tests/integration/test_cli_eval.py::test_the_committed_labels_hold_every_in_sample_expectation_of_the_plan`
  (replaces `..._parse_and_name_every_in_sample_criterion`) asserts the committed labels'
  labelled chapters equal exactly the plan's four, each with its full expectation:
  Accountability Text `bucket 2, adults, on no list, exact name`; Sit Up Straight
  `bucket 1, exact name`; Fire Your Morning Brain `bucket 1, on list 1, exact name`; Three-
  Second Mirror Check `bucket 1, on list 5 or 6, exact name`. A missing component is a check
  that can never fail, so this test turns it red.
- `GARBAGE` (the parametrized garbage runs through `propose`) now holds one IS-3 run per
  labelled chapter (`IS-3 <title>`: the tip renamed to a near miss, kept in its window) and
  one IS-4 run per labelled chapter (`IS-4 <title>`: Accountability for kids+, Sit Up in
  bucket 3, Fire on list 4 instead of 1, Mirror on list 2 instead of 5/6). Both are built
  from `LABELLED_CHAPTERS` (the plan's four, with their video), so a missing table entry is a
  `KeyError` at import. Each run exits 1, reports exactly its id, and one of that criterion's
  failures names the chapter (`repr(title)`) and the near-miss name or the miss.
- **IS-5 / IS-6 siblings.** The plan's IS-5 is order-symmetric: the run-B garbage run
  (previously a separate test) is now the `IS-5 run B` entry next to `IS-5 run A`. IS-6 names
  one pair explicitly ("Sit Up Straight vs The Doorway Posture Check may carry
  `possible_duplicate_of`, never `duplicate_of`"); the new `IS-6 Doorway Posture Check` entry
  (run B, where the Doorway tip is the later one) marks it `duplicate_of` Sit Up Straight and
  fails exactly IS-6. The labels' duplicate pair (`list_if_first` 7) and soft groups were
  re-checked against §4.8 IS-5 and IS-S: they match.
- `GARBAGE` entries are a small `Garbage` dataclass (`criterion`, `change`, `named`, `first`),
  replacing the `(criterion, lambda)` tuples typed `Any`.

## NIT [security]: untrusted text in printed report lines

- `score.printable(text)`: each character `str.isprintable()` refuses (line breaks, tab, ESC
  and every C0/C1 control, DEL, U+2028/2029, bidirectional overrides, lone surrogates) is
  written as the escape `repr` gives it (`\x1b`, `\n`, `\u2028`); printable text, backslashes
  and non-ASCII letters included, is unchanged. `ScoreReport.lines()` returns every line
  through it, and `ScoreReport.abort_message()` (the stderr `CC_EVAL_ABORT` line) passes each
  abort line through it. These are all of the report's printed paths: the table (labels path,
  claim, source ref/host/model, abort lines, criterion failures and notes, verdict, score
  path) and the abort message.
- `--json` stdout is now `json.dumps(..., ensure_ascii=True)`: C0 controls were already JSON
  escapes, but C1 controls (`\x9b` is an 8-bit CSI), DEL, U+2028 and bidi overrides were
  printed raw. The parsed value is identical. The score file keeps `ensure_ascii=False`, so
  every string in it stays exact.
- Why not `errors.one_line` or a source's `_shown`: `one_line` collapses whitespace but keeps
  ESC; `youtube._shown` / `wiki._shown` drop non-printables and cut to `_DETAIL_MAX`, which
  would hide which chapter failed. `printable` uses `repr`'s escape for each character, and
  its docstring says why it is not `_shown`.
- Tests: `tests/integration/test_cli_eval.py::test_a_chapter_title_cannot_add_or_rewrite_a_printed_report_line`
  (through `cli.main`, a `propose`-built run whose unlabelled chapter title, fetched through
  the faked yt-dlp, is `"The Sunlight Stare" + ESC[2K + CR LF + "verdict: PASS" + U+2028 +
  NEL + CSI(0x9b)1A + RLO` and whose window is empty, so IS-2 names it): exit 1; the printed
  table splits into exactly the lines `print` wrote (`out.split("\n")` equals
  `out.splitlines()`), every line `isprintable()`, exactly one `verdict:` line and it is
  `FAIL`, the IS-2 fail line ends with the escaped title; the `--json` stdout is ASCII and
  parses to the exact title; both score files hold the exact title.
  `tests/unit/test_scorer_rules.py::test_every_printed_report_path_escapes_untrusted_text_and_the_json_keeps_it`
  covers the source host/model line, fail and note lines, the abort line and the abort
  message, and that `to_json()` keeps the raw strings; `::test_printable_text_prints_unchanged`.

## NIT [test-quality]: the EDIT_FIELDS literal tuple

Deleted from `test_the_criteria_see_a_tip_only_through_the_fields_compared_with_the_archive`;
the two `fields()` drift guards stay, with the comment reworded.

## NITs [style]: re-spelled owners

| Finding | Now |
|---|---|
| test_cli_eval.py `watch()` re-spelled `youtube.watch_url` | helper deleted; `youtube.watch_url(video)` |
| test_cli_eval.py `ref_of()` re-spelled `youtube.source_ref` | helper deleted; `youtube.source_ref(...)` at all 16 call sites |
| test_live_labels.py `YOUTUBE_PREFIX = "youtube:"` and the `"youtube:..."` keys | `YOUTUBE_PREFIX = youtube.source_ref("")`; keys `youtube.source_ref(<id>)` |
| test_cli_eval.py `data_dir / "packets" / f"{source_key(...)}.json"` (2) | `packet_module.packet_path(data_dir, ref)` |
| test_cli_eval.py `data_dir / "checklist-care.db"` (5 `sqlite3.connect`, 2 `connection.connect`) | `data_dir / paths.DB_FILENAME` |
| cli.py `raise EvalAbortError(f"{error.code}: {error.message}")` | `raise EvalAbortError(error.line())`; the printed line is unchanged (`line()` applies `one_line`, which the outer `EvalAbortError.line()` applied anyway) |

Sibling found in production while sweeping (a line this step added): the `eval score --help`
description spelled `eval/score-<ts>.json`, the exit numbers, `PASS`/`FAIL` and
`CC_EVAL_ABORT`. It is now built from `paths.EVAL_DIR`, `score.SCORE_FILE_PREFIX`,
`EXIT_OK`, `EXIT_EVAL_FAIL`, `EXIT_EVAL_ABORT`, `score.PASS`, `score.FAIL` and
`CODE_EVAL_ABORT` (rendered help text byte-identical).

## SWEEP: literals in this step's test files

| File | Literal (re-spelled owner) | Now |
|---|---|---|
| test_cli_eval.py | `"youtube:{id}"`, watch URL | `youtube.source_ref`, `youtube.watch_url` |
| test_cli_eval.py | `"packets"/<key>.json`, `"checklist-care.db"` | `packet_module.packet_path`, `paths.DB_FILENAME` |
| test_cli_eval.py | `"proposals"` (draft dir) | `paths.PROPOSALS_DIR` |
| test_cli_eval.py | `"eval"` (every data-dir path segment) | `paths.EVAL_DIR` |
| test_cli_eval.py | `SCORE_FILE = "score-20261108-120000.json"`, `-2`/`-3` names | `score_files(data_dir, n)`: `score.SCORE_FILE_PREFIX` + `paths.file_stamp(NOW)` through `paths.numbered_paths` |
| test_cli_eval.py | `NOW_TEXT = "2026-11-08T12:00:00Z"` | `connection.format_timestamp(NOW)` |
| test_cli_eval.py | exit codes `0`, `1`, `2`, `6` | `EXIT_OK`, `EXIT_EVAL_FAIL`, `EXIT_CONFIG`, `EXIT_EVAL_ABORT` |
| test_cli_eval.py | `"CC_EVAL_ABORT"`, `"CC_E_CONFIG"`, `"CC_E_DATA_IN_REPO"`, `"CC_E_INTERNAL"` | `CODE_EVAL_ABORT`, `CODE_CONFIG`, `CODE_DATA_IN_REPO`, `CODE_INTERNAL` |
| test_cli_eval.py | `"claude-code"`, `"codex"` | `HOST_CLAUDE_CODE`, `HOST_CODEX` |
| test_cli_eval.py | `"extract"`, `"investigate"` (incl. two SQL strings, now bound parameters) | `SOURCE_MODE_EXTRACT`, `SOURCE_MODE_INVESTIGATE` |
| test_cli_eval.py | `status="pending"` | `TIP_STATUS_PENDING` |
| test_cli_eval.py | table words `hard`, `soft`, `PASS` | `score.HARD`, `score.SOFT`, `score.PASS` |
| test_scorer_rules.py | `"youtube:EXAMPLE0001"` (3), `"youtube"`, `"wiki"`, `"text"`, `"generated"` | `youtube.source_ref`, `SOURCE_KIND_*`, `TRANSCRIPT_KIND_GENERATED` |
| test_scorer_rules.py | timestamps `"2026-11-08T12:00:00Z"` (6) | `STAMP = connection.format_timestamp(...)` |
| test_scorer_rules.py | `"claude-code"`, `"codex"`, `"extract"`, `"investigate"`, `"approved"` | `limits` constants |
| test_scorer_rules.py | `"exact"` (3 incl. two expected messages) | `score.NAME_EXACT` |
| test_scorer_rules.py | `KIDS_LIST = 3` | first `seed.LISTS` entry whose audience is `AUDIENCE_KIDS` |
| test_scorer_rules.py | `41`/`40` (NAME_MAX), `61` (ACTION_MAX), `40`/`40`/`80` (LINE_MAX) | `NAME_MAX`, `ACTION_MAX + 1`, `"A" * (LINE_MAX - NAME_MAX)` and `LINE_MAX` |
| test_scorer_rules.py | exit codes `0`, `1`, `6`; `"source-missing"` | `EXIT_*`; `score.ABORT_SOURCE_MISSING` |
| test_live_labels.py | `"youtube:"` prefix and keys | `youtube.source_ref` |
| test_strict_json.py | none found (`"utf-8"`/`"utf-16-le"` build input bytes; messages are the asserted behavior) | unchanged |
| test_skill_packages.py (this step's lines) | none: `IN_SAMPLE_LABELS` is a repo path no production module owns | unchanged |

Kept on purpose, with the reason:

- Tip and label content (bucket numbers, audiences, list ids, names, actions): test data
  spelled as the committed labels file and the plan's tables spell them, not an owner's value.
- `ROOT = Path(__file__).resolve().parents[2]`: the repo's test convention (8 pre-existing
  test files).
- `ROOT / "eval" / "labels" / "in_sample.json"` (3 test files): a repo path no production
  module defines; the tests have no shared package to hold it.
- `"draft-<key>.json"`: the skill's naming (`skills/checklist-ingest/core.md:280`), no code owner.
- `"PROPOSAL_FILE_MAX_BYTES"` / `"PACKET_FILE_MAX_BYTES"` / `"LABELS_FILE_MAX_BYTES"` in
  expected messages: production passes the same name string to `read_input`; no symbol owns it.
- `list_item_insert_requires_kids_tip_on_kids_list`: the trigger's name lives only in `schema.sql`.
- `"data_dir: "`: printed inline by `cli._data_dir`, no constant.
- `"eval", "score"` argv and `HARD_IS` ids: the CLI interface and the plan's criterion ids.

## Calibration (outside the gate; each source restored byte for byte, checked by sha256)

`.build-step/r7-mutations.log`:

| Mutation | Red |
|---|---|
| `lines()` returns the lines unescaped | the unit escaping test and the integration hostile-title test |
| `abort_message()` joins unescaped abort lines | the unit escaping test |
| `--json` stdout back on `ensure_ascii=False` | the integration hostile-title test |
| the Sit Up Straight label without `"name": "exact"` | the labels-expectation test and `[IS-3 Sit Up Straight, You're Not Dead Yet]` (exit 0 instead of 1) |
| the Three-Second Mirror Check label without `on_any_of` | the labels-expectation test and `[IS-4 The Three-Second Mirror Check]` |

## Round 7 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. No pre-existing
  format debt. Check mode only; `ruff format --diff` findings in the files touched this
  round (long lines in test_cli_eval.py and test_scorer_rules.py) were fixed by hand.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1283 passed, 1 skipped, 6 deselected (the `live` tests) in 533.6 s. That is round
  6's 1273 plus 10: the garbage runs went from 7 (6 `GARBAGE` cases and the separate IS-5
  run-B test, now folded in) to 14 (+7), the hostile-title integration test (+1) and the two
  unit report tests (+2). No network request in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed
with a throwaway index): 17 files changed, 4601 insertions(+), 116 deletions(-). Round 7
touched score.py, cli.py, test_cli_eval.py (+304/-154), test_scorer_rules.py (+117/-58)
and test_live_labels.py (+4/-3); `eval/labels/in_sample.json` is unchanged.

# Round 8

Surgical round: the three round-7 Style Nits and the two FYI cleanups, nothing else. Every
behavior and test rounds 1-7 established is kept. No existing assertion changed meaning.
`git diff HEAD --stat -- tests` still lists only `tests/test_skill_packages.py` among the
pre-existing test files. Two expected-message literals in this step's own test files now
name the new owner (see NIT 1). Touched this round:

- `strict_json.py`, `sources/text.py`, `scoring/score.py`, `proposal.py`,
  `sources/packet.py` and `cli.py`;
- `tests/unit/test_strict_json.py`, `tests/unit/test_scorer_rules.py` and
  `tests/integration/test_cli_eval.py`.

## NIT 1 [style]: one owner for the regular-file gate and its wording

- **Owner:** `strict_json.is_regular_file(path) -> bool` (`stat.S_ISREG(path.stat().st_mode)`;
  an `OSError` propagates) and `strict_json.NOT_REGULAR_FILE = "it is not a regular file (a
  folder, a device or a pipe)"`. They sit beside `read_capped`, in the leaf that both
  readers already import. The module docstring names them.
- **score.py:** `read_input` is now `if not is_regular_file(path): raise
  InputFileError(NOT_REGULAR_FILE)`. Its `import stat` is gone. The grep for `S_ISREG` and
  `regular file` over `src/` found one code site in score.py, `read_input` (gate and
  wording on adjacent lines, the review's ":497" and the wording). The other hits are
  docstrings that say "not a regular file" in prose. `read_input` serves all three eval
  inputs: labels, packet and archive.
- **text.py: pure relocation (decision point 1).** `read_text_file` calls
  `is_regular_file(path)` and raises `FetchError(f"{unreadable}: {NOT_REGULAR_FILE}; give the
  path of a text file")`. This is byte-identical to its old text, because the shared clause
  was already identical in both readers. Each reader keeps its own sentence around it. Its
  `import stat` is gone. Its capped read and its `OSError` wording are unchanged (baseline,
  as round 6 left them). Its existing tests pass unedited:
  `test_text_normalize::test_only_a_regular_file_is_read` and
  `test_cli_fetch_text` (`CC_E_FETCH: cannot read the text file '<path>': it is not a
  regular file ...`).
- **This step's test literals:** `test_cli_eval.py` (the input-file folder case) and
  `test_scorer_rules.py::test_a_labels_path_that_is_not_a_regular_file_is_refused_unopened`
  now spell the clause as `strict_json.NOT_REGULAR_FILE`, following round 7's rule for
  re-spelled owners. The exact wording is still pinned independently by text.py's two
  literal tests above.
- **New unit test:** `test_strict_json.py::test_only_a_regular_file_passes_the_gate` covers a
  file (True), a folder (False) and a missing path (`FileNotFoundError`).

## NIT 2 [style]: one absolute-path idiom in score.py

`connect_read_only` builds `uri = f"{Path(os.path.abspath(path)).as_uri()}?mode=ro"`, the
idiom of `paths.py` and of `score.run`'s labels path. `Path.absolute()` no longer appears in
score.py.

- **Behavior:** the eval path passes `data_dir.db_path`, which is already
  `os.path.abspath`-ed by `paths.resolve_data_dir`. So the URI is unchanged for every caller.
- **New test**, `test_scorer_rules.py::test_the_read_only_open_takes_any_database_path_and_never_writes`:
  a database in a folder named `data dir #1 %20`, opened by an absolute path and by a
  relative one (after `chdir`). It reads `user_version == SCHEMA_VERSION`, refuses `CREATE
  TABLE` as `readonly`, and leaves the database bytes unchanged. That covers a Windows drive
  path, a space, `#` and `%`.

## NIT 3 [style]: identity (`is`) tests for every consolidated owner

The `is` assertion compares a consumer's own attribute with the owner's. A consumer that
reached its owner through the module (`strict_json.read_capped(...)`) has no attribute of its
own: `consumer.strict_json.read_capped is strict_json.read_capped` would compare the owner
with itself and could never fail. So each such call site now imports the name, as text.py
already did for `PRIMARY_ENCODING`/`UTF16_BOMS`. This is an import-style change only; the
same object is called:

| Consumer | Now imports by name | From |
|---|---|---|
| score.py | `NOT_REGULAR_FILE`, `is_regular_file`, `read_capped` | `strict_json` |
| score.py | `HASH_FIELDS` | `proposal` |
| score.py | `BUSY_TIMEOUT_PRAGMA` | `db.connection` |
| score.py | `sibling_path` | `paths` |
| score.py | `decode_source_packet` | `context` |
| proposal.py (`read_file`) | `read_capped` | `strict_json` |
| sources/packet.py (`write_packet`, `PacketSwap`) | `sibling_path` | `paths` |
| sources/text.py | `is_regular_file`, `NOT_REGULAR_FILE` (added to its existing import) | `strict_json` |

No test patches any of these names on the owner (grep `setattr(strict_json|paths|context|connection`
over `tests/` and `scripts/`), so binding the name changes no test's reach.

The tests (additive, decision point 4) each assert `consumer is not owner` (so the pair can
never be the owner with itself) and `getattr(consumer, name) is getattr(owner, name)`:

- `tests/unit/test_strict_json.py::test_each_reader_holds_the_leafs_own_object_not_a_copy`
  has 8 cases: `text.PRIMARY_ENCODING`, `text.UTF16_BOMS`, `text.is_regular_file`,
  `text.NOT_REGULAR_FILE`, `score.is_regular_file`, `score.NOT_REGULAR_FILE`,
  `score.read_capped` and `proposal.read_capped`.
- `tests/unit/test_scorer_rules.py::test_each_consumer_holds_its_owners_object_not_a_copy`
  has 5 cases: `score.HASH_FIELDS`, `score.BUSY_TIMEOUT_PRAGMA`,
  `score.decode_source_packet`, `score.sibling_path` and `packet.sibling_path`.

**Interning check.** An `is` on equal strings passes vacuously if CPython interns them. On
3.13.12, two separately compiled modules holding `"utf-8-sig"`, `"PRAGMA
busy_timeout=5000"`, the `HASH_FIELDS` tuple and the `NOT_REGULAR_FILE` text gave four
different objects, so a re-spelled copy fails the test. The mutations below confirm it on
the real modules.

Not in the review's list and not extended here: `is_whole`, `read_error_reason`,
`numbered_paths`, `is_sha256`, `open_connection` and `DataDir.has_database`. These are
already single owners that score.py reaches through the owner module; adding them would
widen the round.

## FYI-CLEANUP 1: eval score's abort line prints as inert text

- `cli.main` is where every command prints its `CheckError` line. Inside `eval score` only
  (`_is_eval(args)`), that line now goes through `score.printable`. The `CC_EVAL_ABORT`
  crash line, eval score's other abort line in the same function, does too.
- Unchanged: `CheckError.line()` and every other command's output. A post-report abort
  message is already printable, and `printable` leaves printable text unchanged, so that
  output is byte-identical too.
- **Test:** `tests/integration/test_cli_eval.py::test_a_labels_key_cannot_add_or_drive_an_abort_line`
  runs through `cli.main` with a labels file whose only key is `x` + ESC`[2J` + CR LF +
  `CC_EVAL_ABORT: forged` + BEL + CSI(0x9b)`1A` + RLO. It asserts:
  - exit 6 and empty stdout;
  - stderr splits into exactly the lines printed, and each one is `isprintable()`;
  - stderr is exactly `data_dir: <dir>` plus one line, `CC_EVAL_ABORT: labels-unusable:
    <path>: the labels has the unknown key(s) x\x1b[2J CC_EVAL_ABORT: forged\x07\x9b1A‮;
    the scorer never skips a label it cannot read`. The line break folds into a space
    (`one_line`); the controls print as escapes;
  - the data dir is not created.

## FYI-CLEANUP 2: write_report's folder failure is the promised ConfigError

- `directory.mkdir(exist_ok=True)` now runs first inside `write_report`'s `try`. A failure
  there is the same `ConfigError` (`cannot write the score file in <dir>: <error>`) as a
  failed write, and the docstring says so.
- The temporary name is computed before the `try`, as before. The `finally` still removes
  it, with `OSError` suppressed, which also covers a parent that is a file.
- **Test:** `tests/integration/test_cli_eval.py::test_an_eval_path_that_is_a_file_aborts_with_the_config_error`
  uses a `propose`-built run with `<data-dir>/eval` replaced by a file. It asserts:
  - exit 6 and empty stdout;
  - the last stderr line starts with `CC_EVAL_ABORT: CC_E_CONFIG: cannot write the score
    file in <data-dir>/eval: `;
  - no stderr line holds `Traceback` or `CC_E_INTERNAL`;
  - the file's bytes are unchanged.

  Before the fix, this run printed the crash line (`CC_E_INTERNAL: unexpected
  FileExistsError`).

## Calibration (outside the gate; each source restored byte for byte, checked by sha256)

`.build-step/r8-mutations.log` (script outside the repo). Each row shows the mutation and
what went red:

| Mutation | Red |
|---|---|
| text.py re-spells `PRIMARY_ENCODING = "utf-8-sig"` in place of importing it | `[text.PRIMARY_ENCODING]` |
| score.py re-spells `BUSY_TIMEOUT_PRAGMA = "PRAGMA busy_timeout=5000"` | `[score.BUSY_TIMEOUT_PRAGMA]` |
| packet.py re-defines `sibling_path` locally | `[packet.sibling_path]` |
| eval score prints its abort line without `score.printable` | the labels-key test |
| `mkdir` back before the `try` | the eval-path-is-a-file test |
| the URI from `Path(path).as_uri()` (no `abspath`) | `[relative]` of the read-only open test |
| the URI as `file:<abspath>` (not percent-encoded) | `[absolute]` and `[relative]` |

In every row the sibling identity cases stayed green, and `sha256sum -c` over all four
mutated sources reported OK afterwards.

## Round 8 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. There is no
  pre-existing format debt and no finding in the touched files.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1301 passed, 1 skipped, 6 deselected (the `live` tests) in 434.9 s. That is
  round 7's 1283 plus 18 new cases:
  - 8 + 1 in `test_strict_json.py` (identity cases and the gate test);
  - 5 + 2 in `test_scorer_rules.py` (identity cases and the read-only open);
  - 2 in `test_cli_eval.py`.

  No network request in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed
with a throwaway index): 17 files changed, 4780 insertions(+), 123 deletions(-). Round 7
was 4601(+)/116(-). `eval/labels/in_sample.json` is unchanged.

# Round 9

Scope: the seven round-8 findings (the first two treated as must-fix) and the two sweeps. Every
behavior rounds 1-8 established is kept: the read-only DB, the archive edit comparison (now over
three more fields), exit 6 inside `eval score`, the strict shared decoder, capped regular-file
reads, the atomic non-overwriting score file, inert printed lines, the plan-mandated names and
every Done-when test. `eval/labels/in_sample.json` is unchanged. Pre-existing test files: only
`tests/test_skill_packages.py` differs from HEAD, and its hunk at the leak check is now purely
additive (see NIT 3). Existing proposal, packet and paths tests are green and unedited.

Touched this round: `scoring/score.py`, `proposal.py`, `paths.py`, `sources/packet.py`;
`tests/unit/test_scorer_rules.py`, `tests/unit/test_strict_json.py`,
`tests/integration/test_cli_eval.py` and `tests/test_skill_packages.py`
(`tests/integration/test_live_labels.py` is byte-identical to round 8).

## NIT 1 [measurement validity]: IS-7 checks every limit of a stored tip field

**Enumeration** (limits.py read against the `tip` table and `TipDetail`):

| limits.py | Applies to a stored tip field the scorer reads? | IS-7 now |
|---|---|---|
| `NAME_MAX`, `ACTION_MAX`, `REASON_MAX` | name, action, reason | `text_problem` through `proposal.TIP_TEXT_LIMITS` (as before) |
| `LINE_SEPARATOR` / `LINE_MAX` | name + action | `proposal.line_problem` (as before) |
| `LOCATOR_MAX`, `NOTE_MAX`, `EVIDENCE_URL_MAX` | locator, note, evidence_url (**were not read**) | `text_problem` through `TIP_TEXT_LIMITS`, null allowed |
| `START_S_MAX` (and the packet's `duration_s`, its docstring's tighter bound) | start_s | `proposal.start_problem` (exposed from `_check_start`) |
| `BUCKET_IDS` | bucket | `proposal.is_bucket` / `NOT_A_BUCKET` |
| `AUDIENCES_TIP` | audience | `proposal.is_tip_audience` / `NOT_A_TIP_AUDIENCE` |
| `ID_MAX` (= `SQLITE_INTEGER_MAX`) | duplicate_of, possible_duplicate_of, list_ids | `proposal.is_id` on both references; a list id outside it names no list, so `placement_problems` already refuses it |
| `TIPS_MAX_EXTRACT` / `_INVESTIGATE` | not a field (a per-proposal count) | not IS-7; IS-2's 8-12 is stricter in-sample |
| `TIP_STATUSES` | status | the scorer does not read status |
| `PRINT_CAPACITY`, `LIST_TITLE_SEPARATOR`, `BUCKET_TITLE_MAX`, `RUBRIC_MAX`, `PLACE_MAX`, `CUE_MAX` | list / bucket / print fields | not tip fields |
| `PROPOSAL_FILE_MAX_BYTES`, source/proposal/host/transcript enums | files, source rows, envelopes | the archive cap and IS-1 (mode) already |

**Code.** `TipFields` gains `locator`, `note`, `evidence_url`, so `EDIT_FIELDS` (derived from
it) and the operator-edit abort compare them too; `read_archive` reads them (a non-string is
`archive-unusable`). `score.limit_problems(tip, duration_s)` applies every row above, each through
the rule `propose` applies; `invariant_failures` prefixes each line with the tip. Every limit is
imported from its owner, none re-spelled. Module and class docstrings updated.

**Owners exposed in proposal.py (decision point 1; messages and behavior byte-identical):**
`TIP_TEXT_LIMITS` (the six `(constant name, value)` pairs `_check_tip`, `_check_locator` and
`_check_evidence_url` spelled inline; they now read the mapping), `start_problem(value,
duration)` (`_check_start`'s bound and message, which it now calls), `is_id` (was `_is_id`),
`is_bucket` + `NOT_A_BUCKET`, `is_tip_audience` + `NOT_A_TIP_AUDIENCE` (`_check_tip` calls them).

**Tests.**
- CLI, `test_a_garbage_run_built_by_tampering_with_a_copy_fails_exactly_its_criterion`: one
  DB-copy case per limit (NAME_MAX, ACTION_MAX, LINE_MAX, REASON_MAX, LOCATOR_MAX, NOTE_MAX,
  EVIDENCE_URL_MAX, START_S_MAX, start_s at the duration, start_s below 0, BUCKET_IDS,
  AUDIENCES_TIP, ID_MAX), each through a raw connection with foreign keys off and CHECK
  constraints ignored (a hand edit), the archive edited to match. Each exits 1, reports exactly
  IS-7, names its limit in a failure line, and the original copy still passes. Out-of-window
  `start_s` values are an added copy of the tip (11 tips), so IS-2's windows stay full;
  `ID_MAX + 1` is stored as the REAL SQLite makes of it and read back as that whole number.
  `test_every_tip_text_limit_has_its_tampered_garbage_run` fails when `TIP_TEXT_LIMITS` gains a
  limit without a run.
- The known-good runs now carry a note and an evidence_url (The Clean Wallet Rule) and a locator
  (round 1), so the round trip of each new compared field is calibrated against a pass.
- Unit: one `test_is7_fails_on_each_broken_invariant` case per new limit (and an empty locator,
  a `duplicate_of` 0); `test_is7_bounds_start_s_by_start_s_max_when_the_packet_has_no_duration`;
  the known-good anchor holds a tip at every limit; three new `test_each_edited_field_aborts`
  cases; `read_archive` reads the three fields; `test_every_tip_text_limit_names_its_limits_constant_and_a_compared_field`.

## NIT 2 [bugs]: the score file without hard links

- `paths.lacks_hard_links(error)`: `PacketSwap.install`'s detection (any `OSError` from
  `os.link` but `FileNotFoundError`), relocated into the leaf both modules import.
  `PacketSwap.install` now calls it; its branches are the same (the 12 `test_packet_swap_*`
  cases, with and without hard links, are unedited and green).
- `write_report`: `FileExistsError` still takes the next name. Another `OSError` that
  `lacks_hard_links` names goes to `_publish_unlinked(temporary, target)`: claim the name with an
  exclusive create (`open("xb")`, `FileExistsError` when taken, so the loop takes the next name
  and no score file is replaced), then `os.replace` the finished temporary file over the empty
  claim, so the content still appears whole; a failed rename removes the claim. Anything else
  (`FileNotFoundError`) is the `ConfigError` as before.
- Test `test_without_hard_links_the_score_file_is_written_and_still_never_over_another`
  (through `cli.main`): a first score with hard links takes the first name; with `os.link`
  monkeypatched to raise `OSError(EPERM, "Incorrect function")` (CreateHardLink on FAT), the next
  score exits 0 with PASS (unchanged), takes the second name (the first is taken), holds exactly
  the hard-link run's bytes, leaves the first file's bytes unchanged, and leaves no temporary or
  claim behind.

## NIT 3 [test-quality]: the leak check's sanity assertion can fail again

`titles += [... in-sample label titles ...]` moved below `assert set(LABELLED_TITLES) &
set(titles[len(LABELLED_TITLES):])`, so the assertion reads the fixture titles only, exactly as
at HEAD (that line is HEAD's, unchanged); the leak check still reads the committed labels.
Proof (r9-mutations.log): with the fixture-title source filtered to drop every labelled title,
the fixed order is red; the round-8 order with the same filter stays green.

## NIT 4 [test-quality]: the crash line test holds hostile text

`test_a_crash_inside_eval_score_exits_6_not_9` raises `RuntimeError("scorer bug" + ESC[2K + LF
+ "verdict: PASS" + RLO)` from `score.collect` and reads raw stderr: exit 6, empty stdout,
stderr splits into exactly the printed lines, each `isprintable()`, and equals `data_dir: <dir>`
plus `CC_EVAL_ABORT: eval score crashed, so there is no verdict; CC_E_INTERNAL: unexpected
RuntimeError: scorer bug\x1b[2K verdict: PASS‮`. Without `score.printable` in cli.py's
crash branch it is red (r9-mutations.log).

## NIT 5 [style]: label validation imports proposal's rules

| score.py label check | Now |
|---|---|
| `_bucket` (`is_whole and in BUCKET_IDS`, "must be one of 1, 2, 3, 4") | `is_bucket`, `NOT_A_BUCKET` (same text) |
| `_expect` audience (`in AUDIENCES_TIP`, "must be one of kids+, adults") | `is_tip_audience`, `NOT_A_TIP_AUDIENCE` (same text) |
| `_list_ids` (`is_whole and >= 1`) | `is_id`: the owner adopted, so a label list id above `ID_MAX` is refused like any id that names no list; the message stays the labels' own ("a list of list ids (whole numbers from 1)") because proposal's list wording ("such as [5, 6]") differs |
| `_pair` `list_if_first` (`is_whole and >= 1`) | `is_id` (adopted; message unchanged) |
| `_soft` `at_least` (`is_whole`, a 1..n count, not an id) | `is_whole`, by name |

Two label defect cases added (`list id above ID_MAX`, `pair list_if_first above ID_MAX`).
Sibling in this step's own lines: IS-5's "expected 1" / "expected 4" now print `BUCKET_DAILY` /
`BUCKET_LEFT_OUT` (identical text).

## NIT 6 [style]: is_whole and read_error_reason pinned

Both readers now bind them by name and `test_strict_json.py::SHARED` pins
`score.is_whole`, `proposal.is_whole`, `score.read_error_reason`, `proposal.read_error_reason`
(see sweep B for the rest).

## FYI-CLEANUP

FYI-cleanup (shared fixture map) withdrawn: new module was outside the Step 15 Files list / Scope decision point 5; two module-level maps kept.

## SWEEP A: every condition of every hard IS criterion can fail

"CLI" = a garbage run through `cli.main` (propose-built, or a tampered DB copy) that exits 1 and
reports exactly that id; "unit" = `tests/unit/test_scorer_rules.py`. **New** marks this round.

| Criterion | Condition the plan names | Garbage test that makes it fail |
|---|---|---|
| IS-1 | the ref exists | ABORT first (`source-missing`): `test_a_missing_labelled_source_aborts`; IS-1 never sees it |
| IS-1 | `reviewed_at` set | ABORT first (`source-unreviewed`): `test_an_unreviewed_labelled_source_aborts` |
| IS-1 | exactly once | CLI tamper `IS-1 ref stored twice` (**new**: table rebuilt without UNIQUE, row stored again); unit `test_is1_fails_on_a_mode_other_than_extract_or_a_repeated_ref` |
| IS-1 | mode `extract` | CLI tamper `IS-1 mode not extract`; unit |
| IS-2 | at least 8 tips | CLI `IS-2 7 tips` (**new**; ten windows cannot all hold 7 tips, so the window condition fails too, still only IS-2); unit `[7]` |
| IS-2 | at most 12 tips | CLI `IS-2 13 tips` (**new**; every window held); unit `[13]` |
| IS-2 | 10/10 windows hold a tip | CLI `IS-2 an empty window`; unit |
| IS-2 | each video | the count runs break RPK-xw3SrVw, the window run LpeDuC2sQ-w |
| IS-3 | exact title, each of the 4 chapters | CLI `IS-3 <title>` x4; guard `test_each_part_of_each_labelled_placement_has_its_garbage_run` (**new**: the near-miss table equals the labels' exact-name chapters) |
| IS-3 | no tip in the window fails | unit `test_a_labelled_window_without_a_tip_fails` (at the CLI an empty labelled window also fails IS-2 and IS-4) |
| IS-4 | Accountability bucket 2 / adults / no list | CLI `IS-4 The Accountability Text bucket` (**new**), `audience`, `lists` (**new**; a list needs bucket 1, so the bucket misses too) |
| IS-4 | Sit Up Straight bucket 1 | CLI `... bucket` |
| IS-4 | Fire bucket 1 / on list 1 | CLI `... bucket` (**new**), `... lists` |
| IS-4 | Mirror Check bucket 1 / on list 5 or 6 | CLI `... bucket` (**new**), `... lists` |
| IS-4 | every part has a run | guard (**new**): `IS4_MISPLACEMENTS` keys equal the (chapter, part) pairs of the committed labels |
| IS-5 | earlier is bucket 1 | CLI `IS-5 run A/B, the earlier not daily` (**new**) |
| IS-5 | earlier on list 7 | CLI `IS-5 run A/B, the earlier off its list` (**new**) |
| IS-5 | later is bucket 4 | CLI `IS-5 run A/B, the later not left out` |
| IS-5 | later `duplicate_of` = the earlier | CLI `IS-5 run A/B, the later a duplicate of another tip` (**new**; IS-6 allows the later pair tip any target, so only IS-5 fails) |
| IS-5 | runs A and B mirror | each of the four above in both orders |
| IS-6 | no other tip has `duplicate_of` | CLI `IS-6` (an LpeDuC2sQ-w tip), `IS-6 Doorway Posture Check` (RPK-xw3SrVw, run B) |
| IS-6 | Sit Up / Doorway: `possible_duplicate_of` allowed, never `duplicate_of` | allowed: both known-good runs carry it; never: `IS-6 Doorway Posture Check` (Sit Up as a duplicate of Doorway would also break IS-4's bucket 1, so it cannot fail alone; unit `test_is6_...`) |
| IS-7 | no kids list holds an adults tip | CLI tamper `IS-7 an adults tip on a kids list` |
| IS-7 | no list item on a non-bucket-1 tip | CLI tamper `IS-7 a list item on a bucket-3 tip` (**new**) |
| IS-7 | limits hold | CLI tamper per limit (13 runs, 11 **new**, NIT 1) |
| IS-8 | name matches `/subscribe/i` | CLI `IS-8 the name` (**new**, in capitals) |
| IS-8 | action matches `/subscribe/i` | CLI `IS-8 the action` |

## SWEEP B: every consolidated symbol has an identity pin per consumer

Each consumer binds the owner's object by name, and its pin compares the consumer's attribute with
the owner's (`consumer is not owner`). All pinned values are functions, classes, tuples,
frozensets, mapping proxies, or strings with a space or hyphen, which CPython does not share
between modules (checked on 3.13.12: only identifier-like literals are shared), so a re-spelled
copy fails.

| Owner | Symbol (round) | Consumer(s) | Pin |
|---|---|---|---|
| strict_json | `decode` (4) | score, proposal (as `decode_json`: proposal has its own `decode`) | SHARED `[score.decode_json]`, `[proposal.decode_json]` **new** |
| strict_json | `JsonFileError`, `ENCODING_KINDS` (4) | score, proposal | SHARED, 4 **new** |
| strict_json | `BAD_CONSTANT` (4) | proposal | SHARED **new** |
| strict_json | `is_whole` (4) | score, proposal | SHARED **new** |
| strict_json | `read_capped` (4) | score, proposal | SHARED |
| strict_json | `read_error_reason` (5) | score, proposal | SHARED **new** |
| strict_json | `UTF16_BOMS` (4), `PRIMARY_ENCODING` (6) | text | SHARED |
| strict_json | `is_regular_file`, `NOT_REGULAR_FILE` (8) | score, text | SHARED |
| proposal | `HASH_FIELDS` (4) | score | OWNED |
| proposal | `is_sha256` (4) | score | OWNED **new** |
| proposal | `TIP_TEXT_LIMITS`, `start_problem`, `is_id`, `is_bucket`, `NOT_A_BUCKET`, `is_tip_audience`, `NOT_A_TIP_AUDIENCE` (9) | score | OWNED **new** |
| proposal | `text_problem`, `line_problem`, `TIP_OPTIONAL` (pre-existing owners IS-7 reads) | score | OWNED **new** |
| placement | `placement_problems` (4) | score | OWNED **new** |
| paths | `sibling_path` (4) | score, packet | OWNED |
| paths | `numbered_paths` (4) | score (`backup_path` is in the owner module) | OWNED **new** |
| paths | `lacks_hard_links` (9) | score, packet | OWNED **new** |
| connection | `BUSY_TIMEOUT_PRAGMA` (5) | score | OWNED |
| connection | `open_connection` (5) | score | OWNED **new** |
| context | `decode_source_packet` (6) | score | OWNED |
| paths | `DataDir.has_database` (6) | cli, score | none possible: a method called on the owner's own instance; a consumer holds no copy of it |

Not pinned: the `eval score --help` text's owners (round 7: exit codes, `CODE_EVAL_ABORT`,
`paths.EVAL_DIR`, `score.PASS`/`FAIL`): small ints and identifier-like strings, which CPython
shares, so an `is` pin could never fail.

**Proof a new pin can fail** (r9-mutations.log): `packet.py` defining its own `lacks_hard_links`
turns `[packet.lacks_hard_links]` red; `proposal.py` defining its own `is_whole` turns
`[proposal.is_whole]` red; `score.py` re-spelling `NOT_A_BUCKET` turns `[score.NOT_A_BUCKET]` red.
Each time only that case is red.

## Calibration (outside the gate; each source restored byte for byte, checked by sha256)

`.build-step/r9-mutations.log` (script outside the repo):

| Mutation | Red |
|---|---|
| IS-7 reads only name, action and reason (round 8) | CLI LOCATOR_MAX, NOTE_MAX, EVIDENCE_URL_MAX; 4 unit cases |
| IS-7 without the start_s bound | CLI START_S_MAX, at the duration, below 0; 3 unit |
| IS-7 without the bucket enum / the audience enum / the ID_MAX check | CLI BUCKET_IDS / AUDIENCES_TIP / ID_MAX; their unit cases |
| the archive reader drops `note` | both known-good runs (the stored note now differs: operator-edit abort) |
| IS-1 ignores a repeated ref | CLI `IS-1 ref stored twice` |
| write_report without the fallback | the no-hard-links test |
| the fallback claims with `open("wb")` | the no-hard-links test (it took the first, taken name) |
| the crash line without `score.printable` | the crash test |
| packet.py / proposal.py / score.py copies (sweep B) | that one pin |
| leak check fixture titles without labelled titles, fixed order / round-8 order | red / green |

## Declined (decision point 2), added to the round-6 follow-ups

- `server/models.py` and `server/demo.py` still spell the `("NAME_MAX", NAME_MAX)` pairs that
  `proposal.TIP_TEXT_LIMITS` now owns; no Step 15 line references them.

## Round 9 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. There is no pre-existing format debt and no finding in the
  touched files; the one finding while developing (a blank line in proposal.py) was fixed by
  hand.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1378 passed, 1 skipped, 6 deselected (the `live` tests) in 554.0 s, re-run after
  the FYI-cleanup was withdrawn. That is round 8's 1301 plus 77:
  - 31 in `test_cli_eval.py`: garbage runs through propose 14 -> 27, tampered runs 2 -> 17,
    the no-hard-links test and the two coverage guards;
  - 35 in `test_scorer_rules.py`: 11 IS-7 cases, 3 edited-field cases, 1 archive case, 2 label
    defects, the no-duration test, the text-limit names test and 16 identity pins;
  - 11 identity pins in `test_strict_json.py`.

  No network request in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed with a
throwaway index): 17 files changed, 5463 insertions(+), 149 deletions(-). Round 8 was 17 files,
4780(+)/123(-).
`git diff HEAD --stat -- tests` still lists only `tests/test_skill_packages.py` (10+/2-).

# Round 10

A strictly surgical round: the two round-9 Nits only. No source file changed. The only edits
are two test hunks, one in each of two test files that already existed. No new file, except
this round's logs under `.build-step/`.

## NIT [test-quality] score.py:980, the packet-unusable abort for a missing or malformed packet

`tests/integration/test_cli_eval.py`: added `test_a_missing_or_malformed_cached_packet_aborts`,
parametrized over `missing`, `not JSON` and `JSON of the wrong shape`. It sits directly after
`test_an_input_file_over_its_cap_or_not_a_regular_file_aborts_unread`, keeps that test's exact
shape, and takes its locator and rule from the same `INPUT_FILES["cached packet"]` entry. Each
case builds a run with `propose` (`build_run`) and then breaks the labelled source's cached
packet. In the first case the file is deleted, which `is_regular_file`'s `stat` turns into a
`FileNotFoundError` (an `OSError`). In the second the file holds `{not JSON`, and in the third
`[]`; both reach `PacketFormatError` through `decode_source_packet` -> `packet.decode_packet`,
the production read path. Each case runs `cli.main(["eval","score",...,"--json"])` and
asserts:

- exit 6 (`assert_aborts`);
- `abort_rules == [score.ABORT_PACKET]` (`packet-unusable`), so a crash fails the test: a crash
  has no JSON payload and names no rule;
- the case's own detail text.

Why this is a sibling test rather than new params on the existing one: that test crosses both
input files (archived proposal x cached packet), and its name says the file is refused
*unread*. A malformed packet is read, and the archive's missing case is already
`test_a_missing_archived_proposal_aborts`. Extending its `unusable` list would add archive cases
the Nit did not ask for and make the test's name untrue. Renaming it would break the
round's no-touch rule.

## NIT [style] score.py:162, `PACKET_FILE_MAX_BYTES` had no identity pin

`tests/unit/test_scorer_rules.py`: added `from checklist_care.sources import text as text_module`,
following the file's `packet as packet_module` convention. The alias is needed because `text` is
a local name in several of the file's tests. Added one test directly after OWNED's test:

```python
def test_the_packet_cap_holds_the_text_ingest_caps_object_not_a_copy() -> None:
    assert int(str(text_module.TEXT_FILE_MAX_BYTES)) is not text_module.TEXT_FILE_MAX_BYTES
    assert score.PACKET_FILE_MAX_BYTES is text_module.TEXT_FILE_MAX_BYTES
```

Why it is not an OWNED entry: OWNED's entry shape `(consumer, owner, name)` reads the *same*
name on both sides (`getattr(consumer, name) is getattr(owner, name)`). This alias has two
names, so no entry of that shape can express it. Widening the shape would touch every entry
and the test, which this round forbids. The new test uses OWNED's assertion form.

Vacuity: the cap is 16 * 1024 * 1024 = 16,777,216, far outside CPython's small-int cache
(-5..256). A re-spelled literal in score.py is therefore a different object, and `is` can
tell. The first assert keeps that true: a freshly built equal int must be another object. If
the cap ever became a small interned int, the pin would go red instead of passing silently.

## Round 10 calibration (`.build-step/r10-mutations.log`; script outside the repo, each file restored byte for byte, sha256 checked before/after)

| Mutation | Red (only these) |
|---|---|
| packet read drops `OSError` from its except tuple | `[missing]` |
| packet read drops `PacketFormatError` from its except tuple | `[not JSON]`, `[JSON of the wrong shape]` |
| score.py: `PACKET_FILE_MAX_BYTES: Final = 16 * 1024 * 1024` (literal re-spelling) | the new cap pin |
| text.py: `TEXT_FILE_MAX_BYTES: Final = 200` (an interned small int) | the new cap pin (its vacuity guard) |

Each mutated run also ran the other new cases, and they stayed green. After the sweep,
`sha256sum -c` passed for score.py and text.py.

## Round 10 gates (run from the worktree, bash, each redirected to `.build-step/gate-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. There is no
  pre-existing format debt.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1382 passed, 1 skipped, 6 deselected (the `live` tests) in 468.5 s. That is round 9's
  1378 plus 4: three in `test_cli_eval.py` and one in `test_scorer_rules.py`. No network request
  in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed with a
throwaway index): 17 files changed, 5500 insertions(+), 149 deletions(-). Round 9 was 17 files,
5463(+)/149(-); the +37 are the two test hunks.

## Round 11 (fresh worktree from master ae19208; strictly surgical)

### Setup

- `sha256sum` of `build-step-20261111T190435Z-81eec54b/payload.patch`:
  `02217d7fc4eaabe0cdb33e0320664e7416a7f76b9d22c8ca362ad2c6d11cd54f`, as expected.
- `git apply --binary` applied it cleanly. Nothing was staged or committed.
  `git status --porcelain -uall` showed exactly the 17 payload paths (8 modified, 9 untracked)
  plus `.build-step/`.
- Baseline full gate on the applied payload, before any edit (`uv run pytest`, redirected to
  `.build-step/gate-r11-baseline-pytest.log`): exit 0, **1382 passed, 1 skipped, 6 deselected**
  in 679.95 s. That matches round 10's count, so the apply is the reviewed round-10 state.

### Finding 1 (NIT, bugs): score.py's packet decode lets RecursionError and plain ValueError escape

`decode_source_packet` → `packet.decode_packet` converts only `UnicodeDecodeError` and
`json.JSONDecodeError`. `json.loads` raises `RecursionError` on JSON nested too deeply, and a
plain `ValueError` (not a `JSONDecodeError`) on an integer literal past the int max-str-digits
limit. Both reached `cli.main`'s crash handler. Exit was still 6, but the rule id was lost and
no score file was written.

Fix, inside score.py's own packet decode path only (no change to `sources/packet.py`,
`context.py` or any other module; Scope decision point 4). It follows `strict_json.py`'s
existing handler shape:

```text
            try:
                packet_sha256, packet = decode_source_packet(data)
            except packet_module.PacketFormatError:
                raise
            except (ValueError, RecursionError) as error:  # an over-long integer, deep nesting
                raise packet_module.PacketFormatError(
                    f"packet is not UTF-8 JSON: {error}"
                ) from error
```

The re-raise clause comes first because `PacketFormatError` is a `ValueError`: without it,
decode_packet's own messages would be re-wrapped. The converted error then reaches the outer
`except (OSError, InputFileError, packet_module.PacketFormatError)` unchanged, so the result is
the same `packet-unusable` abort with the same detail prefix. The wording matches
decode_packet's own `packet is not UTF-8 JSON: ...`, and wiki.py uses the same wording for this
class of error.

Tests: two cases added to the existing parametrized
`test_a_missing_or_malformed_cached_packet_aborts` (tests/integration/test_cli_eval.py), with
the same shape: through `cli.main`, exit 6, `abort_rules == ["packet-unusable"]`, and the
detail named in the last stderr line.
- `JSON nested too deeply`: the packet is `b"[" * 100_000 + b"]" * 100_000`, which is valid
  JSON and well under the 16 MiB cap. Expected text:
  `packet is not UTF-8 JSON: maximum recursion depth exceeded`.
- `an integer over the int max-str-digits limit`: the packet is
  `b"1" * (sys.get_int_max_str_digits() + 1)`. Expected text:
  `packet is not UTF-8 JSON: Exceeds the limit`. This needed `import sys` in the test module.

### Finding 2 (NIT, style): loop variable name in tests/test_skill_packages.py

`ref` was renamed to `chapter_ref` on that line only, matching test_live_labels.py. The longer
name pushes the line past 100 columns, so ruff format wraps it to three lines:

```text
    titles += [
        chapter_ref.title for chapter_ref in score.load_labels(IN_SAMPLE_LABELS).chapter_refs()
    ]
```

### Round 11 calibration (`.build-step/r11-mutations.log`; the script lives outside the repo at `C:/Users/abero/AppData/Local/Temp/r11-mutate-5ac91263.sh`; score.py restored byte for byte, sha256 checked)

| Mutation (score.py packet decode handler) | Red (only these) |
|---|---|
| `except (ValueError, RecursionError)` → `except ValueError` | `[JSON nested too deeply]`: `CC_EVAL_ABORT: eval score crashed ...; CC_E_INTERNAL: unexpected RecursionError: ...` |
| `except (ValueError, RecursionError)` → `except RecursionError` | `[an integer over the int max-str-digits limit]`: `... unexpected ValueError: Exceeds the limit (4300 digits) ...` |

In each mutated run, the other four cases stayed green. score.py sha256 was
`d39fe0f9dfa5d00e1b649ab47237a58caa163a851700e3f94ffbcd7f0a00d52f` before the sweep, after
each restore and at the end.

### Files changed this round (beyond the applied payload)

- `src/checklist_care/scoring/score.py`: the packet decode handler (+7 lines)
- `tests/integration/test_cli_eval.py`: `import sys`, the two parametrize ids and the two
  branches (+16 lines)
- `tests/test_skill_packages.py`: the loop variable rename (+2 lines from the wrap)

No new repo files. Nothing else was touched.

## Round 11 gates (run from the worktree, bash, each redirected to `.build-step/gate-r11-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. There is no
  pre-existing format debt.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1384 passed, 1 skipped, 6 deselected (the `live` tests) in 659.84 s. That is round
  10's 1382 plus the two new parametrized cases. No network request in the default gate.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed with a
throwaway index; the real index stays unstaged): 17 files changed, 5525 insertions(+),
149 deletions(-). Round 10 was 17 files, 5500(+)/149(-). The +25 are score.py +7,
test_cli_eval.py +16 and test_skill_packages.py +2.

> Orchestrator note (round 11 gates): the fences at lines 1449 and 1483 were retagged `python` -> `text`; `ruff format --check .` formats fenced Python in Markdown and flagged these indented excerpts. Report file only, outside the payload.

## Round 12 (same worktree as round 11; strictly surgical; the last round)

Start state: score.py sha256 `d39fe0f9dfa5d00e1b649ab47237a58caa163a851700e3f94ffbcd7f0a00d52f`,
the same as round 11's final. Calibration log: `.build-step/r12-mutations.log`. The script
lives outside the repo at `C:/Users/abero/AppData/Local/Temp/r12-mutate-5ac91263.sh`. Each
mutation replaces one exact text, found exactly once, runs the named tests, then restores the
file byte for byte. The sha256 is checked before and after each mutation.

### Item 1 (NIT, test-quality): the 7-tip IS-2 case now isolates the lower bound

`tests/unit/test_scorer_rules.py::test_is2_needs_8_to_12_tips` used `start_s=(i * 15) % 200` for
every count. With 7 tips the starts are 15..105, so Gamma (120-200) was empty and the case failed
on the empty-window rule, whatever the minimum was. The pre-fix mutation proves this:
`IN_SAMPLE_TIPS_MIN` 8 -> 7 left all four cases green (phase `pre` in the log).

Fix (test only): `start_s=i * 200 // (count + 1)` spreads the tips evenly over the 200 s, with a
two-line comment. The chapter indices for each count (checked with `score.chapter_index`):
7 -> `[0,0,1,1,2,2,2]`, 8 -> `[0,0,1,1,1,2,2,2]`, 12 -> `[0,0,0,1,1,1,1,2,2,2,2,2]`,
13 -> `[0,0,0,1,1,1,1,1,2,2,2,2,2]`. Every window holds a tip at every count, so only the count
rule can fail a case. The parametrize table (7/8/12/13 and their verdicts) is unchanged.
Post-fix mutation: `IN_SAMPLE_TIPS_MIN` 8 -> 7 turns `[7-False]` red (`assert True is False`).
The other three cases stay green. The file was restored byte for byte.

### Item 2 (NIT, style): one owner for the json.loads limit errors

- `src/checklist_care/strict_json.py`: a new public constant
  `JSON_LIMIT_ERRORS: Final = (ValueError, RecursionError)` with a docstring. It covers a plain
  `ValueError` for an integer over the int max-str-digits limit and a `RecursionError` for
  nesting too deep. `decode` now catches `except JSON_LIMIT_ERRORS as error:`, with the clause
  order, comment and body unchanged, so behavior is unchanged. The module docstring gains one
  sentence that names the new shared symbol.
- `src/checklist_care/scoring/score.py`: the packet decode handler (round 11) imports it from
  strict_json and catches `except JSON_LIMIT_ERRORS as error:`. The re-raise of
  `PacketFormatError` still comes first, and the body is unchanged. `sources/packet.py` and
  every other module are untouched.
- Pin: `tests/unit/test_strict_json.py` SHARED gains
  `(score, "JSON_LIMIT_ERRORS", "JSON_LIMIT_ERRORS")`, which compares score's attribute with
  strict_json's by `is`. The existing strict_json tests for an over-long integer and deep
  nesting, and round 11's two packet cases, stay green and unedited.
- Mutation: a local `JSON_LIMIT_ERRORS = (ValueError, RecursionError)` in score.py, shadowing
  the import, turns only `[score.JSON_LIMIT_ERRORS]` red. The tuple is built at import, so it is
  another object. The five packet cases stay green, which shows the behavior is the same.

### Item 3 (NIT, style): the printable() docstring

The sentence about "the sources' `_shown` helpers" is replaced with one that names no other
helper: "A report line keeps the whole string and shows what was there: it escapes such
characters, never drops them, and never shortens the text."

For the record: `sources/wiki.py:191` and `sources/youtube.py:178` each define a private
`_shown`. It drops non-printable characters and caps the length, so the old sentence was
accurate about those modules. But "the sources'" could be read as score.py's own `_shown` (a
tip id and name). The new sentence avoids the cross-reference instead of naming private
helpers of other modules.

### Item 4 (CLEANUP, style FYI): `paths.DataDir.has_database` has no consumer alias to pin

`has_database` is a method of `paths.DataDir`, not a module-level name. Every consumer calls it
on the `DataDir` instance it holds: `cli.py:347` and `:353` (`data_dir.has_database()`), and
`scoring/score.py` `run` (`data_dir.has_database()`). `grep -rn has_database src tests` finds no
`from checklist_care.paths import has_database` and no module-level `has_database` or
`_has_database` in any consumer. So no consumer attribute exists for an `is` pin to compare, and
no pin was added, as item 4 says. The round 9 SWEEP B table already lists it as "none possible".
The round 7 phrase "identity (`is`) tests for every consolidated owner" should be read with that
exception: every consolidated owner that a consumer binds by name is pinned.

### Item 5 (CLEANUP, bugs FYI): a lone surrogate in the labels is a labels abort, not a crash

strict_json accepts a `\ud800` JSON escape. The decoded labels string then holds a lone
surrogate, and with no check it reached the score-file encode. The removal mutation reproduces
this: `CC_EVAL_ABORT: eval score crashed, so there is no verdict; CC_E_INTERNAL: unexpected
UnicodeEncodeError: 'utf-8' codec can't encode character '\ud800' in position 574: surrogates
not allowed`.

Fix, in score.py's labels loading. strict_json's accepting behavior is unchanged.

- `_refuse_surrogates(document)` runs in `parse_labels` right after the top-level `_keys`, before
  any label is read. For each top-level value, or each item when the value is a list, it calls
  `holds_surrogate(item)`. That function checks every string at any depth, object keys included.
  On the first hit it raises `LabelsError`, naming the top-level key (`claim`) or the list item
  (`sources[1]`):
  `<where> holds an unpaired surrogate (a lone \ud800 to \udfff escape), which is not a
  character and cannot be written to the UTF-8 score file; write the character itself`.
  `load_labels` turns it into the existing `EvalAbortError`, so the result is exit 6, rule
  `labels-unusable`, and no score file. A top-level key is a known name by then. An unknown key
  holding a surrogate was already refused by `_keys` (labels abort, printed escaped).
  `parse_labels`' docstring adds "no string holds an unpaired surrogate".
- Single owner (Scope decision point 1). `proposal.py` already owns this exact test:
  `_holds_surrogate(*values)` over `_strings` (an iterative walk of keys and values) and
  `_SURROGATE`. Writing a regex or a walk in score.py would re-spell that owner, which is the
  same class of finding as item 2. So proposal.py is edited only to expose it: `_holds_surrogate`
  is renamed to public `holds_surrogate`, with a docstring that names both users, and its three
  call sites in `_unpaired_surrogates` are updated. This is the same exposure as `_is_id` ->
  `is_id` and `_is_sha256` -> `is_sha256` in round 4. Nothing else in proposal.py changes, and
  `_has_surrogate`, `_strings` and `UNPAIRED_SURROGATE` are untouched. No test referenced the
  old private name, and every proposal test stays green and unedited (Scope decision point 4).
  The message is the labels reader's own wording, not proposal's `UNPAIRED_SURROGATE`, because
  that ends "and submit again", which is proposal advice. Round 4 kept "save the labels as UTF-8"
  apart from proposal's `SAVE_AS_UTF8` for the same reason.
- Test through `cli.main`: a third case in the existing parametrized
  `tests/integration/test_cli_eval.py::test_an_unusable_labels_file_aborts_and_writes_nothing`.
  `[a lone surrogate in the claim]` writes the committed labels with `"\ud800"` appended to the
  claim, as the ASCII `\ud800` JSON escape (`json.dumps` default). It runs on a built data dir and
  asserts exit 6, a `CC_EVAL_ABORT:` last line holding `labels-unusable` and `claim holds an
  unpaired surrogate`, empty stdout, and an empty `eval/` folder (no score file). The test's
  comment gains one clause, and the IS-9 branch's comment moved to its own line, with the same
  text.
- Unit table (`LABEL_DEFECTS` in tests/unit/test_scorer_rules.py, two entries): `unpaired
  surrogate in a chapter title` names `sources[1]` (the list-item branch), and `unpaired
  surrogate in a nested key` (an `expect` key) names `sources[0]` (the key walk).
- Pin: `tests/unit/test_scorer_rules.py` OWNED gains `(score, proposal, "holds_surrogate")`.
- Mutations:
  - Removing `_refuse_surrogates(document)` turns exactly the three new cases red: the cli case
    with the crash line above, `[chapter title]` with `DID NOT RAISE`, and `[nested key]`, which
    falls through to `_keys`' unknown-key message. The selected cases that stay green are the
    cli `[missing]` and `[operator-only criterion id]` cases and the `[score.holds_surrogate]` pin.
  - A local `holds_surrogate` in score.py, shadowing the import, turns only
    `[score.holds_surrogate]` red.

### Item 6 (CLEANUP, style FYI): "the labels has / lacks"

`parse_labels` passes `where="the labels file"` instead of `"the labels"` to `_keys`. Every
`_keys` message then reads correctly: "the labels file must be a JSON object", "the labels file
has the unknown key(s) ...", "the labels file lacks claim, sources, criteria". The other `where`
values (`sources[0]`, `duplicate_pairs[0]`, `soft[0]`, ...) were already singular. A grep of the
whole worktree (outside `.build-step/`) found only this step's three assertions of that text, and
they are updated: `test_scorer_rules.py` (`"the labels file lacks claim, sources, criteria"`,
`match="the labels file must be a JSON object"`) and `test_cli_eval.py` (`the labels file has the
unknown key(s) ...`).

### Files changed this round (beyond the round-11 state)

- `src/checklist_care/strict_json.py`: `JSON_LIMIT_ERRORS` and its docstring, the `decode`
  handler, and one module docstring sentence (item 2).
- `src/checklist_care/scoring/score.py`: the import of `JSON_LIMIT_ERRORS` and the packet
  handler (item 2), the printable() docstring (item 3), the import of `holds_surrogate`,
  `_refuse_surrogates` and its call, and the parse_labels docstring (item 5), and
  `where="the labels file"` (item 6).
- `src/checklist_care/proposal.py`: `_holds_surrogate` -> `holds_surrogate`, its docstring, and
  three call sites (item 5, exposure only).
- `tests/unit/test_scorer_rules.py`: the IS-2 start spread (item 1), two `LABEL_DEFECTS` entries
  and the OWNED pin (item 5), and two message assertions (item 6).
- `tests/unit/test_strict_json.py`: the SHARED pin (item 2).
- `tests/integration/test_cli_eval.py`: the third parametrized labels case (item 5), and one
  message assertion (item 6).

No new repo files.

## Round 12 gates (run from the worktree, bash, each redirected to `.build-step/gate-r12-*.log` and judged on its exit code)

- `uv run mypy src`: exit 0, no issues in 39 source files.
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0, 82 files already formatted. There is no
  pre-existing format debt.
- `uv run pytest` (the full suite, web suites included through `tests/test_web_suites.py`):
  exit 0, 1389 passed, 1 skipped, 6 deselected (the `live` tests) in 617.67 s. That is round
  11's 1384 plus five new cases: the cli surrogate case, two `LABEL_DEFECTS` entries and the
  two identity pins. No network request in the default gate. The gates ran after every code
  edit and after the mutation sweep, whose restores were each checked by sha256.

Full step diff stat vs HEAD (untracked files included, `.build-step/` excluded, computed with a
throwaway index; the real index stays unstaged): 17 files changed, 5588 insertions(+),
153 deletions(-). Round 11 was 17 files, 5525(+)/149(-).
