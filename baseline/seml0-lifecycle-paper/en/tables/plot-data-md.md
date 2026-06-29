# SemL0 — Plot data (real numbers, ready to chart)

> Every table below is **measured** data extracted from the dated baseline summaries.
> Use these to draw the figures yourself. Source files cited per block.
> Read bytes shown in MiB (= bytes / 1,048,576) for readability; raw bytes in the source files.

---

## Chart 1 — W6 SF100 main results, per variant (bar charts)
Source: `baseline/sf100-matrix-20260613-cn.md` (45,000 ops/repeat, all `mismatches=0`). `naive` = anchor.

| variant | candidate L0 (mean) | read bytes (MiB) | avg µs | p50 µs | p90 µs | p99 µs | store GiB | L0 files |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| naive | 49,257,601 | 3461.6 | 53,513.8 | 53,889 | 172,222 | 179,259 | 134.6 | 1,703 |
| kv-lsm | 49,257,601 | 3461.6 | 53,892.0 | 53,704 | 172,315 | 178,889 | 134.6 | 1,703 |
| schema | 5,929,197 | 820.0 | 9,816.8 | 10,259 | 26,481 | 37,333 | 133.9 | 3,444 |
| edge-type-only | 5,899,015 | 819.9 | 8,676.5 | 9,111 | 22,278 | 31,074 | 133.9 | 3,446 |
| budg-b64 | 6,022,507 | 819.7 | 8,722.1 | 9,065 | 22,500 | 30,981 | 133.9 | 3,483 |
| budg-b256 | 6,480,991 | 818.4 | 8,588.0 | 9,065 | 21,019 | 31,019 | 133.9 | 3,675 |
| budg-b1024 | 7,929,225 | 816.4 | 8,564.5 | 9,120 | 20,056 | 31,148 | 134.0 | 4,451 |
| semantic | 7,782,877 | 642.7 | 8,250.9 | 8,733 | 20,989 | 30,415 | 146.4 | 6,615 |
| oracle | 532,193 | 1257.8 | 9,723.8 | 10,028 | 25,741 | 36,704 | 134.6 | 1,703 |

- **Chart 1a (candidate L0 / read bytes):** bar per variant — shows naive/kv ≈8× the candidates and ≈4–5× the read bytes of the semantic variants; `oracle` is the lower bound on candidates.
- **Chart 1b (latency p50/p90/p99):** grouped bars per variant. Note: naive's p99 is ~6× the pruned variants. (Latency is *supporting* — Gate 1 FALLBACK for the static read-only bench; budg-b64 vs schema avg only 11.15% with overlapping σ.)

## Chart 2 — W6 budget sweep (line chart: budget → cost/benefit)
Same source. Shows the schema→full-semantic interpolation; bigger byte-budget = more L0 files, candidates drift up.

| variant | extra-L0 budget | candidate L0 | read bytes (MiB) | avg µs | L0 files |
|---|---|---:|---:|---:|---:|
| schema | 0 (baseline) | 5,929,197 | 820.0 | 9,816.8 | 3,444 |
| budg-b64 | 64 | 6,022,507 | 819.7 | 8,722.1 | 3,483 |
| budg-b256 | 256 | 6,480,991 | 818.4 | 8,588.0 | 3,675 |
| budg-b1024 | 1024 | 7,929,225 | 816.4 | 8,564.5 | 4,451 |
| semantic | ∞ (full) | 7,782,877 | 642.7 | 8,250.9 | 6,615 |

## Chart 3 — W6 import RSS, per variant (bar; Gate 2)
| variant | max RSS GiB |
|---|---:|
| budg-b64 | 2.21 |
| schema | 2.42 |
| naive | 2.43 |
| kv-lsm | 2.49 |
| budg-b256 | 2.49 |
| edge-type-only | 2.53 |
| oracle | 2.67 |
| budg-b1024 | 2.68 |
| semantic | **118.03** (unbudgeted stress point / outlier) |
- Pruned/budgeted RSS ≈ naive (Gate 2 = GO). The unbudgeted `semantic` import RSS is a stress point motivating budgeted lifecycle control; not a recommended configuration or main operating claim.

---

## Chart 4 — W9 steady-state time-series (line charts; THE lifecycle/degradation story) ★
Source: `baseline/w9-steady-state-summary-20260615-cn.md` (SF30 mixed read/write, 6 checkpoints, ~163 q/s, 0 writer errors).

**p99 latency over time (µs)** — schema degrades, semantic stays flat:
| t (s) | schema | budg-b64 | semantic |
|---:|---:|---:|---:|
| 300 | 1,270.59 | 1,275.69 | 414.58 |
| 600 | 2,689.06 | 2,668.46 | 557.76 |
| 900 | 4,070.45 | 4,019.79 | 792.06 |
| 1200 | 5,493.15 | 5,558.31 | 958.19 |
| 1500 | 6,886.92 | 7,029.10 | 1,096.84 |
| 1800 | 8,740.23 | 8,255.77 | 1,273.97 |

**p50 latency over time (µs):**
| t (s) | schema | budg-b64 | semantic |
|---:|---:|---:|---:|
| 300 | 28.94 | 27.67 | 50.71 |
| 600 | 50.33 | 48.64 | 80.79 |
| 900 | 396.14 | 392.52 | 126.42 |
| 1200 | 447.92 | 455.66 | 162.53 |
| 1500 | 714.27 | 741.68 | 213.38 |
| 1800 | 817.62 | 834.28 | 252.20 |

