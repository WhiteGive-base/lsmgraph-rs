## 7. Evaluation

We answer six research questions over LDBC SNB at three scales: SF1 (smoke/correctness), SF30 (main workload), and SF100 (scale). Unless noted, correctness is compared against a semantics-blind `naive` anchor with `mismatches=0`.

- **RQ1 — Does the semantic surface reduce read amplification?** W6 (SF100, 45k ops/repeat): the `naive` baseline probes ~49.3M candidate L0 segments; the pruned variants probe roughly an order of magnitude fewer, and `semantic` achieves the lowest read bytes — all at `mismatches=0`.

  **Table 1 — W6 SF100, per variant (45k ops/repeat, all `mismatches=0`):**

  | variant | candidate L0 | read bytes (MiB) | avg µs | p99 µs | RSS GiB |
  |---|---:|---:|---:|---:|---:|
  | naive (anchor) | 49,257,601 | 3461.6 | 53,513.8 | 179,259 | 2.43 |
  | schema | 5,929,197 | 820.0 | 9,816.8 | 37,333 | 2.42 |
  | edge-type-only | 5,899,015 | 819.9 | 8,676.5 | 31,074 | 2.53 |
  | budg-b64 | 6,022,507 | 819.7 | 8,722.1 | 30,981 | 2.21 |
  | semantic | 7,782,877 | 642.7 | 8,250.9 | 30,415 | 118.03\* |
  | oracle (lower bound) | 532,193 | 1257.8 | 9,723.8 | 36,704 | 2.67 |

  (\*`semantic` is the unbudgeted full-materialization variant. Its 118.03 GiB import RSS is an outlier and is reported as a stress point, not the intended operating point. Full sweep incl. kv-lsm / budg-b256 / budg-b1024 in `tables/plot-data-md.md`.)
- **RQ2 — Does benefit-scored materialization avoid the full-semantic cost?** Budgeted variants interpolate between schema-level and full-semantic layouts, retaining useful partitions without the full-materialization fanout cliff (W6 budget sweep). The 118.03 GiB full-semantic RSS is evidence for budgeted lifecycle control, not a recommended configuration.
- **RQ3 — Does feedback adapt to workload shift, and does the surface help under sustained churn?** W7 (SF30, controlled): feedback redirects materialization budget to the new hot partitions within a few flushes (Gate 4 = GO). W9 (SF30 mixed read/write, ~163 q/s, 30 min): under sustained churn the semantics-blind `schema` tail latency degrades steadily while `semantic` stays roughly flat:

  **Table 2 — W9 p99 latency over time (µs):**

  | t (s) | schema | budg-b64 | semantic |
  |---:|---:|---:|---:|
  | 300 | 1,270.6 | 1,275.7 | 414.6 |
  | 900 | 4,070.5 | 4,019.8 | 792.1 |
  | 1800 | 8,740.2 | 8,255.8 | 1,274.0 |

  Mean deltas vs `schema`: `semantic` p50 −63.9%, p99 −82.5% (cost side: candidate L0 +86.3%, L0 files +93.4%). This dynamic tail-latency result is the strongest latency evidence; full 6-checkpoint trace in plot-data.
- **RQ4 — Do schema evolution and dynamic deltas preserve correctness?** W13: ten schema-evolution tests pass; old segments stay readable across epochs; mixed schema + snapshot + tombstone deltas survive compaction and reopen (§6).
- **RQ5 — What write/storage/maintenance cost is visible?** W6 maintenance table (store/manifest/L0/import wall, RSS); RSS for budgeted/schema is on par with `naive` (Gate 2 = GO; ~0.99x / ~0.91x).
- **RQ6 — Does semantic-aware compaction retain the pruning surface at bounded cost?** Controlled C2 microbenchmark, naive vs semantic L1→L2 merge over identical exact inputs (Gate-C2 = GO):

| scale | policy | retention | read after vs before | output segs | write_amp | mismatches |
|---|---|---|---|---|---|---|
| SF1 | naive | 0.0 | **4× blow-up** | 1 | 1.22 | 0 |
| SF1 | semantic | 1.0 | flat | 4 | 1.87 | 0 |
| SF10c | naive | 0.0 | **6× blow-up** | 1 | 1.14 | 0 |
| SF10c | semantic | 1.0 | flat | 6 | 1.85 | 0 |

Semantic merge keeps `exact_surface_ratio = 1.0` and read cost flat; naive merge collapses keys (retention 0.0) and inflates reads by ≈ #edge-types. The cost stays bounded: output segments equal the number of active partitions, and write_amp is only modestly higher (≈1.85 vs ≈1.14, <2×). **This retention result is confirmed on real LDBC SF30** (1.09B edges, 40 (src_label,edge_type) partitions → 528 exact L1 segments): semantic retention 1.0 vs naive 0.0, write_amp 1.24 vs 1.07. A metadata-level replay over the 40 real typed-neighbor partitions bridges the read-amp consequence to SF30: naive requires 93.6 candidate segments/query and 282.16 GB aggregate candidate bytes (6.52× weighted proxy), while semantic requires 13.2 segments/query and 43.25 GB (1.00×).

**Evaluation boundary.** SF100 read-amp/candidate/RSS/correctness are measured (W6). Latency is workload-dependent (Gate 1 = FALLBACK) and reported as supporting. C2 combines controlled full-read rows with real-SF30 retention/write-cost rows and a metadata-level SF30 replay; it is not a production scheduler or full end-to-end SF30 read trace.
