# Step 10 dev report: editable bucket rubrics

## Result

DONE pending the full-gate line below (filled in after `uv run pytest` finished).

GATE_RESULT_PLACEHOLDER

## What was built

### API (`GET /api/buckets`, `PUT /api/buckets/{id}`)

- `src/checklist_care/server/routes_buckets.py` (new). `GET /buckets` returns
  `[{id, title, rubric, updated_at}]` by id. `PUT /buckets/{id}` takes `{title?, rubric?}`:
  - at least one field (`{}` is a 400 naming `title` and `rubric`);
  - neither may be null or empty or blank (400 `CC_E_VALIDATION`, naming the field, nothing stored);
  - a field left out keeps its value; `updated_at` becomes now; the reply is the stored bucket;
  - unknown bucket (5) is 404 `CC_E_NOT_FOUND`; a bad path id (0, `x`) is 400 naming `bucket_id`;
  - extra fields (`id`, `updated_at`) are 400 (`extra="forbid"`).
- `server/app.py` mounts the router (`ROUTERS`), which is how the integration tests reach it.
- `server/models.py`: `BucketPut`, `BucketId`, `BucketTitle` (the existing one-line tip-text rule,
  `proposal.text_problem`, bounded by `BUCKET_TITLE_MAX`) and `BucketRubric` with
  `rubric_problem()`. A rubric may span lines: LF and tab are allowed; CR, other control codes,
  U+2028/U+2029 and unpaired surrogates are refused. The text is stored exactly as given (no
  trimming or normalisation server-side), so `context` prints exactly what was PUT.
- `store/buckets.py`: `get_bucket()` and `update_bucket()` (the caller holds the transaction).
- `limits.py`: `BUCKET_TITLE_MAX = 40` and `RUBRIC_MAX = 2000`. The plan gives no caps for these
  texts. They follow the existing "bound what an untrusted request can store" caps
  (`NOTE_MAX` and similar). Because `/api/meta` serves every public `limits.py` constant, the UI
  reads both from there and does not restate them.

### Skill seam

`context.build_context` already reads `bucket_store.all_buckets(conn)` from the DB, so no change
was needed there. The round-trip test proves it end to end.

### Web (`#/buckets`)

- `web/src/pages/BucketsPage.tsx` replaces the placeholder. It loads `GET /api/buckets` and shows
  one card per bucket with a Title input, a Rubric textarea, live `n / MAX` counters, "Last changed"
  and two buttons, Save and Undo changes.
  - Save sends a `PUT` with only the changed fields, trimmed. It is disabled while nothing has
    changed or the draft is invalid (empty title or rubric, or over a limit). The page shows a hint
    for each case.
  - A server refusal appears as an alert, and the draft is kept.
  - After a save, the status line says that the skill sorts its next ingest by the new rubric.
- `web/src/meta.ts`: `MetaLimits` gains `BUCKET_TITLE_MAX` and `RUBRIC_MAX`. A new `refreshMeta()`
  refetches `/api/meta` into every mounted `useMeta()`. The Buckets page calls it after a title
  change, so the Library, Review and TipEditor bucket selects show the new title without a reload.
  A failed refresh keeps the payload already shown; a failed first load is still an error.
- `web/src/model.ts`: `Bucket` and `BucketPut` types. `web/src/app.css` adds the Buckets styles.

## Tests