**candidate L0 over time / L0 files (cost side):**
| t (s) | schema cand | semantic cand | schema L0 files | semantic L0 files |
|---:|---:|---:|---:|---:|
| 300 | 48.61 | 74.23 | 1,124 | 2,158 |
| 1800 | 289.89 | 556.33 | 1,365 | 2,640 |

- **Chart 4 (p99 over time):** line per variant — **schema/budg-b64 p99 climbs ~6.9× (1.27k→8.74k µs) while semantic stays ~3× (0.41k→1.27k µs)**. Mean deltas vs schema: semantic p50 −63.9%, p99 −82.5% (cost: candidate L0 +86.3%, L0 files +93.4%).
- This is the strongest latency evidence in the project — on the **dynamic** workload semantic wins big on tail; pair it with the cost (more L0 files).

---

## Chart 5 — W8 property predicate deltas vs schema (bar)
Source: `baseline/w8-property-2hop-summary-20260615-cn.md` (SF30; presence/equality/absent-default aggregate; required-property is exact-prune = 0 candidates for all).
| variant | candidate L0 Δ | body reads Δ | read bytes Δ | elapsed Δ |
|---|---:|---:|---:|---:|
| budg-b64 | −22.0% | −22.2% | −14.5% | −23.2% |
| semantic | −21.8% | −22.2% | −14.4% | −23.9% |
- Boundary: 2-hop typed expansion improves body/read/elapsed but candidate L0 > schema → **do not** chart "universal 2-hop candidate reduction."

---

## Chart 6 — C2 lifecycle retention: naive vs semantic merge (bar) ★
Source: `baseline/path-b-c2/` (controlled; `tables/table-c2-retention.md`). SF30(real) = **done** (1.09B edges; rows below).
| scale | policy | pruning_retention | exact_surface after | read after vs before | output segs | write_amp | mismatches |
|---|---|---:|---:|---|---:|---:|---:|
| SF1 (synth) | naive | 0.0 | 0.0 | 4× blow-up | 1 | 1.22 | 0 |
| SF1 (synth) | semantic | 1.0 | 1.0 | flat | 4 | 1.87 | 0 |
| SF10c (synth) | naive | 0.0 | 0.0 | 6× blow-up | 1 | 1.14 | 0 |
| SF10c (synth) | semantic | 1.0 | 1.0 | flat | 6 | 1.85 | 0 |
| SF1 (real LDBC) | naive | 0.0 | 0.0 | — | 17 (from 44) | 1.08 | — |
| SF1 (real LDBC) | semantic | 1.0 | 1.0 | — | 44 (from 44) | 1.27 | — |
| SF30 (real LDBC, 1.09B edges) | naive | 0.0 | 0.0 | metadata proxy: 6.52× weighted candidate-byte read-amp | 503 (from 528) | 1.07 | — |
| SF30 (real LDBC, 1.09B edges) | semantic | 1.0 | 1.0 | metadata proxy: 1.00× weighted candidate-byte read-amp | 528 (from 528) | 1.24 | — |

- **Chart 6a (retention + cost):** grouped bars — `exact_surface_ratio` (1.0 vs 0.0) and `write_amp` (cost) side by side.
- **Chart 6b (real-SF30 proxy):** bars for weighted candidate-byte read-amp (naive 6.52× vs semantic 1.00×) and avg candidate segments/query (93.6 vs 13.2). Label it as metadata replay, not full body-read workload.

---

## Chart 7 — External baselines (bar; honest labels) ★
Record: `baseline/seml0-baseline-defense-20260619/`. LiveGraph raw verified: `remote-logs/livegraph-sf10-20260612/`.

**LiveGraph SF10 — real external graph store (footprint + scan latency):**
| metric | value |
|---|---|
| edges (= dense = scan = LiveGraph) | 355,185,382 |
| load s | 1,502.59 |
| peak RSS GiB | ≈45.7 |
| on-disk GB (block + WAL) | 37.58 + 1.0 |
| scan µs (positive core, weighted avg) | 2.742 |
| scan µs edge_type=1 (avg / p99) | 3.377 / 15.197 |

(SemL0-vs-LiveGraph SF10 head-to-head lives in `baseline/seml0-baseline-defense-20260619/sf10-main-baseline-table.md` — pull to this box to inline the SemL0 column. External comparison is **SF10 only**; LiveGraph SF100 infeasible.)

**Internal / simulated (clearly NOT external systems):**
| baseline | read bytes | candidate L0 | body reads | note |
|---|---:|---:|---:|---|
| RocksDB-style KV-LSM (SF100) | 1,000,730,528 | 49,257,601 | 3,122,166 | internal, RocksDB-*style*, checked 45k / 0 mismatch |
| KV-style encoding (SIM) | 175,938,084 | 45,000 | 4,807,169 | **simulated**, archived (weighted avg 4906.2 µs) |

---

## Figures that are diagrams (no data table — draw by hand)
- **Fig: pipeline/lifecycle diagram** (§3 four loops). 
- **Fig: schema-snapshot timeline** (§6 epoch e0→e1, old segments auto-skip / conservative read, no rewrite at boundary).
