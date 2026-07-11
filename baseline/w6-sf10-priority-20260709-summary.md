# W6 SF10 Matrix Report

Generated: 2026-07-11T13:33:49+08:00
Run dir: `/data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf10-priority-20260709`
Status: `DONE`

This report is regenerated from raw W6 JSON/TSV artifacts. Missing variants are labeled `PENDING`; no simulated/model-derived rows are promoted to measured results.

## Resource Envelope

No `resource-monitor.tsv` was recorded for this run. Per-variant import RSS below remains measured from `/usr/bin/time -v`; no all-PENDING envelope is emitted.

## Aggregate Matrix

The p50/p90/p99 column is the mean of per-edge-type percentiles across repeats; it is not a raw global percentile over all operations.

| variant | layout | store GiB | L0 files | ops/repeat | candidate L0 mean+/-std | read bytes mean+/-std | avg us mean+/-std | p50/p90/p99 edge-mean us | compare vs naive |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| schema | Schema | 13.3 | 376 | 9,000 | 131,321 +/- 0 | 81,477,611 +/- 71,328,014 | 749.8 +/- 0.5 | 1,048 +/- 3/1,248 +/- 5/1,407 +/- 35 | PASS (9,000) |
| naive | Naive | 13.4 | 170 | 9,000 | 1,001,798 +/- 0 | 338,940,880 +/- 190,626,032 | 4,177.3 +/- 28.2 | 5,713 +/- 13/7,426 +/- 262/7,833 +/- 0 | anchor |
| kv-lsm | RocksDbStyle | 13.4 | 170 | 9,000 | 1,001,798 +/- 0 | 338,940,880 +/- 190,626,032 | 4,111.1 +/- 32.7 | 5,676 +/- 35/6,296 +/- 288/7,833 +/- 0 | PASS (9,000) |
| edge-type-only | EdgeTypeOnly | 13.3 | 380 | 9,000 | 122,444 +/- 0 | 78,943,597 +/- 71,327,713 | 534.6 +/- 3.8 | 706 +/- 0/1,017 +/- 0/1,198 +/- 18 | PASS (9,000) |
| semantic | Semantic | 14.6 | 737 | 9,000 | 158,358 +/- 0 | 78,098,749 +/- 70,894,420 | 509.1 +/- 3.8 | 702 +/- 3/744 +/- 24/1,170 +/- 13 | PASS (9,000) |
| budg-b64 | SemanticBudgeted | 13.3 | 444 | 9,000 | 149,019 +/- 0 | 78,331,325 +/- 71,159,727 | 525.4 +/- 13.5 | 706 +/- 0/989 +/- 0/1,170 +/- 13 | PASS (9,000) |
| budg-b256 | SemanticBudgeted | 13.7 | 569 | 9,000 | 158,924 +/- 0 | 78,103,013 +/- 70,879,509 | 522.6 +/- 10.9 | 707 +/- 3/874 +/- 109/1,170 +/- 13 | PASS (9,000) |
| budg-b1024 | SemanticBudgeted | 14.5 | 605 | 9,000 | 158,924 +/- 0 | 78,103,013 +/- 70,879,509 | 517.1 +/- 5.2 | 706 +/- 0/915 +/- 105/1,170 +/- 13 | PASS (9,000) |
| oracle | OracleSemantic | 13.4 | 170 | 9,000 | 28,907 +/- 0 | 129,725,328 +/- 139,216,903 | 659.5 +/- 12.0 | 804 +/- 3/1,054 +/- 14/1,443 +/- 335 | PASS (9,000) |

## Correctness Compares

| variant | checked | passed | mismatches | elapsed s | same snapshot |
| --- | --- | --- | --- | --- | --- |
| schema | 9,000 | 9,000 | 0 | 44.9 | yes |
| kv-lsm | 9,000 | 9,000 | 0 | 75.9 | yes |
| edge-type-only | 9,000 | 9,000 | 0 | 38.1 | yes |
| semantic | 9,000 | 9,000 | 0 | 37.6 | yes |
| budg-b64 | 9,000 | 9,000 | 0 | 37.7 | yes |
| budg-b256 | 9,000 | 9,000 | 0 | 38.2 | yes |
| budg-b1024 | 9,000 | 9,000 | 0 | 38.4 | yes |
| oracle | 9,000 | 9,000 | 0 | 66.4 | yes |

## Import RSS

| variant | max RSS GiB |
| --- | --- |
| schema | 2.12 |
| naive | 2.22 |
| kv-lsm | 2.33 |
| edge-type-only | 1.79 |
| semantic | 8.38 |
| budg-b64 | 2.26 |
| budg-b256 | 5.18 |
| budg-b1024 | 8.21 |
| oracle | 2.27 |

## Gate Candidates

- Gate 1 latency: GO: budg-b64 avg_us=525.4+/-13.5, schema avg_us=749.8+/-0.5, delta=29.92%. GO requires the 1-stddev intervals not to overlap.
- Gate 2 RSS: GO: schema/naive RSS=0.96x, budg-b64/naive RSS=1.02x. Heuristic threshold is <=2.0x; final text should describe measured RSS exactly.

## Required Manual Checks Before Paper Use

- Confirm `DONE` exists before treating this as final.
- Keep `kv-lsm` as measured only if its import, bench, compare, and manifest rows are present.
- If Gate 1 is FALLBACK, write latency claims as preliminary or omit latency advantage.
- If Gate 2 is FALLBACK, move RSS to caveat/limitation rather than a main claim.
- Reconcile any reader over-read caveat before making read_bytes a primary claim.
