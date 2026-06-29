## 5. Lifecycle Retention of the Pruning Surface

> Contribution C2 (keystone). Reframed from "feedback-driven semantic compaction":
> the query-semantic pruning surface (§4) is not an L0-only artifact — it is state
> that must survive the LSM lifecycle. This section shows that ordinary compaction
> silently erodes it, that a semantics-aware merge retains or rebuilds it, and that
> the cost of doing so is bounded and explainable.
>
> Evidence basis: Gate-C2 = GO (`baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`).
> Controlled microbenchmark at SF1 and SF10-class scale isolates the merge policy.

### 5.1 Why compaction erodes semantic pruning surfaces

A segment's pruning power on the topology axis depends on two keys being concrete:
its `src_label` and its `edge_type_partition` (§4). When a level merge combines
input segments that span multiple source labels or edge types, the segment writer
deterministically records the sentinels `UNKNOWN_SOURCE_LABEL` / `MIXED_EDGE_TYPE`
for the merged output (it preserves a key only when *every* input edge agrees on
it). The completeness flag stays `Exact` — the segment exactly knows it is mixed —
but the *keys are gone*, so `signature_pruning_decision` can no longer prove the
segment disjoint from a label- or edge-type-selective query. A query that could
previously skip the segment must now read it in full.

Thus a size-only ("naive") compaction, which merges by byte budget without regard
to query semantics, collapses N single-partition segments into one mixed segment
and converts every later selective read over those partitions into a full-segment
read. The pruning surface built at flush time is destroyed by maintenance that is
blind to it.

### 5.2 A semantics-aware merge policy

SemL0's level merge is parameterized by a `LevelMergePolicy`; with
`semantic_partition_outputs` enabled it groups the merge's live records by
`(src_label, edge_type)` and emits one output segment per group, instead of a
single size-bounded run. Because the writer re-derives metadata per output, each
output segment recovers concrete keys and an `Exact` topology state. This has two
consequences:

- **Retention.** Inputs that were already exact stay exact across the merge.
- **Rebuild.** If some inputs were *already* mixed (e.g. produced by a prior naive
  merge), re-partitioning by `(src_label, edge_type)` and re-deriving metadata can
  *raise* the exact-surface ratio above the input's — the merge rebuilds a surface
  that maintenance had previously lost.

The output segment count therefore equals the number of *active* semantic
partitions, which is bounded by the schema (number of distinct edge types per
label), not by data volume.

### 5.3 Feedback-driven selection of costly partitions

The same runtime feedback that drives L0 compaction (query counts, candidate
fanout, read-byte and rewrite-size proxies; §4, and the controlled workload-shift
evidence in §7, W7) selects *which* partitions are worth rewriting first, so
retention effort is spent where read amplification is actually being paid. We do
not claim a globally optimal or production-grade background scheduler; the loop is
an event-driven, conservative-correct maintenance mechanism.

### 5.4 Retention metrics and cost accounting

We attach a measurement primitive to every level merge. Each segment is summarized
by an orthogonal `SegmentSemanticState` (topology / degree / property / tombstone /
schema), and the inputs (`selected_source ∪ selected_target`) and outputs of a
merge are each aggregated into a `PruningSurfaceSummary`. The **headline** metric is
the edge-weighted `exact_surface_ratio` (fraction of edges living in a
topology-`Exact` segment); `pruning_retention` is its after/before ratio. Crucially,
before and after are measured on the *same* edge population (the merge is lossless
modulo tombstone GC), so the comparison is apples-to-apples.

Benefit is never reported without cost. The same per-merge record carries
`input_bytes`, `output_bytes`/`rewrite_bytes`, `output_segments`, and
`logical_update_bytes` (live edge payload), from which we derive
`write_amp = rewrite_bytes / logical_update_bytes`. The C2 results table
(`tables/table-c2-retention-schema.md`) places retention and cost columns
side by side by construction.

### 5.5 Results: retention vs rewrite cost

We build N exact `(Person, edge_type)` L1 segments and run the *same* L1→L2 merge
under both policies, comparing the pruning surface and a typed-neighbor workload
before and after. Two scales (SF1-class: 4 edge types; SF10-class: 6 edge types,
240k edges):

| metric | naive (SF1 / SF10c) | semantic (SF1 / SF10c) |
|---|---|---|
| `pruning_retention` | 0.0 / 0.0 | 1.0 / 1.0 |
| `exact_surface_ratio` after | 0.0 / 0.0 | 1.0 / 1.0 |
| read bytes, after vs before | **4× / 6× blow-up** | flat / flat |
| `output_segments` | 1 / 1 | 4 / 6 (= # partitions) |
| `write_amp` | 1.22 / 1.14 | 1.87 / 1.85 |
| correctness mismatches | 0 / 0 | 0 / 0 |

Three findings hold across scale:

1. **Retention is decisive.** Semantic merge keeps `exact_surface_ratio = 1.0`;
   naive merge drops it to `0.0`, collapsing all partitions into one mixed segment.
2. **Read amplification scales with partition count.** After a naive merge, the
   typed-neighbor workload reads ≈ `#edge_types` more bytes (4× at 4 types, 6× at 6
   types), because the mixed segment cannot be edge-type-pruned; after a semantic
   merge, read bytes are unchanged.
3. **Cost is bounded and explainable.** Semantic merge's `output_segments` equals
   the number of active semantic partitions (4, 6), not an unbounded explosion, and
   `write_amp` is modestly higher (≈1.85 vs ≈1.14) due to per-segment header /
   offset / bloom overhead across N segments — well under 2× and far smaller than
   the read benefit it preserves. All variants are correct (0 mismatch).

**Real SF30 bridge.** On a real LDBC SF30 store (1.09B edges, 40
`(src_label, edge_type)` partitions, 528 exact L1 segments), the same open-mode
merge confirms surface retention and cost: semantic retention 1.0 vs naive 0.0,
write_amp 1.24 vs 1.07. We additionally replay the 40 typed-neighbor partitions
over the real post-merge segment metadata. This metadata-level proxy shows naive
mixed outputs require 93.6 candidate segments/query and 282.16 GB of candidate
bytes in aggregate (6.52× weighted read-amp over exact partition bytes), while
semantic outputs require 13.2 candidate segments/query and 43.25 GB (1.00×).

**Claim boundary.** The controlled rows measure a full read workload and correctness;
the real SF30 rows measure retention, write cost, and metadata-level candidate
segments/bytes. They do not claim a full end-to-end SF30 read workload with body
decode. We therefore claim that semantic-aware compaction *retains or rebuilds* the
pruning surface and removes the read amplification that naive merge introduces, at a
*bounded, explainable* write cost — not "negligible overhead", not optimality, not a
production-grade scheduler.

### Evidence map
- Gate verdict: `baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`
- Results: `baseline/path-b-c2/stage5-scale-summary-20260618-cn.md`, `stage4-smoke-summary-20260618-cn.md`, raw `stage4-smoke-sf1-20260618.json` / `stage5-scale-sf10class-20260618.json`
- Real SF30 proxy: `remote-logs/c2-sf30-readamp-proxy-20260621/{naive,semantic}-proxy.json`
- Table schema: `tables/table-c2-retention-schema.md`
- Code: `LevelMergePolicy` + `compact_level_to_next` + `split_semantic_compaction_segments` (src/graph.rs); `SegmentSemanticState` + `PruningSurfaceSummary` (src/csr/format.rs); runner `src/bin/c2_merge_retention.rs`
