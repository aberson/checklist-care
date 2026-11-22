# Step 8 dev report: smoke gate

## Round 3

DONE. Both files changed. `scripts/smoke.py` is 827 lines (sha256 `59a163db...5c58`).
`tests/integration/test_smoke_script.py` went from 11 tests to 73, in 31 test functions.
Nothing from earlier rounds was undone.

### M2: the phase-5 and phase-6 checks compare content, and the anchors prove it

**The checks (smoke.py).** One helper, `difference(rows, expected, *, noun)`, is now the
only comparison in each check.
- It returns None only when `rows == expected`.
- Otherwise it names where the two sides first differ: either the count (`9 tips,
  expected 10`), or the row number and the field with both values (`tip 4/10 list_ids is
  [8], expected [7]`).
- Every key on either side counts. A field that is missing shows as `missing`, so it can
  never pass as null, and a row with an extra field differs too.
- Phase 5 (`review`) compares each pending tip, in id order, with the proposed tip on
  `PENDING_FIELDS`. That is every field of `proposal.TIP_FIELDS` (12, where the old
  check had 7) plus the envelope's `source_ref`. The projection leaves out keys the server
  did not send. Failure line: `the pending tips differ from the proposal: <difference>`.
- Phase 6 (`check_print`) compares each row of the sheet, in order, with
  `{PRINTED_FIELDS}` of the approved tip, and nothing else may be on the row. Failure
  line: `list 7 prints other rows than the approved tips: <difference>`.
- `context`'s chapters (phase 3) go through the same helper, compared with
  `asdict(packet.Chapter)`.
- A hand run of `--offline` passed in 4.6 s. All 12 tip fields round-trip through
  propose, the DB and `GET /api/tips` unchanged.

**The same-count anchors (tests).** Each one runs `main()` in-process against the real
commands and the real server, rewrites one real answer (`Tamper`), and expects exit 1.
The test checks the exact failure line, which names the drifted field and gives both
values. It also checks the cleanup: exactly one `FAIL:` line, the real server process
gone, the log tail shown, and the temp root empty.
- `test_a_pending_tip_drifted_in_one_field_...` is parametrized over
  `PENDING_TABLE = (*proposal.TIP_FIELDS, "source_ref")`. That is 13 rows: name, action,
  reason, bucket, audience, list_ids, start_s, locator, evidence_url, duplicate_of,
  possible_duplicate_of, note, source_ref. Each row changes one field of the 4th tip and
  keeps the count at 10.
  - `drifted()` picks a new value by type (str, int, bool, list, None). For a value type
    it does not know it raises, so a new field type cannot pass silently.
- `test_a_pending_list_with_the_right_count_but_other_tips_...` has two rows:
  - `two-tips-swap-ids`: the same tips, but the order by id changes. This kills an
    order-insensitive check.
  - `a-null-field-dropped`: the 4th tip loses `locator`, whose value is null. This kills a
    `.get()` (missing = null) check.
- `test_a_printed_row_drifted_in_one_field_...` is parametrized over
  `PRINT_TABLE = fields(printing.PrintItem)` (name, action).
- `test_a_print_sheet_with_the_right_count_but_other_rows_...` has three rows, all with
  the count kept at 10:
  - `another-tip-in-a-row`: one approved row is replaced by a different tip.
  - `two-rows-swapped`
  - `a-row-with-another-field`
- The three count anchors from round 2 stay, with the new messages. They, and the other
  dedicated anchors, also probe that the port refuses. The table rows skip that probe,
  because Windows takes about 2 s to report a refused port and each row already proves
  the server process exited.
- Each table is derived from the producer (proposal.py, printing.py, packet.py), never
  from smoke.py. A field added there is a new row, and a smoke that skipped it fails
  that row.

**Structural proof (scratch, not committed).**
`C:\Users\abero\AppData\Local\Temp\claude\fbe012e81828268f\4b9a0510-2d10-48e2-b0b3-2b7fd67fed7e\scratchpad\mutants.py`
writes each mutant over `scripts/smoke.py`, runs the mutant's anchors, and restores the
original bytes in a `finally`. Results are in `...\scratchpad\mutants-results.txt`. Every
mutant behaved, and the sha256 was identical after the run (`59a163db...`, byte for byte).
- The count-only mutant of `review`: all 15 same-count pending rows failed, and the count
  anchor stayed green. The control shows that a count anchor alone cannot tell.
- The count-only mutant of `check_print`: all 5 same-count rows failed, and both count
  anchors stayed green.
