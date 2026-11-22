# Step 15 remaining findings (round 10 of 10, budget exhausted)

Outcome: iteration budget exhausted -> BLOCKED (build-step terminal NEEDS WORK). Aggregated: NEEDS-WORK.
Gates (round 10, payload 02217d7f): typecheck ok, lint ok, format ok, pytest 1382 passed / 1 skipped / 6 deselected.
Sidecar: reviews/round-10/review-deep/2026-11-11T17-12-51.json (run state). Nit trend r1..r10: 5,3,4,2,(r5 retry),7,3,6,2,2.

## correctness: PASS (0 findings)

## bugs: NEEDS-WORK (1 findings)
- [Nit] src/checklist_care/scoring/score.py:980: decode_source_packet is json.loads(data.decode('utf-8')) inside decode_packet, which only converts UnicodeDecodeError and json.JSONDecodeError to PacketFormatError. A cached packet that is deeply nested (RecursionError) or holds an over-long integer (plain ValueError, verified not a JSONDecodeError) escapes this clause, so it is not the packet-unusable abort the module docstring and the developer claim for a malformed packet. It reaches cli.main's crash handler instead: exit is still 6, but the rule id is lost, the line reads 'eval score crashed', and no score file is written (every other abor

## security: PASS (1 findings)
- [FYI] src/checklist_care/cli.py:310: With --debug, eval score prints the raw traceback, whose exception text can quote untrusted strings (chapter titles, label keys) without score.printable, unlike the escaped crash line below it. Operator opt-in and stderr only, so observation not defect.

## test-quality: PASS (2 findings)
- [FYI] src/checklist_care/scoring/score.py:1594-1600: The no-hard-link publish path is tested for success and for never overwriting a taken name, but no test makes os.replace fail after the exclusive claim, so the claim-removal cleanup (a failed rename must not leave an empty score file under a score name) has no test that can fail. The r9 mutations log covers removing the fallback and a plain open, not this branch. Not a Done-when item; observation only.
- [FYI] tests/unit/test_scorer_rules.py:741-744: The 7-tip row also leaves the Gamma window empty (start_s tops out at 105), so it would still fail if IN_SAMPLE_TIPS_MIN were lowered to 7; this row does not isolate the lower count bound. The integration case 'IS-2 7 tips' asserts the 'holds 7 tips, expected 8 to 12' text and does isolate it, so there is no coverage gap.

## style: NEEDS-WORK (3 findings)
- [Nit] tests/test_skill_packages.py:1058: The loop variable ref binds a ChapterRef (score.py: ChapterRef carries the source ref in .ref and the chapter title in .title), so ref.title reads as if ref were a source-ref string. The sibling convention added in this same step names the loop variable chapter_ref (tests/integration/test_live_labels.py: for chapter_ref in labels.chapter_refs():). Hand-readable naming deviation, not tooling-enforced.
- [FYI] tests/integration/test_cli_eval.py:1717: The same file types its sibling mutation callback as Callable[[dict[str, Any]], None] (tests/integration/test_cli_eval.py:490: def rewrite_archive(data_dir: Path, video: str, change: Callable[[dict[str, Any]], None]) -> None:). Typing deviation within one file; observation only.
- [FYI] tests/integration/test_cli_eval.py:1188: The module constant is defined after the earlier helper that reads it (tests/integration/test_cli_eval.py:1179: target = tip_id(data_dir, TAMPERED_TIP)). It works at runtime because the read happens inside a function body; readability-only observation, outside the gate.

## plan-conformance: PASS (2 findings)
- [FYI] src/checklist_care/scoring/score.py:49: Plan 4.8 lists the operator-edit ABORT fields as name, action, bucket, audience and list_ids. The diff aborts on 13 compared fields plus the source mode plus any added or deleted tip. The docstring states this is deliberate (chapter matching reads start_s, IS-5/IS-6 the duplicate fields, IS-7 every limit-bound field), and it applies to every YouTube-applicable edit, so it is not a Done-when miss; flagged for operator awareness because a wider abort can turn a real M2/M3 run into exit 6 where the plan text would score it.
- [FYI] src/checklist_care/scoring/score.py:1237: IS-7 (plan 4.8: 'limits hold') checks every per-tip-field limit but not the per-source tip-count limits TIPS_MAX_EXTRACT/TIPS_MAX_INVESTIGATE in limits.py. IS-2's 8 to 12 tip window already fails any count near those limits and an exact IS-7-only garbage run for them would also fail IS-2, so this is consistent with the Done-when (exactly-one-id garbage runs); noted only because the stated intent says IS-7 covers every limits.py limit.

