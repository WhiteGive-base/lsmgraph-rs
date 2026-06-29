# W14 Final Verdict

Last update: 2026-06-17 13:50 CST

## Verdict

```text
W14 verdict: GO for SF100 reuse-store bench-only correctness evidence
G1/S3 verdict: PARTIAL / not sufficient to start S3 implementation
Fresh SF100 rebuild matrix: FALLBACK / do not run
S3 full implementation: BLOCKED
Immediate next step: update remote gate status, then do C10 offline latency attribution before any S0/S1/S2 work
```

## Evidence Used

| Item | Status | Evidence |
|---|---|---|
| P0 compare optimization | PASSED | `w14-mainline-to-s3-progress-20260617-cn.md`; digest compare path works without reopening engines for standalone neighbor compare. |
| P0b import-many optimization | PASSED / SF1 smoke | `remote-logs/w14-import-many-sf1-smoke-20260617-codex1`; SF1 schema-vs-semantic `checked=200 mismatches=0`. |
| SF100 type-only reuse-store gate | PASSED | `remote-logs/w14-sf100-type-only-reuse-cachedplan-20260617-codex2`; schema-vs-edge-type-only/budg-b64/semantic all `checked=100 mismatches=0`. |
| SF100 minimal reuse-store matrix | PASSED | `remote-logs/w14-sf100-minimal-reuse-20260617-codex1`; DONE at `2026-06-17T13:05:29+08:00`; `PASSED` marker written at 2026-06-17 13:50 CST after digest JSON verification. |

## SF100 Minimal Matrix Result

```text
property-required:
  schema-vs-edge-type-only: checked=5000 mismatches=0 verdict=PASS
  schema-vs-budg-b64: checked=5000 mismatches=0 verdict=PASS
  schema-vs-semantic: checked=5000 mismatches=0 verdict=PASS

degree-class:
  schema-vs-edge-type-only: checked=5000 mismatches=0 verdict=PASS
  schema-vs-budg-b64: checked=5000 mismatches=0 verdict=PASS
  schema-vs-semantic: checked=5000 mismatches=0 verdict=PASS
```

## S3 Gate Interpretation

```text
W14 is complete as a remote run, but it is not a full S3 green light.

The SF100 reuse-store matrix proves correctness parity and shows that pruned variants can avoid the schema baseline cliff.
It does not prove that composite semantics materially outperform edge-type-only on the completed W14 scenarios.
```

Observed remote metric summary:

```text
property-required:
  all variants reported one aggregate benchmark row with zero candidate/body/read metrics.
  Treat this as correctness evidence, not as a positive performance signal.

degree-class:
  schema elapsed_ms_sum=776331, filter_passed_segments=1072533
  edge-type-only elapsed_ms_sum=6071, filter_passed_segments=7349
  budg-b64 elapsed_ms_sum=5973, filter_passed_segments=7351
  semantic elapsed_ms_sum=6070, filter_passed_segments=7383

Interpretation:
  edge-type-only, budg-b64, and semantic are all close on degree-class.
  This supports pruning versus schema, but not a strong claim that richer composite semantics beat edge-type-only.
```

S3 decision:

```text
Do not start complete S3.
Do not start S1/S2 implementation.
Do not add new disk format, L1+ container segment, or compaction scheduler work.

Allowed next work:
  G2/C10 offline attribution from existing W6/W8/W14 logs.
  Only after C10 has a verdict, decide whether S0 semantic-dilution diagnosis is worth a small, targeted run.
```

Local evidence mirror:

```text
w14-result-check/w14-sf100-minimal-reuse-20260617-codex1
```

Progress docs updated:

```text
baseline/w14-mainline-to-s3-progress-20260617-cn.md
baseline/w14-compare-optimization-validation-20260616-cn.md
```

## Claim Boundary

Allowed:

```text
SF100 reuse-store bench-only evidence supports correctness parity for property-required and degree-class scenarios across edge-type-only, budg-b64, and semantic variants.
The W14 digest path is usable for final validation because all compared digests matched and no standalone neighbor-compare fallback was needed.
The fresh rebuild cost boundary is now explicit: import-many helps reduce repeated CSV scans, but it does not remove per-store CSR/sidecar materialization cost.
```

Not allowed:

```text
Do not claim a fresh SF100 rebuild matrix completed.
Do not claim S3 implementation has started.
Do not claim stable latency superiority from W14 alone.
Do not hide the expensive schema/semantic open phases; schema open was about 583-595s and semantic open about 675-744s in the SF100 reuse-store run.
```

## Next Step

```text
W10 should consume this as measured reuse-store evidence.
W10 still needs claim labeling, table rewrite, appendix/artifact integration, and a final submission-ready/not-ready decision.
W7 remains a formal gap unless a dedicated runner is defined and completed; otherwise write it as controlled evidence/limitation.
```
