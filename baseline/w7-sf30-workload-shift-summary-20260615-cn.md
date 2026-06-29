# W7 SF30-derived workload-shift summary

Source: `/data/WorkSpace/lsmgraph-rs/remote-logs/w7-sf30-workload-shift-formal-20260615-1536`
Runner: `w7-sf30-workload-shift`
Scale: `real-sf30-derived`

## Run Parameters

| item | value |
|---|---:|
| phase_flushes | 8 |
| hot_sources_per_phase | 64 |
| edges_per_source_per_flush | 1 |
| queries_per_source | 8 |
| max_scan_rows_per_phase | 2000000 |

## Gate

Gate: GO

Feedback-only selected a hot semantic partition in both phase A and phase B, and the selected hot range changed after the workload shift.

## Migration Summary

| variant | phase A selected flush | phase A feedback-weight flush | phase A hot-compaction flush | phase B selected flush | phase B feedback-weight flush | phase B hot-compaction flush | selected ranges changed |
|---|---:|---:|---:|---:|---:|---:|---|
| feedback-only | 0 | 0 | 1 | 0 | 0 | 1 | True |
| static-budgeted | 0 | 1 | 1 | 0 | 1 | 1 | True |
| no-feedback | 0 | n/a | n/a | 0 | n/a | n/a | False |

## Last-Flush Query Cost

| variant | phase | candidate/query before | candidate/query after | read bytes/query before | read bytes/query after | body reads/query before | body reads/query after |
|---|---|---:|---:|---:|---:|---:|---:|
| feedback-only | A | 2.000 | 0.000 | 256.0 | 256.0 | 3.000 | 1.000 |
| feedback-only | B | 2.000 | 0.000 | 256.0 | 256.0 | 3.000 | 1.000 |
| static-budgeted | A | 2.000 | 0.000 | 256.0 | 256.0 | 3.000 | 1.000 |
| static-budgeted | B | 2.000 | 0.000 | 256.0 | 256.0 | 3.000 | 1.000 |
| no-feedback | A | 8.000 | n/a | 256.0 | n/a | 8.000 | n/a |
| no-feedback | B | 8.000 | n/a | 256.0 | n/a | 8.000 | n/a |

## Rewrite Cost

| variant | phase | hot compactions | compaction input | compaction output | io write bytes | write amplification |
|---|---|---:|---:|---:|---:|---:|
| feedback-only | A | 4 | 59.25 KiB | 47.00 KiB | 47.00 KiB | 0.793 |
| feedback-only | B | 4 | 59.25 KiB | 47.00 KiB | 47.00 KiB | 0.793 |
| static-budgeted | A | 4 | 59.25 KiB | 47.00 KiB | 47.00 KiB | 0.793 |
| static-budgeted | B | 4 | 59.25 KiB | 47.00 KiB | 47.00 KiB | 0.793 |
| no-feedback | A | 0 | 0 B | 0 B | 0 B | 0.000 |
| no-feedback | B | 0 | 0 B | 0 B | 0 B | 0.000 |

## Claim Boundary

Safe claim: W7 provides real-SF30-derived evidence that feedback-driven SemL0 can move semantic compaction priority after a workload shift. This is still a derived workload, not a full-store SF30 production trace.
