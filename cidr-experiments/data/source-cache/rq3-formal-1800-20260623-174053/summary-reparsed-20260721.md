# RQ3 feedback-vs-no-feedback tail latency under churn

Generated from raw JSONL outputs under:

`rq3-formal-1800-20260623-174053`

Status: `ready-for-paper with caveats`

Markers: DONE=True, FAILED=False, ABORT=False. Single variable = L0 compaction strategy; all arms `--variant semantic` on identical copies of the same base store under the same mixed read/write workload.

## Run validation

| arm | JSONL lines | checkpoints | final event | done | compactions | rewrite bytes | writer errors | writer slow ops |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| `feedback` | 8 | 6 | `done` | true | 29 | 164.86 MiB | 0 | 0 |
| `full` | 8 | 6 | `done` | true | 29 | 308.59 MiB | 0 | 0 |
| `none` | 8 | 6 | `done` | true | 0 | 0 B | 0 | 0 |

## Aggregate (tail vs maintenance cost)

| arm | total queries | mean p50 us | mean p99 us | first p99 us | last p99 us | p99 slope (last/first) | max p99 us | mean candidate L0 | last L0 files | rewrite bytes | io write bytes | writer max op us | flush-stall proxy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `feedback` | 295,140 | 63.75 | 411.82 | 127.11 | 692.54 | 5.45 | 692.54 | 148.24 | 294 | 164.86 MiB | 94.44 MiB | 64,508 | 0 |
| `full` | 295,541 | 34.37 | 146.20 | 112.87 | 168.76 | 1.50 | 175.41 | 9.61 | 18 | 308.59 MiB | 165.76 MiB | 104,561 | 0 |
| `none` | 295,342 | 117.53 | 832.67 | 376.70 | 1,227.18 | 3.26 | 1,227.18 | 288.33 | 578 | 0 B | 12.95 MiB | 127,822 | 0 |

## Per-checkpoint p99 trajectory

| arm | elapsed s | queries | p50 us | p99 us | candidate L0 mean | L0 files | compactions | rewrite bytes | writer max op us |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `feedback` | 300.00 | 49,219 | 9.25 | 127.11 | 23.36 | 46 | 4 | 5.82 MiB | 64,508 |
| `feedback` | 600.00 | 49,172 | 23.97 | 227.82 | 69.76 | 101 | 5 | 16.69 MiB | 64,508 |
| `feedback` | 900.00 | 49,225 | 70.33 | 349.44 | 121.15 | 144 | 5 | 25.02 MiB | 64,508 |
| `feedback` | 1,200.00 | 49,194 | 81.40 | 447.87 | 170.23 | 212 | 5 | 32.37 MiB | 64,508 |
| `feedback` | 1,500.00 | 49,115 | 95.18 | 626.15 | 233.25 | 258 | 5 | 37.84 MiB | 64,508 |
| `feedback` | 1,800.00 | 49,215 | 102.37 | 692.54 | 271.70 | 294 | 5 | 47.11 MiB | 64,508 |
| `full` | 300.00 | 49,271 | 6.18 | 112.87 | 9.52 | 20 | 4 | 8.07 MiB | 104,561 |
| `full` | 600.01 | 49,251 | 7.66 | 126.86 | 9.48 | 20 | 5 | 27.61 MiB | 104,561 |
| `full` | 900.00 | 49,292 | 48.65 | 144.39 | 9.88 | 20 | 5 | 45.48 MiB | 104,561 |
| `full` | 1,200.00 | 49,260 | 47.61 | 148.90 | 9.46 | 18 | 5 | 60.77 MiB | 104,561 |
| `full` | 1,500.00 | 49,225 | 48.05 | 175.41 | 9.46 | 18 | 5 | 75.82 MiB | 104,561 |
| `full` | 1,800.00 | 49,242 | 48.09 | 168.76 | 9.87 | 18 | 5 | 90.83 MiB | 104,561 |
| `none` | 300.00 | 49,264 | 17.25 | 376.70 | 47.20 | 96 | 0 | 0 B | 127,822 |
| `none` | 600.00 | 49,206 | 47.69 | 541.67 | 143.64 | 192 | 0 | 0 B | 127,822 |
| `none` | 900.00 | 49,256 | 100.02 | 776.07 | 240.09 | 288 | 0 | 0 B | 127,822 |
| `none` | 1,200.00 | 49,237 | 130.43 | 960.70 | 336.55 | 384 | 0 | 0 B | 127,822 |
| `none` | 1,500.01 | 49,221 | 187.31 | 1,113.69 | 433.02 | 482 | 0 | 0 B | 127,822 |
| `none` | 1,800.00 | 49,158 | 222.48 | 1,227.18 | 529.50 | 578 | 0 | 0 B | 127,822 |

## Deltas vs none (the feedback question)

| arm | mean p99 delta | last p99 delta | p99 slope delta | mean candidate L0 delta | rewrite bytes | io write bytes delta |
|---|---:|---:|---:|---:|---:|---:|
| `feedback` | -50.5% | -43.6% | +67.2% | -48.6% | 164.86 MiB | +629.5% |
| `full` | -82.4% | -86.2% | -54.1% | -96.7% | 308.59 MiB | +1180.4% |

## Conclusion (read the p99 ordering, then claim accordingly)

Map the p99 ordering to a claim:
- `feedback < full < none` -> RQ3 strong: feedback compaction lowers tail AND the gain is from the targeting signal (beats blind compaction at equal cadence).
- `feedback ~= full < none` -> drop the strong claim; report efficiency: same tail as full at lower rewrite bytes / writer-max-op.
- `none ~= feedback` -> at this churn rate/horizon semantic pruning alone holds the tail; feedback value moves to bounding L0 growth over longer horizons (extend duration).
- `feedback > none` -> compaction's write-blocking cost dominates here; report as a when-not-to-use bound.

Boundaries (state in paper): semantic-layout-only (no general feedback claim); single churn rate + single workload shape; `full` is a strong/coarse non-feedback baseline (round-robin would be fairer); n=1, SF30, no confidence intervals; the window may be too short to surface the none-vs-feedback long-horizon divergence.
