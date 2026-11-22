# build-step remaining findings — checklist-care Step 8 (#8), run build-step-20261109T061643Z-560ecdfc

Outcome: audit required (stop-and-audit, Step 9 rule 1) after 3/10 rounds.
Same material shape flagged by the test-quality arm in rounds 1, 2 and 3 at different sites:
a guard in scripts/smoke.py can be removed while every test stays green.

## Audit finding (one, structural)

Call-site / side-effect mutation sweep on a copy of the reviewed tree (40 mutants; control 73 passed):
22 red, 18 green. Plan-required survivors (real holes):
- smoke.py:733 phase-7 check_port_refuses(port) call deleted -> green (plan Step 8: 'Assert the port refuses connections')
- smoke.py:658 check_port_refuses retry deadline removed -> green
- smoke.py:646 / :648 port_state TimeoutError / OSError classified as 'refused' -> green
- smoke.py:667 remove_tree retry loop range(1) -> green; smoke.py:677 exists-check always returns -> green
- smoke.py:274 kill_tree in run_cli TimeoutExpired branch -> green (orphan recreates temp dir ~6 s later)
- smoke.py:279 kill_tree in run_cli BaseException branch -> green
- smoke.py:494 serve creationflags -> 0 -> green; smoke.py:267 run_cli NO_WINDOW -> 0 -> green
Weaker (backstopped, message differs only): expect_ok on init (300) and context (389).
Not plan-required / equivalent (delete or justify): run.port (715, dead field); server_stopped/removed flags (731, 734, 756, 762, 754).

Common root: every red anchor asserts the FAIL text / exit code of a check fed bad data through main();
nothing asserts that a guard or safety side effect EXECUTED on the pass path or the failure path, and the tests
never create the conditions (late port refusal, rmtree failing N-1 times, a child that outlives a timeout,
Popen creationflags) under which those guards are observable.

Structural regression-test proposal: a guard registry plus ONE parametrized in-process test
test_every_guard_is_reached[guard]. Guards are looked up as module attributes at call time; the test runs
main(['--offline', fixture]) and the existing tamper/timeout/interrupt scenarios with recording spies that call
the real function, and asserts per guard: called on the pass run and on its named failure run, with the expected
arguments (creationflags include NO_WINDOW / NEW_PROCESS_GROUP), plus a real-state check (child pid dead, port refuses,
dir gone). Add the three missing scenario seams: port_state non-refused then refused; rmtree failing N-1 times then
succeeding; a sleeping child for the timeout test (assert its pid is gone). Deleting any registered call site then
fails by guard name. Keep the call-site mutation driver as the periodic proof that the registry is complete.
Evidence: C:/Users/abero/AppData/Local/Temp/claude/fbe012e81828268f/4b9a0510-2d10-48e2-b0b3-2b7fd67fed7e/scratchpad/audit/audit-results.md (driver audit_mutants.py, raw rows audit-raw.tsv beside it).

## Round 3 (last round) per reviewer — code lane

- correctness: PASS (0 material, 0 advisory)
- bugs: PASS (0 material, 0 advisory)
- security: PASS (0 material, 0 advisory)
- test-quality: NEEDS-WORK (2 material, 8 advisory)
  - [material] tests/integration/test_smoke_script.py:1151 — The CLI-timeout anchor passes with the tree kill removed (test name says 'is_killed')
  - [material] scripts/smoke.py:733 — Phase 7's own port-refusal assertion has no anchor through its production caller
  - [advisory] tests/integration/test_smoke_script.py:957 — Temp-dir removal retries are untested; the removal anchor passes with a single attempt
  - [advisory] scripts/smoke.py:279 — run_cli's interrupt path (kill tree, re-raise) has no test
  - [advisory] tests/integration/test_smoke_script.py:611 — Port probe repeated on the same cleanup path (about 6.5 s)
  - [advisory] tests/integration/test_smoke_script.py:646 — Print right-count rows 'another-tip-in-a-row' and 'two-rows-swapped' assert the identical failure line
  - [advisory] tests/integration/test_smoke_script.py:710 — The print-count rows share one branch, and the count branch is exercised four times
  - [advisory] tests/integration/test_smoke_script.py:1453 — The three fetch no-key-line rows exercise one comprehension, and FETCH_KEYS is pinned by copy-vs-copy
  - [advisory] tests/integration/test_smoke_script.py:503 — Per-field drift rows run a full serve cycle each (about 58 s), and a shared server would keep coverage at lower cost
  - [advisory] tests/integration/test_smoke_script.py:212 — Assertions that cannot fail on their own
- style: PASS (0 material, 4 advisory)
  - [advisory] tests/integration/test_smoke_script.py:922 — A new test name contains 'live', the substring reserved for -m live network tests
  - [advisory] tests/integration/test_smoke_script.py:472 — Test comment cites an ephemeral build-step review id ('A3') not a durable reference
  - [advisory] scripts/smoke.py:24 — Module docstring splits its numbered 'seven phases' list with an unrelated paragraph
  - [advisory] scripts/smoke.py:206 — Helper `shown()` is public and shadowed by local `shown` strings; committed modules name such helpers privately

## Gates (round 3 payload)
- tests: 749 passed, 1 skipped, 1 deselected (full suite, 192.6 s); typecheck ok; lint ok

## Round history
- Round 1: NEEDS WORK — test-quality: no red anchor for the smoke's drift checks (phase-6 print, phase-5 pending-vs-proposed)
- Round 2: NEEDS WORK — test-quality: tamper anchors corrupt row counts only; field-level drift checks removable green
- Round 3: NEEDS WORK — test-quality: run_cli timeout kill and phase-7 check_port_refuses removable green
