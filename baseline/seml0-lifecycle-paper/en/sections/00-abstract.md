# SemL0: Query-Semantic Lifecycle Management for LSM-Based Dynamic Property Graphs

*Single-file working draft (md-only, Path B / lifecycle framing). Assembled 2026-06-18 from `paper/sections/`. Claims are bounded per `paper/sections/09-limitations.md`; latency is supporting only (Gate 1 = FALLBACK), C2 retention is GO (`baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`).*

## Abstract

LSM-based stores keep dynamic property graphs write-friendly, but their read path is largely query-semantics-blind: overlapping L0 — and, after compaction, cross-level — segments are probed even when the graph predicate (label, edge type, direction, degree, property) proves them irrelevant.

SemL0 turns a property-graph query signature into a lifecycle control signal: flush materializes a per-segment **semantic pruning surface**, and a segment is pruned only when its metadata *proves* it cannot hold a visible match, else it is read conservatively.

Because naive compaction collapses semantic partitions into mixed segments and erodes this surface, SemL0 makes compaction semantics-aware: feedback selects costly partitions and semantic merge **retains or rebuilds** the surface across levels, with the read benefit accounted against rewrite cost.

Under tombstones, degree change, additive schema evolution, and snapshot-visible deltas, SemL0 prunes only under exact evidence and otherwise reads conservatively, preserving a **no-false-negative invariant** across compaction and reopen.

On LDBC SNB up to SF100, SemL0 cuts candidate segments and read bytes with zero correctness mismatches; budgeted/schema variants keep memory on par with a semantics-blind baseline, while unbudgeted full semantic materialization is reported as a stress point. Latency gains are workload-dependent, so we report pruning-surface retention and its write-amplification cost rather than claiming uniform speedups.

## Contributions

- **C1 — Query-semantic pruning surface.** Property-graph query signatures (source label, edge type, direction, degree class, destination label, property presence, snapshot/schema epoch) are exposed to the LSM read path as segment-level metadata, with an exact/conservative/unknown completeness model that prunes only on proof of disjointness or absence, and a prune-or-keep reason taxonomy.
- **C2 — Lifecycle retention of the pruning surface (keystone).** The surface is treated as state that must survive the whole LSM lifecycle: flush generates it, feedback identifies costly partitions, and semantic-aware compaction retains or rebuilds it across levels, pairing read benefit with explicit rewrite/write-amplification cost.
- **C3 — Snapshot- and schema-correct semantic pruning.** Pruning is DB-safe under tombstones, degree change, additive schema evolution, and snapshot deltas: three invariants (Segment Schema / Epoch-Aware Resolution / Conservative Pruning) and a no-false-negative theorem.