- Gutting `review`'s check: 16/16 red. Gutting `check_print`'s check: 7/7 red.
- Five more mutants, each turned its anchor red:
  - order-insensitive review, killed by `two-tips-swap-ids`;
  - order-insensitive print, killed by `two-rows-swapped`;
  - missing treated as null in `difference`, killed by `a-null-field-dropped`;
  - missing treated as null in the projection, killed by `a-null-field-dropped`;
  - extra keys ignored, killed by `a-row-with-another-field`.
- `REVIEWED_FIELDS = proposal.TIP_REQUIRED`: 8 red (the 6 optional-field rows, the A3
  test and the derivation test).

### A3: one definition of the checked fields

- `REVIEWED_FIELDS: Final = proposal.TIP_FIELDS` (the whole tip field list).
- `PENDING_FIELDS = (*REVIEWED_FIELDS, SOURCE_REF_FIELD)`.
- `PRINTED_FIELDS = tuple(f.name for f in fields(printing.PrintItem))`.
- Expected chapters are `asdict(chapter)`.
- `FETCH_KEYS` names fetch's three lines.

Two tests cover this:
- `test_the_checked_fields_come_from_their_producers` checks:
  - `smoke.REVIEWED_FIELDS is proposal.TIP_FIELDS`;
  - `SOURCE_REF_FIELD in proposal.ENVELOPE_FIELDS`;
  - the tables equal the smoke's lists;
  - `PrintItem.to_json()` keys equal the dataclass fields;
  - every tip the smoke proposes carries exactly `TIP_FIELDS`, in that order.
- `test_the_checked_fields_follow_their_producers_when_these_change` monkeypatches
  `proposal.TIP_FIELDS` and `printing.PrintItem` with one more field, then loads a fresh
  smoke module. Its lists must include the new field.

Mutants of this:
- A hand-typed `REVIEWED_FIELDS` with the same 12 names fails both tests.
- A hand-typed `PRINTED_FIELDS = ("name", "action")` fails the derivation test. Equality
  alone could not see it, which is why that test exists.

### Coordinator addition: every guard has a red anchor

Each guard has an anchor that runs `main()` with the in-process tamper. It expects exit 1
with that guard's failure line and that the cleanup ran. In the mutation run, each guard
gutted (`if False:`, or an `except` narrowed to `ZeroDivisionError`) turned its anchor red.

The guards the coordinator listed:
- **(a) Pre-approval empty print** (`if before:`). Anchor row
  `a-row-prints-before-approval`; message now `list 7 already prints 1 row(s) before any
  approval: [...]`.
- **(b) `/api/health` data_dir identity.** Anchor row `health-names-another-data-dir`.
- **(c) `context` packet_sha256, and chapters.**
  - `another-packet-sha256` anchors the sha check.
  - The chapters check is now field by field. Its anchors are the chapter table (title,
    start_s, end_s, from `packet.Chapter`) and `a-chapter-missing`.
  - New shape guard `chapters-not-a-list`.
- **(d) fetch lines**, on the offline fake-fetch path. `FetchSeam.rewrite` edits the real
  `cli.main` fetch output.
  - Missing line: one row each for `packet`, `source_ref`, `packet_sha256`.
  - Drifted value: one row for each of the same three keys.

The other guards, each with its own anchor:
- init exited 0 but wrote no DB;
- `context` is not JSON, is not an object, or has no skill_sha256 / no source block;
- a packet with no chapters (it would have made a vacuous pass);
- `/health`: not ok, HTTP 500, not JSON, never answers (`READY_TIMEOUT_S=1`);
- the server exits at once (`serve --port 0`, exit 2);
- a pending tip with a string id;
- `api()`'s three translations: HTTP 503, connection lost, not JSON;
- a sheet without `items` before approval;
- a printed row that is not an object;
- approve miscounts;
- a command over its time limit (tree killed);
- a command that cannot start (`init`, `serve`);
- the packet cannot be copied, or cannot be read back;
- the server outlives phase 7's kill: phase 7 fails, and the cleanup's real kill stops
  the server;
- the temp dir cannot be removed: the phase-7 and cleanup FAIL lines both appear, and the
  test removes the dir;
- an interrupt (KeyboardInterrupt in phase 5) still stops the server and removes the dir;
- the cleanup's stop, the cleanup's remove, and the log tail.

