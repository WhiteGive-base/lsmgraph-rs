# Table C2 — Lifecycle Retention: naive vs semantic merge (RQ6)

> Sources: `baseline/path-b-c2/stage4-smoke-sf1-20260618.json` (SF1 synthetic),
> `stage5-scale-sf10class-20260618.json` (SF10-class synthetic),
> `remote-logs/c2-sf30-real-20260619/{naive,semantic}.json` (**real LDBC SF30**, 1.09B edges),
> `remote-logs/c2-sf30-readamp-proxy-20260621/{naive,semantic}-proxy.json`
> (**real LDBC SF30 metadata replay**, 40 typed-neighbor partitions),
> SF1-real from the build-l1 validation. Column definitions: `tables/table-c2-retention-schema.md`.
> **Read as three layers:** A proves the read-amplification consequence on a controlled workload;
> B proves retention + write cost on real data; C bridges the real SF30 store to a
> metadata-level typed-neighbor read-amplification proxy.

## Layer A — Controlled read-amplification (synthetic; full read workload + correctness)
Fixed partition structure isolates the merge policy; typed-neighbor read workload and vs-naive correctness are measured.

| scale | policy | pruning_retention | read after vs before | output segs | write_amp | mismatches |
|---|---|---:|---|---:|---:|---:|
| SF1 | naive | **0.000** | **4× blow-up** | 1 | 1.22 | 0 |
| SF1 | semantic | **1.000** | flat | 4 | 1.87 | 0 |
| SF10c | naive | **0.000** | **6× blow-up** | 1 | 1.14 | 0 |
| SF10c | semantic | **1.000** | flat | 6 | 1.85 | 0 |

→ semantic merge retains the exact surface (1.0) and keeps reads flat; naive collapses the
`(src_label, edge_type)` keys (0.0) and inflates typed-neighbor read bytes by ≈ #edge_types.

## Layer B — Real LDBC data: retention + write cost (open-mode: surface + cost only)
Real SNB label/edge-type distributions. Per-run read workload is **not** re-measured here (read-amp is
established in Layer A; merge losslessness is covered by the engine test suite). †open-mode.

| scale | policy | pruning_retention | exact_surface after | output segs | output_bytes | logical_bytes | write_amp |
|---|---|---:|---:|---:|---:|---:|---:|
| SF1 (real) | naive | **0.000** | 0.000 | 17 (from 44) | — | — | 1.08 |
| SF1 (real) | semantic | **1.000** | 1.000 | 44 (from 44) | — | — | 1.27 |
| **SF30 (real, 1.09B edges)** | naive | **0.000** | 0.000 | 503 (from 528) | 37.20 GB | 32.4 GB | 1.07 |
| **SF30 (real, 1.09B edges)** | semantic | **1.000** | 1.000 | 528 (from 528) | 43.25 GB | 32.4 GB | 1.24 |

→ retention 1.0 vs 0.0 **holds on real 1.09B-edge data** (40 `(src_label, edge_type)` partitions →
528 exact L1 segments); the cost is bounded — semantic write_amp 1.24 vs naive 1.07 (+16%), from
per-segment overhead across 528 segments.

## Layer C — Real SF30 metadata-level read-amplification proxy
This replay uses the real SF30 post-merge segment metadata and the 40 active
`(src_label, edge_type)` typed-neighbor partitions. It measures how many post-merge
segments/bytes remain conservative candidates for each typed partition. It does
**not** claim a full end-to-end SF30 read workload with body decode.

| scale | policy | query partitions | avg candidate segs/query | candidate bytes total | exact bytes before | weighted read-amp proxy | mixed candidate segs |
|---|---|---:|---:|---:|---:|---:|---:|
| **SF30 (real metadata replay)** | naive | 40 | **93.6** | **282.16 GB** | 43.25 GB | **6.52×** | 3,745 |
| **SF30 (real metadata replay)** | semantic | 40 | **13.2** | **43.25 GB** | 43.25 GB | **1.00×** | 0 |

→ on the real SF30 store, naive merge's mixed outputs force typed partitions to
read many unrelated post-merge segments; semantic merge keeps candidate bytes equal
to the exact pre-merge partition bytes. This is a metadata-level bridge from Layer A
to the real SF30 segment distribution.

## Boundary
Layer A = controlled synthetic microbenchmark (read-amp + correctness). Layer B = real LDBC
(retention + write cost). Layer C = real LDBC metadata replay (candidate segments/bytes, no
full body-read workload). We claim semantic-aware compaction *retains or rebuilds* the pruning
surface and removes naive merge's read amplification, at a *bounded, explainable* write cost —
not negligible overhead, not optimality, not a production scheduler.
