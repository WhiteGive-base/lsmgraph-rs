## 9. Limitations

We state limitations explicitly so the claims in §4–§6 are read at the right
strength. These are derived from the submission gates
(`SEML0-K4-STATUS-AND-PLAN-20260617-CN.md`, `baseline/path-b-c2/...`).

- **Latency is a supporting signal, not a headline.** Read-amplification and
  candidate-segment reductions are robust (§4), but end-to-end latency improvement
  is workload-dependent: it appears on semantically selective reads and is not a
  uniform speedup (Gate 1 = FALLBACK). Latency additionally depends on metadata
  lookup, cache state, async scheduling, and body decode; we report it with
  attribution rather than claiming uniform gains.
- **Composite vs single-axis semantics.** We do not claim that composite semantic
  partitioning is stably better than edge-type-only partitioning; the necessity
  experiment is only partial (W14). The safe statement is that edge-type is one
  exact axis of a more general signature.
- **C2 retention is a controlled microbenchmark.** The lifecycle-retention results
  (§5) fix the partition structure to isolate the merge policy. They show retention
  and read-amplification removal at a *bounded, explainable* write cost
  (`write_amp` ≈1.85 vs ≈1.14; `output_segments` = number of active semantic
  partitions). We do not claim negligible overhead, global optimality, or a
  production-grade background scheduler. A real LDBC SF30 retention run (1.09B edges,
  two store copies, naive vs semantic) confirms retention 1.0 vs 0.0 (write_amp 1.24 vs
  1.07). A metadata-level replay over the real SF30 post-merge segment metadata shows
  the same read-amplification mechanism at SF30 scale (naive 6.52× weighted candidate-byte
  proxy vs semantic 1.00×), but full end-to-end SF30 read execution with body decode and
  broader real-data sweeps remain future work.
- **External comparison is SF10-only.** We compare against an external graph store
  (LiveGraph) at SF10 (§8; load 1,502 s, ≈45.7 GiB RSS, scan ≈2.74 µs over positive
  core edge types); LiveGraph SF100 load was infeasible (~17–21 days), so the external
  comparison does not extend to SF100. The RocksDB-*style* KV-LSM and KV-style rows are
  an internal layout baseline and a simulation, not external systems.
- **Feedback is controlled evidence.** Workload-shift adaptation is demonstrated on
  controlled/real-SF30-derived runs, not a full-store production trace.
- **Schema scope is additive + fixed-width.** Correctness holds for additive schema
  changes, fixed-width property equality, presence/absence, alias/tombstone/snapshot
  and epoch/encoding-epoch resolution (§6). Arbitrary rename/drop/type-change
  physical migration, range/string/compound predicates, and online full-store
  rewrite are out of scope.
- **No production write-stall characterization.** Background maintenance is
  event-driven and conservative; we do not characterize tail-latency under
  sustained production write pressure.
- **Submission packaging.** Final venue, template, TeX/PDF, page budget, and PDF
  visual inspection remain open engineering steps (Stage 10), not experimental gaps.