To make this possible:
- `run_cli` now reads `CLI_TIMEOUT_S` at call time (`timeout: float | None = None`), so the
  time-limit anchor can lower it.
- `fetch_fields`'s local is renamed so it no longer shadows `dataclasses.fields`.

**Deleted as unreachable by construction:** `main()`'s `if problems and code == EXIT_OK:
code = EXIT_FAIL`.
- A run that returns from `run_phases` has already stopped its server
  (`server_stopped = True`) and removed its temp dir (`removed = True`) in phase 7, so
  `cleanup()` returns `[]`.
- Any phase failure sets a non-zero code first.
- A `BaseException` re-raises out of the `finally` before that line runs.
- So `problems` is non-empty only when the code is already non-zero. A comment in `main()`
  records why.

**Not anchored (an interrupt in a blocking call):** `run_cli`'s `except BaseException:
kill_tree; raise`. Reaching it needs a Ctrl+C while `communicate()` blocks. The interrupt
path of `main()` is anchored.

### Cost

- The smoke test file now takes about 147 s alone (73 tests); it was 38 s.
- Most of that is 35 in-process runs that need a real server, about 2.3 s each (three
  CLI subprocesses and a uvicorn start). Those anchors run phases 1-4 for real, so they
  cannot be made cheaper.
- Rows that fail before the server are 0.3-0.7 s each.
- The offline smoke subprocess itself still passes in about 6.8 s.

### Round 3 gates

All four gates ran on the final code, after the mutation run had restored `smoke.py`.

- `uv run mypy src`: Success, no issues in 33 source files (`gate-typecheck.log`).
- `uv run ruff check .`: All checks passed (`gate-lint.log`).
- `uv run ruff format --check .`: 63 files already formatted (`gate-format.log`). Format
  findings in my own test file were fixed by hand; the formatter only ran in check mode.
- `uv run pytest -rs`: 749 passed, 1 skipped, 1 deselected in 188.82 s; exit 0
  (`gate-test.log`).
  - The run covers every suite under `testpaths = ["tests"]`, including
    `tests/test_web_suites.py` (17 tests).
  - The skip is the existing symlink test, `tests/unit/test_paths.py:129`.
  - The deselected test is the live one (`-m 'not live'`).
- Extra check (not a gate): `MYPYPATH=src mypy --strict scripts/smoke.py
  tests/integration/test_smoke_script.py` is clean.
- After the run, no checklist-care or smoke process was left, and no
  `checklist-care-smoke-*` directory was left in `%TEMP%`.
- No pre-existing format debt.

## Round 2

DONE. Round 2 changed only `tests/integration/test_smoke_script.py`, which grew from 401
to 553 lines and from 8 to 11 tests. `scripts/smoke.py` is unchanged, byte for byte
(sha256 `09e9082f9e89f9f727320d7b80ade1f6624b324013cd57ea53b9065a735cfbe1`).

- **M1 (bad review and print data).** The new tests wrap the smoke's one HTTP helper
  (`smoke.http_json`), so every request still goes to the real server. The wrapper rewrites
  one matching answer (`Tamper` + `tamper_with`) and counts it (`tamper.applied == 1`). Each
  test runs `main(["--offline", FIXTURE])` in-process against a real server.
  - `test_a_pending_list_missing_a_proposed_tip_fails_the_review_and_cleans_up`: the
    `GET /tips?status=pending` answer loses one of its 10 tips. Expected stderr line:
    `FAIL: phase 5/7 review: the 9 pending tips differ from the 10 proposed; ...`, with no
    `phase 5/7 review: ok` line.
  - `test_a_print_sheet_other_than_the_approved_tips_fails_the_smoke_and_cleans_up`, run
    twice:
    - `missing-an-approved-tip`: the sheet loses one of the approved tips.
    - `with-an-unapproved-tip`: the sheet gains a tip nobody approved.
    - Only the `GET /print/lists/7` after the approval is rewritten, so the empty sheet
      before approval still passes. Expected stderr line:
      `FAIL: phase 6/7 print: list 7 prints 9|11 rows, expected the 10 approved tips; ...`.
  - All three use the shared `assert_failed_and_cleaned_up`, which checks:
    - exit 1, and the failure line on stderr;
    - no `PASS`;
    - the recorded real server `Popen` has exited and its port refuses;
    - the server log tail was shown;
    - the work dir was under the test's temp root, and that root is now empty.
  - The round-1 injected-`RuntimeError` test uses the same helper and the same new
    `started_servers` fixture. That fixture records each server and kills any left over at
    teardown, as the old inline `finally` did.
- **Mutation proof.** Each check was gutted (`if False:`), the new tests were run, and
  `smoke.py` was restored byte for byte; the sha256 matched afterwards.
  - Gutting `review`'s `if served != expected:` makes the pending test fail. The smoke then
    approves 9 tips and reaches phase 6.
  - Gutting `check_print`'s `if items != expected:` makes both print tests fail. The smoke
    then prints `PASS`, which is exactly the hole M1 named.
- **A1.** `refuses()` now retries while the connect times out or hits another `OSError`. A
  refusal returns True, and an accepting port returns False at once. The retries stop at
  `REFUSE_DEADLINE_S` (20 s), with probes of `REFUSE_PROBE_S` (5 s) every 0.2 s, and the
  answer is then False. A scratch check (not committed) confirmed both paths:
  - a timeout, then a reset, then a refusal gives True after 3 probes;
  - a timeout that lasts past the deadline gives False.
- **A2.** The two offline tests of the `--live` path are now
  `test_the_fetch_mode_fetches_the_in_sample_video_and_runs_the_whole_cycle` and
  `test_the_fetch_mode_reports_a_fetch_error_as_exit_4`. The reviewer's example
  (`test_offline_live_path_...`) would still contain `live`, which `pytest -k live` matches.
  No test name in this file contains `live` now.
- **Not changed (advisories with no action required).** `processes.py` still launches
  `taskkill` by bare name.

### Round 2 gates

- `uv run mypy src`: Success, no issues in 33 source files.
- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 63 files already formatted.
- `uv run pytest -rs`: 687 passed, 1 skipped, 1 deselected in 80.83 s; exit 0.
  - The skip is the existing symlink test, `tests/unit/test_paths.py:129`.
  - This run includes `tests/test_web_suites.py`.
- The smoke test file alone: 11 passed in 38.5 s.
- No pre-existing format debt.

## Round 1

Two new files, no production module changed.

- `scripts/smoke.py` (774 lines, new)
- `tests/integration/test_smoke_script.py` (401 lines in round 1, new)

## What `scripts/smoke.py` does

`--live` and `--offline PACKET` are a required, mutually exclusive pair (argparse; a usage
error exits 2 and creates nothing). Each run makes `tempfile.mkdtemp(prefix="checklist-care-smoke-")`
and prints `work_dir: <path>`. Its seven phases each print `phase N/7 <name>: ok (...)`:

1. **init**: `python -m checklist_care.cli --data-dir <tmp>/data init` as a subprocess. It also
   checks that the DB file exists.
2. **packet**:
   - `--offline`: `read_packet` the file, copy it byte for byte to
     `packet.packet_path(data_dir, ref)`, then call `store/sources.upsert_fetched` in-process
     inside `connection.transaction`, with the same fields `cli._cmd_fetch` passes
     (`revision=youtube.revision(packet)`).
   - `--live`: a real `fetch` subprocess of `youtube.watch_url("LpeDuC2sQ-w")` with a 180 s
     limit. It parses fetch's `packet:` / `source_ref:` / `packet_sha256:` stdout lines and
     checks each against `packet_path`, `source_ref` and the file's sha256.
   - Fetch exit 4 makes the smoke exit 4. The message names `CODE_FETCH` and the M1
     `LIVE: BLOCKED-EXTERNAL` rule.
3. **propose**:
   - `context --source-ref` runs as a subprocess. Its `source.packet_sha256` must equal the
     packet file's sha256, and its chapters must equal the packet's.
   - The smoke then builds one tip per chapter: the chapter title verbatim as the name, a fixed
     action, bucket 1, `adults`, list 7, `start_s` from the chapter. `skill_sha256` and
     `packet_sha256` are copied from `context`.
   - It writes the proposal as UTF-8 without BOM, with LF, and runs `propose` as a subprocess,
     which applies the real validation.
   - Enum and limit values come from `limits.py`; the schema comes from
     `proposal.PROPOSAL_SCHEMA`.
4. **serve**:
   - Takes a free loopback port and prints `port: N`.
   - Spawns `sys.executable -m checklist_care.cli --data-dir D serve --port N` with
     `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` (and `start_new_session` off Windows).
     Server output goes to `<work>/server.log`.
   - Polls `/api/health` (60 s) and requires `ok: true` and `data_dir` equal to this smoke's
     data dir, so it cannot talk to a stranger on that port.
5. **review**:
   - `GET /api/tips?status=pending` must match the proposed tips field by field (name,
     action, reason, bucket, audience, list_ids, start_s, source_ref).
   - `GET /api/print/lists/7` must be empty before approval.
   - `POST /api/review/approve` must answer `{"approved": n}`.
6. **print**: `GET /api/print/lists/7` items must equal `[{name, action}]` of the approved tips,
   in proposal order.
7. **stop**:
   - `processes.kill_tree(proc)` (the shared helper: `taskkill /T /F /PID <pid>`, window-less),
     then `proc.wait(10)`.
   - The port must refuse connections (5 s connect probe; polls up to 10 s).
   - The temp dir is then removed, with 10 tries 0.5 s apart.
   - Last line is `PASS in <s> s`.

**Failure handling.** Any `SmokeFailure` or unexpected exception prints
`FAIL: phase N/7 <name>: ...` and the last 30 lines of the server log to stderr. `cleanup()`
then always stops the server, if one was started and not yet stopped, and removes the temp
dir. On Ctrl+C the cleanup runs in a `finally`. Cleanup problems print as
`FAIL: cleanup: ...` and turn a pass into exit 1. HTTP requests use stdlib urllib with an
empty ProxyHandler, so `HTTP_PROXY` cannot divert loopback calls. Every CLI child gets
`CREATE_NO_WINDOW`, and a timeout or interrupt kills its process tree.

A local hand run of the offline smoke passed in 8.1 s and left no `checklist-care-smoke-*`
directory in `%TEMP%`.

## Tests (`tests/integration/test_smoke_script.py`, 8 tests, about 25 s)

- **Subprocess runs** of `python scripts/smoke.py` with cwd at the checkout root, no console
  window, and `TMP`/`TEMP`/`TMPDIR` pointed at the test's own folder. Each test asserts that
  `work_dir` was in that folder (so the "empty" check proves something) and that the folder
  is empty afterwards.
  - `--offline tests/fixtures/packet_youtube_synthetic.json`: exit 0, under 120 s, all 7
    phases in order, the port refuses afterwards, nothing left behind.
  - Red anchor: a packet with one chapter title over `NAME_MAX` gives exit 1 at phase 3.
    stderr carries `propose` exit 5 with `CC_E_PROPOSAL`/`NAME_MAX`, the server never started,
    nothing is left.
  - A missing packet gives exit 1 at phase 2, nothing left.
- **In-process `main()` runs** (`scripts/smoke.py` loaded with importlib and registered in
  `sys.modules`):
  - A failure injected at phase 6 (`check_print` raises RuntimeError) after the real server
    started: exit 1, the recorded server `Popen` has exited, the port refuses, the server log
    tail was shown, and the temp dir is gone.
  - The port checks have a red anchor: a live listener is not "refused", and
    `check_port_refuses` raises on it.
  - `--live`/`--offline` are required and exclusive (exit 2, no temp dir).
  - `--live`, offline: the smoke's `fetch` call goes to `cli.main` in-process with the two
    YouTube seams faked from the fixture, so the smoke parses the real `fetch` output
    format. All other commands and the server stay real subprocesses. The run passes all
    7 phases, and the fetched URL is the in-sample watch URL.
  - `--live` with a fetch error: exit 4, `CC_E_FETCH`, the `LIVE: BLOCKED-EXTERNAL` hint, and
    no temp dir left.

The real network run (`uv run python scripts/smoke.py --live`) is only in manual step M1. No
test in the default gate touches the network.

## Gates (final code)

- `uv run mypy src`: Success, no issues in 33 source files.
- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 62 files already formatted.
- `uv run pytest -rs`: 684 passed, 1 skipped, 1 deselected in 71.08 s; exit 0.
  - The skip already existed: `tests/unit/test_paths.py:129` needs symlink rights on Windows.
  - The deselected test is the live test (`-m 'not live'`).
  - This run includes `tests/test_web_suites.py`.
- Extra check (not a gate): `MYPYPATH=src mypy --strict scripts/smoke.py` is clean.

## Notes

- No pre-existing format debt.
- `kill_tree` runs `taskkill /T /F /PID <pid>`, the same command as the plan's
  `taskkill /PID <pid> /T /F` with the flags in another order. I reused it as instructed
  instead of writing a second kill.
- The smoke keeps chapter titles verbatim, as Step 8 says. A live chapter title over
  `NAME_MAX` would therefore fail at `propose`, which is the drift this gate exists to catch.
