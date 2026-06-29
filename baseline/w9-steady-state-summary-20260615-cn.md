# W9 mixed read/write steady-state summary

Generated from raw JSONL outputs under:

`remote-logs/w9-steady-state-formal-20260615-1200`

Status: `ready-for-paper with caveats`

Markers: DONE=True, FAILED=False, ABORT=False. The runner reuses W8 frozen SF30 stores as mutable steady-state starting stores.

## Run validation

| variant | JSONL lines | checkpoints | final event | done event | writer errors | writer slow ops |
|---|---:|---:|---|---:|---:|---:|
| `schema` | 8 | 6 | `done` | true | 0 | 0 |
| `budg-b64` | 8 | 6 | `done` | true | 0 | 0 |
| `semantic` | 8 | 6 | `done` | true | 0 | 0 |

## Aggregate checkpoint summary

| variant | total queries | mean query rate/s | mean candidate L0 | last candidate L0 | mean p50 us | mean p99 us | max p99 us | last L0 files | rewrite bytes | io write bytes | flushes | writer max op us | flush-stall proxy count |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `schema` | 293,502 | 163.06 | 169.26 | 289.89 | 409.20 | 4,858.40 | 8,740.23 | 1,365 | 0 B | 13.04 MiB | 289 | 17,416 | 0 |
| `budg-b64` | 293,411 | 163.01 | 162.25 | 282.85 | 416.74 | 4,801.19 | 8,255.77 | 1,432 | 0 B | 13.04 MiB | 289 | 45,243 | 0 |
| `semantic` | 295,251 | 164.03 | 315.30 | 556.33 | 147.67 | 848.90 | 1,273.97 | 2,640 | 0 B | 12.95 MiB | 289 | 12,639 | 0 |

## Per-checkpoint trace

| variant | elapsed s | queries | query rate/s | p50 us | p99 us | candidate L0 mean | L0 files | rewrite bytes | io write bytes | writer errors | writer slow ops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `schema` | 300.00 | 49,150 | 163.83 | 28.94 | 1,270.59 | 48.61 | 1,124 | 0 B | 2.16 MiB | 0 | 0 |
| `schema` | 600.00 | 49,155 | 163.85 | 50.33 | 2,689.06 | 96.85 | 1,172 | 0 B | 2.17 MiB | 0 | 0 |
| `schema` | 900.00 | 49,065 | 163.55 | 396.14 | 4,070.45 | 145.13 | 1,220 | 0 B | 2.16 MiB | 0 | 0 |
| `schema` | 1,200.00 | 48,917 | 163.05 | 447.92 | 5,493.15 | 193.41 | 1,269 | 0 B | 2.21 MiB | 0 | 0 |
| `schema` | 1,500.00 | 48,774 | 162.58 | 714.27 | 6,886.92 | 241.67 | 1,317 | 0 B | 2.17 MiB | 0 | 0 |
| `schema` | 1,800.00 | 48,441 | 161.47 | 817.62 | 8,740.23 | 289.89 | 1,365 | 0 B | 2.17 MiB | 0 | 0 |
| `budg-b64` | 300.00 | 49,214 | 164.04 | 27.67 | 1,275.69 | 41.62 | 1,191 | 0 B | 2.16 MiB | 0 | 0 |
| `budg-b64` | 600.00 | 49,138 | 163.80 | 48.64 | 2,668.46 | 89.85 | 1,239 | 0 B | 2.17 MiB | 0 | 0 |
| `budg-b64` | 900.00 | 49,002 | 163.34 | 392.52 | 4,019.79 | 138.13 | 1,287 | 0 B | 2.16 MiB | 0 | 0 |
| `budg-b64` | 1,200.00 | 48,896 | 162.99 | 455.66 | 5,558.31 | 186.38 | 1,336 | 0 B | 2.21 MiB | 0 | 0 |
| `budg-b64` | 1,500.00 | 48,677 | 162.25 | 741.68 | 7,029.10 | 234.65 | 1,384 | 0 B | 2.17 MiB | 0 | 0 |
| `budg-b64` | 1,800.00 | 48,484 | 161.61 | 834.28 | 8,255.77 | 282.85 | 1,432 | 0 B | 2.17 MiB | 0 | 0 |
| `semantic` | 300.00 | 49,251 | 164.17 | 50.71 | 414.58 | 74.23 | 2,158 | 0 B | 2.14 MiB | 0 | 0 |
| `semantic` | 600.00 | 49,259 | 164.20 | 80.79 | 557.76 | 170.65 | 2,254 | 0 B | 2.16 MiB | 0 | 0 |
| `semantic` | 900.01 | 49,191 | 163.97 | 126.42 | 792.06 | 267.11 | 2,350 | 0 B | 2.14 MiB | 0 | 0 |
| `semantic` | 1,200.00 | 49,163 | 163.88 | 162.53 | 958.19 | 363.55 | 2,446 | 0 B | 2.15 MiB | 0 | 0 |
| `semantic` | 1,500.00 | 49,247 | 164.15 | 213.38 | 1,096.84 | 459.91 | 2,544 | 0 B | 2.19 MiB | 0 | 0 |
| `semantic` | 1,800.00 | 49,140 | 163.80 | 252.20 | 1,273.97 | 556.33 | 2,640 | 0 B | 2.16 MiB | 0 | 0 |

## Deltas vs schema

| variant | mean candidate L0 delta | mean p50 delta | mean p99 delta | last L0 files delta | io write bytes delta |
|---|---:|---:|---:|---:|---:|
| `budg-b64` | -4.1% | +1.8% | -1.2% | +4.9% | 0.0% |
| `semantic` | +86.3% | -63.9% | -82.5% | +93.4% | -0.7% |

## Conclusion

W9 is usable as steady-state evidence with cautious wording. The safe claim is that the mixed read/write workload completed for schema, budg-b64, and semantic on SF30 stores with explicit checkpoint traces for candidate L0, latency, L0 files, rewrite bytes, flush-stall proxy, and IO write bytes. Only claim budg-b64/semantic advantages where the delta table shows them; otherwise phrase as stability and workload-coverage evidence.
