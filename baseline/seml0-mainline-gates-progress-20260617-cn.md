# SemL0 G1/G2/G3 Mainline Gates

Last update: 2026-06-17 13:38 CST

## Current Remote Authority

```text
Authoritative root: /data/WorkSpace/lsmgraph-rs
Supersedes as current status: baseline/seml0-mainline-gates-progress-20260616-cn.md
W14 final run: remote-logs/w14-sf100-minimal-reuse-20260617-codex1
W14 DONE: 2026-06-17T13:05:29+08:00
Active heavy SF30/SF100 runner: none confirmed
```

## Gate Summary

| Gate | Name | Status | Decision |
|---|---|---|---|
| G1 | W14 necessity / reuse-store SF100 evidence | PARTIAL GO | W14 run completed and digest correctness passed; schema-vs-pruned evidence is usable; composite-over-edge-type-only evidence is not strong enough for S3 implementation. |
| G2 | C10 latency attribution | UNLOCKED / NOT STARTED | Start offline attribution from existing W6/W8/W14 logs first; do not start a heavy runner until offline evidence is insufficient. |
| G3 | S0 semantic dilution | BLOCKED BY G2 | No S0 runner until C10 gives a verdict. |
| S1/S2/S3 implementation | Full S3 / new disk format / L1+ scheduler | BLOCKED | Do not implement S3 now. Keep full S3 as backlog/future work. |

## W14 Result

```text
Run: remote-logs/w14-sf100-minimal-reuse-20260617-codex1
DONE: 2026-06-17T13:05:29+08:00
property-required digest compares: 3/3 PASS, checked=5000, mismatches=0
degree-class digest compares: 3/3 PASS, checked=5000, mismatches=0
fresh SF100 rebuild matrix: not run; still FALLBACK / do not run
```

Metric interpretation:

```text
property-required:
  useful as correctness parity, not a positive performance signal in this run.

degree-class:
  schema elapsed_ms_sum=776331, filter_passed_segments=1072533
  edge-type-only elapsed_ms_sum=6071, filter_passed_segments=7349
  budg-b64 elapsed_ms_sum=5973, filter_passed_segments=7351
  semantic elapsed_ms_sum=6070, filter_passed_segments=7383

Conclusion:
  Strong schema-vs-pruned gap.
  Weak composite-vs-edge-type-only separation.
```

## Current Policy

```text
Do:
  - update W14/W10 evidence docs from the completed remote run
  - start C10 offline attribution from existing JSON/logs/manifests
  - decide S0 only after C10 verdict

Do not:
  - restart fresh SF100 rebuild matrix
  - start full S3
  - implement S1/S2
  - add new disk format, L1+ container segments, or scheduler work
```

## Next Minimal Action

```text
C10 offline attribution:
  inputs: W6/W8/W14 JSON, stderr open-phase logs, runner nohup.out, manifest.tsv
  output: baseline/c10-latency-attribution-summary-20260617-cn.md
  verdict: GO / FALLBACK / NO-GO
```
