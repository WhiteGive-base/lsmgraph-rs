# W14 SemL0 必要性实验 Summary

- Run root: `remote-logs/w14-semantic-necessity-sf30-smoke-20260615-codex4`
- Verdict: **FALLBACK**
- Reason: compare 正确且 telemetry 有效，但没有稳定证明 composite semantics 优于 edge-type-only；论文只能把 edge-type-only 写成 SemL0 的一维特例，必要性 claim 要收窄。

SF100 formal follow-up:

- Run root: `remote-logs/w14-semantic-necessity-sf100-formal-20260616-codex1`
- Status: **FAILED by stop condition**
- Failure: `edge-type-only` SF100 import exceeded `IMPORT_TIMEOUT_SECONDS=10800` and wrote `FAILED=2026-06-16T09:25:04+08:00 unexpected exit status=124`.
- Evidence boundary: no valid SF100 W14 composite matrix was produced; do not write SF100 W14 composite advantage claims.
- Current W14 paper boundary remains the SF30 **FALLBACK** result below.

## Table X: semantic necessity matrix

| Scenario | Variant | Candidate L0 | Body reads | Body bytes | Read bytes | Elapsed ms mean | p99 us mean | Pruned total | Non-edge pruned | Compare |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| property-required | schema | 290 | 3 | 1,248 | 8,496 | 1 | 2,000 | 1,076 | 25 | anchor |
| property-required | edge-type-only | 0 | 0 | 0 | 0 | 0 | 75 | 1,078 | 9 | checked=50, mismatches=0 |
| property-required | budg-b64 | 290 | 3 | 1,248 | 8,496 | 1 | 2,000 | 1,143 | 18 | checked=50, mismatches=0 |
| property-required | semantic | 320 | 3 | 1,248 | 7,448 | 3 | 5,000 | 2,322 | 287 | checked=50, mismatches=0 |
| type-only | schema | 3,371 | 50 | 4,544 | 107,208,616 | 241 | 150,000 | 0 | 0 | anchor |
| type-only | edge-type-only | 3,371 | 50 | 4,544 | 107,585,160 | 209 | 150,000 | 0 | 0 | checked=50, mismatches=0 |
| type-only | budg-b64 | 3,360 | 50 | 4,544 | 107,192,104 | 210 | 150,000 | 0 | 0 | checked=50, mismatches=0 |
| type-only | semantic | 3,360 | 50 | 4,544 | 4,544 | 5 | 250 | 0 | 0 | checked=50, mismatches=0 |
| type-src-label | schema | 1,177 | 50 | 5,408 | 23,639,592 | 75 | 50,000 | 0 | 0 | anchor |
| type-src-label | edge-type-only | 1,178 | 50 | 5,408 | 25,196,048 | 74 | 50,000 | 0 | 0 | checked=50, mismatches=0 |
| type-src-label | budg-b64 | 1,128 | 50 | 5,408 | 23,623,080 | 87 | 50,000 | 0 | 0 | checked=50, mismatches=0 |
| type-src-label | semantic | 1,128 | 50 | 5,408 | 23,623,224 | 73 | 50,000 | 0 | 0 | checked=50, mismatches=0 |

## Pruning reason breakdown

### property-required

| Variant | Reason | Pruned segments | Kept segments | Saved segment bytes | Saved body reads |
|---|---|---:|---:|---:|---:|
| schema | edge_type | 1,051 | 0 | 43,307,251,072 | 1,051 |
| schema | property_absence | 25 | 0 | 456,730,968 | 25 |
| schema | schema_tombstone_fallback | 0 | 290 | 0 | 0 |
| edge-type-only | edge_type | 1,069 | 0 | 43,351,591,496 | 1,069 |
| edge-type-only | property_absence | 9 | 0 | 412,822,016 | 9 |
| budg-b64 | edge_type | 1,125 | 0 | 43,350,066,760 | 1,125 |
| budg-b64 | property_absence | 18 | 0 | 413,101,696 | 18 |
| budg-b64 | schema_tombstone_fallback | 0 | 290 | 0 | 0 |
| semantic | edge_type | 2,035 | 0 | 43,349,741,496 | 2,035 |
| semantic | property_absence | 287 | 0 | 417,122,312 | 287 |
| semantic | schema_tombstone_fallback | 0 | 320 | 0 | 0 |

### type-only

| Variant | Reason | Pruned segments | Kept segments | Saved segment bytes | Saved body reads |
|---|---|---:|---:|---:|---:|
| schema | kept_candidate | 0 | 3,371 | 0 | 0 |
| edge-type-only | kept_candidate | 0 | 3,371 | 0 | 0 |
| budg-b64 | kept_candidate | 0 | 3,360 | 0 | 0 |
| semantic | kept_candidate | 0 | 3,360 | 0 | 0 |

### type-src-label

| Variant | Reason | Pruned segments | Kept segments | Saved segment bytes | Saved body reads |
|---|---|---:|---:|---:|---:|
| schema | kept_candidate | 0 | 1,177 | 0 | 0 |
| edge-type-only | kept_candidate | 0 | 1,178 | 0 | 0 |
| budg-b64 | kept_candidate | 0 | 1,128 | 0 | 0 |
| semantic | kept_candidate | 0 | 1,128 | 0 | 0 |

## Claim rule

- GO: 可以写 edge-type-only 是 SemL0 的一维特例，真实 property-graph semantics 还能进一步降低 candidate/body/read。
- FALLBACK: 只能写 SemL0 暴露了可验证的 semantic telemetry，必要性 claim 收窄，不写稳定优于 edge-type-only。
- NO-GO: 有 mismatch、缺字段或缺结果，W14 不能进入论文主 claim。
