# Step 15 remaining findings (round 12 of 12, budget exhausted)

Outcome: iteration budget exhausted -> BLOCKED (build-step terminal NEEDS WORK). Aggregated: NEEDS-WORK.
Gates (round 12, payload 2b587bcd): typecheck ok, lint ok, format ok, pytest 1389 passed / 1 skipped / 6 deselected.
Sidecar: reviews/round-12/review-deep/2026-11-11T19-03-17.json (run state). Nit trend r1..r12: 5,3,4,2,(r5 retry),7,3,6,2,2,3,1.

## correctness: PASS (1 findings)
- [FYI] src/checklist_care/cli.py:337: The stated intent says every printed line of eval score passes through score.printable, but this preamble line (first stderr line of eval score too) prints the operator-supplied data-dir path raw. The value is operator input, not untrusted metadata, so this is awareness only; the hostile-input tests use printable tmp paths and would not catch a control character in the path.

## bugs: PASS (1 findings)
- [FYI] src/checklist_care/cli.py:337: Inside eval score the first stderr line prints the operator-supplied data dir raw, while every other eval line (report, abort, crash) goes through score.printable. A data dir path holding an ESC sequence or line break (a hostile directory name or env value) reaches the terminal unescaped, so 'every printed line passes through printable' is true of report, abort and crash lines but not of this one. Low impact: the value is the operator's own argument and the line is the cross-command contract; surfaced for awareness only.

## security: PASS (0 findings)

## test-quality: NEEDS-WORK (1 findings)
- [Nit] src/checklist_care/scoring/score.py:1627-1631: keep-with-gap: the no-hard-links publish path documents 'A failed rename removes the claim' (and write_report promises no file under a score name after a failed write). The only failure test (DiskFullWriter) fails at temporary.write_bytes, before any link or claim, and the no-hard-links test only exercises success and the taken-name retry. No test makes os.replace fail after the exclusive-create claim, so deleting the target.unlink() cleanup (leaving an empty score-<ts>.json that a later run would skip over and a reader could take for a result) keeps the whole suite green; the r11/r12 mutation

## style: PASS (5 findings)
- [FYI] src/checklist_care/scoring/score.py:1344: Minor naming: the eight hard criterion functions in this file are named _is1 through _is8 (score.py:1147 to 1338), and the soft one breaks that pattern with an underscore (registered at score.py:1425 as Criterion("IS-S", SOFT, _is_soft, _lacks_soft)). Observation only; no behavior effect.
- [FYI] tests/unit/test_scorer_rules.py:992: The code-quality convention asks for identity (is) pins. For the tip text limits this is not attainable: NAME_MAX (40), ACTION_MAX (60), LINE_MAX (80) and REASON_MAX (110) in limits.py are small ints that CPython caches, so an is check would also pass for a re-spelled literal. The equality check here only validates the constant-name string against the limits.py object, which is its intended purpose. Recorded as a limit of the identity convention, not a defect.
- [FYI] tests/integration/test_cli_eval.py:1142: The same test module defines raw_connection (test_cli_eval.py:1131) for direct edits of the copy's database, and tip_id (line 481) and tamper_mode (line 1142, also line 1152) open sqlite3.connect directly. tamper_mode needs no foreign-key or CHECK bypass, so the behavior is fine, but the file mixes two spellings of the same open. Surrounding-convention note only.
- [FYI] tests/test_skill_packages.py:95: The committed labels path is now spelled in three test modules added or changed by this step (also tests/integration/test_cli_eval.py:102 and tests/unit/test_scorer_rules.py:59). No production module owns that path, so this is not a point-1 violation; it is a drift surface worth one shared test constant if a later step touches it.
- [FYI] src/checklist_care/strict_json.py:95: read_capped signals an over-cap file by returning None rather than raising, which differs from the raise-style refusals of the surrounding readers. Both callers (proposal.read_file and scoring/score.py read_input) test for None and raise their own refusal, and the docstring states the contract, so the behavior is correct. Observation on the error-handling convention only.

## plan-conformance: PASS (1 findings)
- [FYI] src/checklist_care/scoring/score.py:49-50: The operator-edit ABORT compares twelve tip fields plus the source mode and an added/deleted-tip count, wider than the plan's list (name, action, bucket, audience, list_ids). It is documented in the module docstring and stays within the ABORT intent (a changed value would be scored as the skill's work); noted for operator awareness, no action required.

