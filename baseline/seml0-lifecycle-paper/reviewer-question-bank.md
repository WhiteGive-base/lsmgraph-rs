# Reviewer Question Bank (rebuttal prep)

Anticipated reviewer questions for SemL0 (Query-Semantic Lifecycle Management), with
prepared, claim-bounded answers. Keep answers consistent with §9 Limitations.

## Q1. "Read bytes drop, but latency doesn't uniformly improve — so what?"
SemL0's primary objective is reducing read amplification and preserving the semantic
pruning surface *safely*, not uniform latency. End-to-end latency also depends on
metadata lookup, cache state, async scheduling, and body decode; we therefore report
latency as supporting evidence with attribution. The durable wins are fewer candidate
segments / read bytes (§4), surface retention across compaction (§5), and bounded,
explainable cost — all robust regardless of latency variance. On semantically
selective workloads latency does improve; we do not over-generalize.

## Q2. "How is this different from compaction tuning / learned LSM?"
Those tune size ratios / triggers / bloom bits on *size and frequency* signals over
opaque KV records. SemL0's control signal is a *property-graph query signature* and
its maintained object is a *segment-level semantic pruning surface* with exact-proof
safety. It is orthogonal and composable with standard compaction tuning (§8).

## Q3. "Does semantic merge blow up write amplification / file count?"
Cost is reported in the same table as benefit (§5). `output_segments` equals the
number of *active* semantic partitions (bounded by schema), not unbounded explosion;
`write_amp` is modestly higher (≈1.85 vs ≈1.14) from per-segment overhead across N
segments — under 2×, far smaller than the read amplification it prevents (4–6×).
On real SF30 metadata replay, semantic keeps the weighted candidate-byte proxy at
1.00× while naive rises to 6.52×, so the extra write cost is tied to a concrete
read-amplification mechanism rather than a synthetic-only artifact.

## Q4. "After a schema change, is old storage invalidated / can you miss edges?"
No. Additive changes route only future writes; old segments keep their `schema_epoch`
and stay readable. Pruning uses stable edge-type ids, so a new-label query auto-skips
old exact segments. When exactness is unprovable (epoch/encoding/tombstone), SemL0
degrades to conservative reads. We prove a no-false-negative invariant (§6): pruning
can only cause extra reads, never a missed visible edge.

## Q5. "External SOTA baseline?"
We compare against **LiveGraph** (an external transactional graph store) at SF10 —
measured load (1,502 s), footprint (≈45.7 GiB RSS, 37.58 GB block + 1 GB WAL), and
typed-neighbor scan latency (§8). LiveGraph SF100 load was infeasible (~17–21 days),
so the external comparison is **SF10-only** — an honest scope limit, not an omission.
Layout-level systems (LSMGraph's multi-level CSR; BACH's adjacency↔CSR transformation)
are orthogonal and composable rather than head-to-head competitors. Internal read-amp
scale evidence uses LDBC up to SF100 (§7); C2 retention is a naive-vs-semantic ablation.

## Q6. "Is C2 real or synthetic?"
C2 is a controlled microbenchmark that fixes partition structure to isolate the merge
policy — the right tool for a compaction-behavior claim — **and it is confirmed on real
LDBC SF30** (1.09B edges, 40 partitions → 528 exact L1 segments: retention 1.0 vs 0.0,
write_amp 1.24 vs 1.07), consistent with synthetic SF1/SF10c (0 mismatch). The real-SF30
run is open-mode (surface + write cost; the read-amplification consequence is shown on
the synthetic rows and bridged to real SF30 by metadata replay, §9). The replay covers
40 typed-neighbor partitions and shows naive 93.6 candidate segments/query and 6.52×
weighted candidate-byte read-amp vs semantic 13.2 and 1.00×. It is not a full body-read
SF30 workload, and we state that boundary.

## Q7. "Does the 118 GiB full-semantic RSS mean the approach is impractical?"
No, because the 118.03 GiB row is the unbudgeted full-materialization stress point,
not the intended operating point. SemL0's design includes benefit scoring and budgets
precisely because full semantic materialization can hit a fanout/RSS cliff. The main
claim is therefore budgeted lifecycle control: budgeted/schema variants stay near the
semantics-blind RSS envelope while retaining useful pruning, and C2 shows that the
surface can be preserved across compaction at explicit bounded write cost.
