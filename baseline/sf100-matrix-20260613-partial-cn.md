# W6 SF100 Matrix Report

Generated: 2026-06-14T20:02:48+08:00
Run dir: `remote-logs/w6-sf100-matrix-20260613-132325`
Status: `RUNNING/PARTIAL`

This report is regenerated from raw W6 JSON/TSV artifacts. Missing variants are labeled `PENDING`; no simulated/model-derived rows are promoted to measured results.

## Resource Envelope

| first sample | last sample | samples | min MemAvailable GiB | min /data free GiB | last W6 store |
| --- | --- | --- | --- | --- | --- |
| 2026-06-13T13:29:40+08:00 | 2026-06-14T09:54:51+08:00 | 246 | 135 | 232 | 350G |

## Aggregate Matrix

The p50/p90/p99 column is the mean of per-edge-type percentiles across repeats; it is not a raw global percentile over all operations.

| variant | layout | store GiB | L0 files | ops/repeat | candidate L0 mean+/-std | read bytes mean+/-std | avg us mean+/-std | p50/p90/p99 edge-mean us | compare vs naive |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| schema | Schema | 133.9 | 3,444 | 45,000 | 5,929,197 +/- 0 | 859,764,709 +/- 697,015,798 | 9,816.8 +/- 1,585.5 | 10,259 +/- 1,663/26,481 +/- 3,274/37,333 +/- 8,957 | PASS (45,000) |
| naive | Naive | 134.6 | 1,703 | 45,000 | 49,257,601 +/- 0 | 3,629,768,504 +/- 1,869,743,834 | 53,513.8 +/- 189.7 | 53,889 +/- 0/172,222 +/- 0/179,259 +/- 693 | anchor |
| kv-lsm | RocksDbStyle | 134.6 | 1,703 | 45,000 | 49,257,601 +/- 0 | 3,629,768,504 +/- 1,869,743,834 | 53,892.0 +/- 139.3 | 53,704 +/- 131/172,315 +/- 131/178,889 +/- 454 | PASS (45,000) |
| edge-type-only | EdgeTypeOnly | 133.9 | 3,446 | 45,000 | 5,899,015 +/- 0 | 859,688,909 +/- 696,984,663 | 8,676.5 +/- 16.3 | 9,111 +/- 45/22,278 +/- 1,260/31,074 +/- 114 | PASS (45,000) |
| semantic | Semantic | 146.4 | 6,615 | 45,000 | 7,782,877 +/- 0 | 673,877,573 +/- 453,538,953 | 8,250.9 +/- 37.1 | 8,733 +/- 5/20,989 +/- 79/30,415 +/- 13 | PASS (45,000) |
| budg-b64 | SemanticBudgeted | 133.9 | 3,483 | 45,000 | 6,022,507 +/- 0 | 859,490,557 +/- 697,318,824 | 8,722.1 +/- 37.1 | 9,065 +/- 13/22,500 +/- 1,574/30,981 +/- 250 | PASS (45,000) |
| budg-b256 | SemanticBudgeted | 133.9 | 3,675 | 45,000 | 6,480,991 +/- 0 | 858,117,440 +/- 697,618,110 | 8,588.0 +/- 13.8 | 9,065 +/- 13/21,019 +/- 448/31,019 +/- 183 | PASS (45,000) |
| budg-b1024 | SemanticBudgeted | 134.0 | 4,451 | 45,000 | 7,929,225 +/- 0 | 856,071,877 +/- 699,563,826 | 8,564.5 +/- 27.1 | 9,120 +/- 26/20,056 +/- 1,227/31,148 +/- 131 | PASS (45,000) |
| oracle | OracleSemantic | 134.6 | 1,703 | 45,000 | 532,193 +/- 0 | 1,318,896,776 +/- 1,338,783,348 | 9,723.8 +/- 1,641.3 | 10,028 +/- 1,473/25,741 +/- 3,169/36,704 +/- 10,580 | PASS (45,000) |

## Correctness Compares

| variant | checked | passed | mismatches | elapsed s | same snapshot |
| --- | --- | --- | --- | --- | --- |
| schema | 45,000 | 45,000 | 0 | 2,827.2 | yes |
| kv-lsm | 45,000 | 45,000 | 0 | 4,862.9 | yes |
| edge-type-only | 45,000 | 45,000 | 0 | 2,815.8 | yes |
| semantic | 45,000 | 45,000 | 0 | 2,807.5 | yes |
| budg-b64 | 45,000 | 45,000 | 0 | 2,791.6 | yes |
| budg-b256 | 45,000 | 45,000 | 0 | 2,817.0 | yes |
| budg-b1024 | 45,000 | 45,000 | 0 | 2,774.4 | yes |
| oracle | 45,000 | 45,000 | 0 | 5,236.7 | yes |

## Import RSS

| variant | max RSS GiB |
| --- | --- |
| schema | 2.42 |
| naive | 2.43 |
| kv-lsm | 2.49 |
| edge-type-only | 2.53 |
| semantic | 118.03 |
| budg-b64 | 2.21 |
| budg-b256 | 2.49 |
| budg-b1024 | 2.68 |
| oracle | 2.67 |

## Gate Candidates

- Gate 1 latency: FALLBACK: budg-b64 avg_us=8722.1+/-37.1, schema avg_us=9816.8+/-1585.5, delta=11.15%. GO requires the 1-stddev intervals not to overlap.
- Gate 2 RSS: GO: schema/naive RSS=0.99x, budg-b64/naive RSS=0.91x. Heuristic threshold is <=2.0x; final text should describe measured RSS exactly.

## Required Manual Checks Before Paper Use

- Confirm `DONE` exists before treating this as final.
- Keep `kv-lsm` as measured only if its import, bench, compare, and manifest rows are present.
- If Gate 1 is FALLBACK, write latency claims as preliminary or omit latency advantage.
- If Gate 2 is FALLBACK, move RSS to caveat/limitation rather than a main claim.
- Reconcile any reader over-read caveat before making read_bytes a primary claim.