- `tests/integration/test_rubric_roundtrip.py` (new, 9 tests). Everything runs through the real
  `cli.main` and `create_app`/TestClient on one data dir, with `CHECKLIST_CARE_DATA_DIR` set, so the
  CLI call is literally `cli.main(["context"])`.
  - Round trip: `context` prints the seeded rubrics. Then `PUT /api/buckets/1` with a rubric that
    holds a newline, a tab and non-ASCII text. Then `cli.main(["context"])`, run while the server is
    still up, prints exactly that rubric: the parsed JSON matches, and the JSON-encoded string
    appears in stdout. The other buckets are unchanged.
  - A title and rubric PUT reaches `context`, `GET /buckets` and `/api/meta`. A later rubric-only PUT
    keeps the title.
  - An empty rubric returns 400 `CC_E_VALIDATION`, fields `["rubric"]`, "must not be empty". The DB
    is unchanged and `context` still prints the seeded rubric.
  - Refusal matrix (blank, null, over-long, CRLF, NUL, U+2028, wrong type, empty, null, too-long and
    two-line titles, an empty body, extra fields, a lone-surrogate escape): each returns 400 with the
    right fields and stores nothing.
  - The longest title and rubric are stored. Bucket 5 returns 404; ids 0 and `x` return 400.
  - `models.RUBRIC_MAX is limits.RUBRIC_MAX`. The value is above the small-int cache, so the `is`
    check is not vacuous. Every seeded bucket passes the PUT rules.
- `web/src/pages/BucketsPage.test.tsx` (new, 8 tests). It renders the demo fixture's real
  `/api/buckets` payload against a stand-in server.
  - Edit-and-save flow: changing the rubric sends `PUT /api/buckets/1 {rubric}`, trimmed and with
    only that field. Afterwards the textarea keeps the saved text, Save is disabled again,
    "Last changed" updates and the status message appears.
  - An empty rubric or title disables Save and shows a hint, and Undo changes restores the text.
  - A server 400 shows an alert and keeps the draft. A load failure shows an alert.
  - Pure-rule tests cover `bucketPutOf` and `bucketDraftProblem`, using limits from meta.
  - Through `<App/>`: renaming bucket 2 refetches `/api/meta`, and the Library bucket filter then
    shows the new title.
  - Mutation checks run during development: removing the `refreshMeta()` call fails the rename
    test, and sending POST instead of PUT fails 3 tests.
- `web/src/App.test.tsx`: the placeholder-based "bucket titles" test now checks the real page:
  headings and rubrics from `/api/buckets`, `/api/meta` fetched once and the page fetched again on
  each visit. `#/buckets` is added to the hash-navigation loop. The old `sent[0] === /api/meta`
  ordering assertion was dropped: the page now fetches its own data, and React runs child effects
  before the App's meta effect.

## Side changes outside the plan's Files list (and why)

- `tests/integration/test_api_review.py`: `demo_payload` now includes `GET /api/buckets`, so the
  vitest fixture carries the server's real bucket payload and fails on drift.
  `web/src/fixtures/demoReview.json` was regenerated with
  `CHECKLIST_CARE_WRITE_DEMO_FIXTURE=1 uv run pytest -k demo_fixture`. It gains `buckets` and the
  two new limits.
- `src/checklist_care/db/seed.py`: `seed_database()` without `now` called a `utc_now` bound at
  import time, so the tests' `connection.utc_now` patch never reached the `serve --demo` bucket
  timestamps (the demo fixture's `updated_at` came out as wall-clock time). It now calls
  `connection.utc_now()` at call time. The only caller without `now` is `demo.populate`; `init`
  already passes `now`.
- `web/src/testing/api.ts`: `DEMO_BUCKETS`, and `demoRoutes()` answers `GET /api/buckets`.

## Gates

- `uv run mypy src`: exit 0 (34 files, no issues).
- `uv run ruff check .`: exit 0.
- `uv run ruff format --check .`: exit 0 (65 files). One finding in my own new test file was fixed
  by hand. There is no pre-existing format debt.
- `npm --prefix web run typecheck` and `vitest run` (61 tests) passed standalone before the full gate.
- `uv run pytest` (full gate, including `tests/test_web_suites.py`, which runs typecheck, vitest and
  Playwright `test:print`): see the result at the top.

## Notes for reviewers

- The plan's §4.2 table does not list `BUCKET_TITLE_MAX`/`RUBRIC_MAX`; `limits.py` documents them.
  The plan text was not edited (the orchestrator owns it).
- The client trims the title and rubric before sending; the server stores exactly what it receives.
  The round-trip contract ("context prints exactly that rubric") is therefore between the API and
  `context`.
