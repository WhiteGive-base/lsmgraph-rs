# External baselines — consolidated (paper-side)

> Source of record (organized package): `baseline/seml0-baseline-defense-20260619/`
> (`external-baseline-results.md/.tsv`, `external-runs/`, `sf10-main-baseline-table.md`,
> `evidence-map.md`) — assembled by recovering completed **remote** runs (with DONE
> markers), not a plan-only table. LiveGraph SF10 raw verified on this box at
> `remote-logs/livegraph-sf10-20260612/` (DONE). **Honest labels matter:** only LiveGraph
> is an external *system*; the others are internal/style/simulated.

## 1. LiveGraph SF10 — real external graph store ★ (this is the external SOTA baseline)
Verified from `remote-logs/livegraph-sf10-20260612/livegraph-sf10.json` + `livegraph-footprint.tsv`.

| metric | value |
|---|---|
| edges (dense = scan = LiveGraph edge_count) | **355,185,382** (gate: all three equal) |
| vertices | 29,987,835 |
| load time | **1,502.59 s** |
| peak RSS | 47,908,592 KiB (**≈45.7 GiB**) |
| on-disk block store | 37.58 GB |
| WAL | 1.0 GB |
| neighbor scan, positive core edge types (weighted avg) | **2.742 µs** |
| neighbor scan, edge_type=1 | avg **3.377 µs**, p50 2.387, p90 7.540, p99 **15.197 µs** |

- **Honest scope:** external comparison is at **SF10 only**. LiveGraph SF100 was previously found infeasible (load extrapolated 17–21 days; `baseline/livegraph-sf100-attempt-20260612-cn.md`), so we do **not** claim an external comparison at SF100.
- The full SemL0-vs-LiveGraph SF10 head-to-head table lives in `baseline/seml0-baseline-defense-20260619/sf10-main-baseline-table.md` (organized on the authoring machine; pull into this box to inline the SemL0 column).

## 2. RocksDB-style KV-LSM — internal "external-style" baseline (NOT official RocksDB)
Recovered RocksDbStyle artifact (`external-runs/`). Label as RocksDB-*style* / KV-LSM; do **not** claim "beats RocksDB."

| metric | value |
|---|---|
| correctness | checked 45,000, **mismatches 0** |
| SF100 read bytes | 1,000,730,528 |
| SF100 candidate L0 | 49,257,601 |
| SF100 body reads | 3,122,166 |

(Consistent with the W6 `kv-lsm` variant's candidate L0 = 49,257,601; this is the semantics-blind KV-LSM layout, an internal baseline.)

## 3. KV-style encoding simulation — SIMULATED (archived, not a measured result)
Archived; must be labeled **simulated**, never promoted to a measured row.

| metric | value |
|---|---|
| read bytes | 175,938,084 |
| candidate L0 | 45,000 |
| body reads | 4,807,169 |
| weighted avg latency | 4,906.2 µs |

## Paper framing (claim-bounded)
- **(a) external SOTA baseline gap → partially filled:** SemL0 now has a real external graph-store comparison (**LiveGraph, SF10**), with measured load / footprint / neighbor-scan latency. Limitation: SF10 only (SF100 LiveGraph infeasible).
- RocksDB-style and KV-style rows are an **internal layout baseline** and a **simulation**, labeled as such — they are not external systems.
- This complements the internal variant matrix (W6 SF100) and the C2 retention (real SF30); it does not change Gate verdicts.
