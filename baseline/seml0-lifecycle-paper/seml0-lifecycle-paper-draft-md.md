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
## 1. Introduction

Dynamic property graphs — social networks, knowledge graphs, financial transaction graphs — must absorb a continuous stream of edge and property updates while still answering selective reads quickly. Log-structured merge (LSM) storage suits the write side: updates are buffered, flushed as immutable segments, and reorganized by background compaction. The read side pays for this: recent L0 segments overlap in key space, so a query touching a single source vertex may probe many segments, most of which cannot contain a relevant edge.

The usual framing treats this as a *physical adjacency locality* problem; we argue it is also a *semantics* problem. A property-graph query almost always exposes structure before the engine reads any segment body: source and destination labels, edge type, traversal direction, a degree class, a required property, and the schema epoch it resolves against. An engine blind to these cannot skip a segment it could provably never match.

SemL0's thesis is that a property-graph query signature should act as a **lifecycle control signal** for an LSM-based graph store. At flush time the engine records a per-segment *semantic pruning surface*; at read time it prunes a segment only when its metadata proves the segment disjoint from the query, and otherwise degrades to a conservative read. This surface must also be *maintained*: naive, semantics-blind compaction collapses distinct partitions into mixed segments, silently destroying the surface and reintroducing read amplification. SemL0 therefore makes compaction semantics-aware — retaining or rebuilding the surface across levels — and keeps the whole loop conservative-correct under updates and schema evolution.

We contribute C1 (the pruning surface), C2 (its lifecycle retention — the keystone), and C3 (snapshot/schema correctness), summarized above and evaluated in §7. We are deliberate about claim strength: read-amplification and candidate reductions are robust and safe; end-to-end latency is workload-dependent and reported as supporting evidence, not a uniform speedup.

We do **not** position SemL0 as a complete graph DBMS, a full schema-migration engine, a production write-stall study, or an end-to-end/production performance report.
## 2. Background and Problem

**LSM graph layout.** Writes land in an in-memory buffer and are flushed to immutable CSR-like L0 segments; background compaction merges segments down a level hierarchy. L0 segments overlap in source-vertex space, so a read may probe many of them. Compaction reduces overlap but, if blind to query semantics, yields fewer yet *coarser* segments.

**Graph query signatures.** Unlike opaque key-value reads, a property-graph access carries semantics: source label, edge type, direction, degree class, destination label, property presence/value, snapshot, and schema epoch. These are available before any segment body is read.

**False-negative risk.** Pruning is useful only if it is *safe*. A segment may be skipped solely when its metadata *proves* it cannot contain a visible match for the query; otherwise it must be read. A merely "probably irrelevant" decision is unacceptable, because a missed edge is a silent correctness bug.

**The shared safety contract** (used by every later section):

> Prune a segment only when its metadata proves disjointness or absence; otherwise read conservatively.

Everything in §4–§6 is a way to (a) record enough metadata to make such proofs at flush time, (b) preserve that metadata through compaction, and (c) fall back to conservative reads exactly when a proof is unavailable — under mixed/unknown metadata, tombstones, or uncertain schema epochs.
## 3. System Overview

SemL0 is organized as four cooperating loops over the same semantic metadata.

1. **Write / flush.** The write path emits CSR-like L0 segments. At flush, the engine derives each segment's semantic metadata — source/destination label, edge-type partition, degree class, property presence, schema epoch — and an exact/conservative/unknown completeness flag (§4). This is where the pruning surface is created.

2. **Read / prune.** The read path compiles a query predicate into a storage-facing `GraphAccessSignature` and, for each candidate segment, calls a pruning decision that either proves the segment disjoint (skip, recording the reason) or keeps it for a CSR bloom/offset/body probe (§4). Unprovable cases fall back to conservative reads.

3. **Feedback / compaction.** Runtime metrics (query counts, candidate fanout, read and rewrite proxies) feed an event-driven maintenance scheduler. It selects hot/costly semantic partitions and runs a semantics-aware merge that retains or rebuilds the pruning surface across levels rather than collapsing it (§5), recording retention and cost side by side.

4. **Schema / snapshot safety.** A versioned schema catalog and per-segment schema epoch let new schema interpretations coexist with old segments; tombstone and snapshot metadata bound what compaction may discard. Pruning degrades to conservative reads whenever epoch/encoding/tombstone interactions make exactness unprovable (§6).

The same metadata object — summarized as an orthogonal `SegmentSemanticState` (topology / degree / property / tombstone / schema) — is produced at flush, consumed at read, measured at compaction, and resolved against the schema catalog. Maintaining this single surface across the whole lifecycle is what distinguishes SemL0 from L0-only semantic indexing.
## 4. The Query-Semantic Pruning Surface (Contribution C1)

> Path B / lifecycle framing. This section is **Contribution C1: generating and
> exploiting the query-semantic pruning surface**. It retains the layout and
> metadata mechanisms of the earlier "physical design" draft but re-narrates them
> as the *creation* of a per-segment pruning surface at flush time and its
> *exploitation* on the read path. The surface created here is the object that
> Section 5 (C2) must retain across compaction and that Section 6 (C3) must keep
> conservative-correct under schema and snapshot change. Evidence reuses W6, W8,
> and W14.

**Reader question.** *Can a property-graph query signature, made visible to LSM
segment metadata, materially reduce read amplification without changing query
results?*

**Answer (bounded).** Yes for candidate-segment and read-byte reduction with
zero correctness mismatch on LDBC SNB up to SF100 (W6); latency is reported as a
supporting observation only, because its measured advantage does not clear the
significance gate (Gate 1 = FALLBACK).

### 4.1 From query predicate to a storage-facing access signature

LSM graph stores are update-friendly, but their read path is largely
query-semantics-blind: overlapping L0 segments — and, after compaction,
cross-level segments — are probed even when the graph predicate proves them
irrelevant. SemL0's first step is to make the predicates that matter to a storage
lookup visible to the layout, so that a neighbor scan over one edge type does not
have to treat every recent edge segment as an equally plausible candidate.

The mechanism is the `GraphAccessSignature` (`src/semantic.rs`). It is *not* a
logical query plan; it is the subset of query semantics that can guide physical
pruning while remaining cheap to evaluate against segment metadata. A signature
carries a source label, an optional edge type, a direction
(`EdgeDirection::{Out, In, Both, Unknown}`), an optional degree class
(`DegreeClass`), an optional destination label, an optional timestamp window
(`min_ts`/`max_ts`), and an optional property predicate (`PropertyPredicate`).
A neighbor scan is constructed with `GraphAccessSignature::neighbor_scan(src,
edge_type)` and refined with builders such as `with_degree_class`,
`with_dst_label`, `with_required_property`, and `with_absent_or_default_property`.
The segment metadata then answers a single storage question: *can this segment
hold an edge that is visible to this signature?*

The contract throughout the paper is conservative and asymmetric:

> A segment is pruned only when its metadata *proves* it cannot hold a visible
> match for the signature; in every other case it is kept and read
> conservatively.

This asymmetry is what lets C1 compose with the later sections. Section 5 (C2)
may change *which* semantic ranges are merged first across levels, but it may not
make pruning less conservative. Section 6 (C3) may advance catalog epochs for
future writes, but old segment summaries are still interpreted through their
recorded `schema_epoch`. The cost of a degraded surface is therefore always
*extra reads*, never a false negative.

### 4.2 Per-segment semantic metadata: the pruning surface

The pruning surface is materialized at flush time as semantic metadata on every
CSR-like segment. Each segment is described by a `CsrSegmentMeta`
(`src/csr/format.rs`) that records, among other fields: `src_label` and
`dst_label`; the `edge_type_partition` (a concrete edge type, or the
`MIXED_EDGE_TYPE` sentinel); `direction`; a `degree_class` with a
`degree_class_exact` flag; a `property_presence_bitmap` with a
`property_summary_completeness` flag; the `may_contain_tombstones` flag; the
`schema_epoch` and `property_encoding_epoch` under which the summary was built;
and the timestamp window `[min_ts, max_ts]`. These per-dimension descriptors,
taken over the whole segment set, *are* the query-semantic pruning surface that
the read path exploits.

A freshly written, single-partition segment carries concrete keys and an exact
summary, so it contributes a sharp pruning surface. A segment produced by mixing
unrelated partitions (for example, the output of a naive merge over several edge
types) collapses its keys to the sentinels and contributes a *blurred* surface
that prunes less. Crucially, the segment is never *wrong* — it is only less
selective.

#### Exact, conservative, and unknown completeness

Pruning power is governed by an explicit three-valued completeness flag,
`SemanticSummaryCompleteness` (`src/schema.rs`): `Exact`, `Conservative`, or
`Unknown`. Only `Exact` and `Conservative` summaries
(`allows_semantic_pruning()`) may participate in pruning; `Unknown` summaries
force a conservative read. This yields three operational cases for the read path:

| Segment metadata state | Read-path action |
|---|---|
| exact match for the signature | keep as a candidate |
| exact disjointness, or exact absence of a required property | prune |
| mixed / unknown / legacy / tombstone-sensitive summary | keep conservatively |

The three-valued design is what makes the surface safe to expose: the system can
materialize an aggressive `Exact` summary where it can prove disjointness and
*fall back* to `Conservative`/`Unknown` everywhere it cannot, rather than risking
a false negative.

### 4.3 The semantic L0 index and the degree directory

To exploit the surface, SemL0 builds a `SemanticL0Index` (`src/graph.rs`) over
recent CSR segment metadata. The index groups L0 segments by signature
dimensions, so a lookup bound to a specific edge type (and, where available,
degree class) can be routed directly to the matching exact partitions instead of
scanning unrelated ones; mixed and unknown groups are still folded into the
candidate set, preserving the conservative fallback.

Degree-aware pruning matters because graph neighborhoods are heavily skewed: a
high-degree source and a low-degree source have very different
read-amplification profiles. SemL0 records `DegreeClass` summaries
(`Low`/`Medium`/`High`, with an explicit `Unknown`) and maintains a degree
directory that routes degree-sensitive lookups to the right segment classes.
A degree predicate is honored against a segment only when that segment's
`degree_class_exact` flag holds; otherwise the segment is *kept*, not pruned
(see the `budgeted_not_materialized` reason below). This is a physical-design
mechanism, not a new query-language feature: the user asks for neighbors, and the
storage layer decides which semantic partitions are safe candidates.

### 4.4 Property presence and its boundary

Property-graph queries frequently require more than topology. SemL0 records a
`property_presence_bitmap` over representable property ids and uses it *only* for
safe absence reasoning: `definitely_lacks_property` returns true only when the
property summary allows pruning *and* the corresponding presence bit is clear.
The boundary is intentionally narrow — fixed-width, low-id property presence and
required-property predicates, where `property_presence_bit` maps a property id to
a bit only for ids below 64. Property ids outside the representable range, or
segments whose property summary is incomplete, are read conservatively. The
paper must not generalize this to string, range, or compound predicates, nor to
full SQL null semantics.

### 4.5 The prune-or-keep reason taxonomy

The pruning surface is exploited through a single decision procedure,
`CsrSegmentMeta::signature_pruning_decision` (`src/csr/format.rs`), which returns
a `SignaturePruningDecision { pruned, reason }`. The reason is a fixed
vocabulary; making it explicit is what lets us attribute, audit, and measure the
surface rather than treat pruning as a black box. The taxonomy has two families.

**Prune reasons (the surface proved disjointness or absence).** Evaluated in the
order the decision procedure applies them:

| Reason | Pruned because |
|---|---|
| `time` | the segment timestamp window is disjoint from the signature's `[min_ts, max_ts]` |
| `src_label` | the signature's source label cannot match the segment's `src_label` |
| `edge_type` | the segment is a concrete partition for a different edge type than requested |
| `direction` | the segment direction is disjoint from the requested direction |
| `degree` | the segment's exact degree class cannot contain the requested degree class |
| `dst_label` | the segment's concrete destination label cannot match the requested one |
| `property_absence` | an exact property summary proves a required property is absent |

**Keep reasons (the surface could not prove disjointness, so the segment stays a
conservative candidate).** These are the safety valves that guarantee no false
negative:

| Reason | Kept because |
|---|---|
| `mixed_unknown_fallback` | the topology or property summary is not `Exact`/`Conservative`, so no semantic pruning is permitted |
| `budgeted_not_materialized` | a degree predicate is present but the segment's degree class was not materialized exactly (a benefit-budget choice; see §4.6) |
| `schema_tombstone_fallback` | an exact summary would prune on property absence, but the segment may contain tombstones, so it is read conservatively |
| `kept_candidate` | the segment survived every disjointness test and is a genuine candidate |

This taxonomy is the operational definition of the pruning surface: each *prune*
reason is a dimension along which the surface is sharp, and each *keep* reason is
a place where the surface is deliberately blurred to stay correct. The same
vocabulary is reused by Section 5 to measure whether compaction *retains* a sharp
surface or degrades it into `mixed_unknown_fallback` territory, and by Section 6
to argue that schema and snapshot changes only *move* mass from prune reasons
into keep reasons (precision loss), never the reverse (a false negative).

### 4.6 Benefit-scored materialization

Materializing an exact partition for every signature dimension would maximize
pruning but inflate segment fanout and metadata cost. SemL0 therefore uses a
*benefit-scored* materialization policy (`src/graph.rs`): it estimates whether
preserving an exact semantic partition is worth its added file and metadata
fanout, using dimensions such as edge type, degree class, and segment size, and
otherwise merges low-benefit partitions into mixed segments that are read
conservatively. The `budgeted_not_materialized` keep reason above is the runtime
trace of this policy: where the budget declined to materialize an exact degree
class, the read path keeps the segment instead of pruning it. Benefit scoring
turns C1 from "materialize everything" into a bounded layout policy and is the
reason the surface can stay sharp where it pays without unbounded fanout.

### 4.7 Evidence and claim boundary

**Read-amplification and candidate reduction with zero mismatch (W6, SF100).**
The W6 SF100 matrix (`baseline/sf100-matrix-20260613-cn.md`) reports, over
45,000 ops per repeat, that semantic-pruning variants sharply cut candidate L0
segments and read bytes relative to the semantics-blind `naive` baseline, while
every correctness compare reports `mismatches=0`. The `naive`/`kv-lsm` baselines
probe on the order of 49 million (naive mean 49,257,601)
candidate L0 segments, whereas `schema`, `edge-type-only`, and budgeted variants
probe roughly an order of magnitude fewer; the `semantic` variant achieves the
lowest measured read bytes. RSS for the budgeted and schema variants is on par
with `naive` (Gate 2 = GO; schema/naive ~0.99x, budg-b64/naive ~0.91x), so the
surface does not come at a memory-footprint cost for those variants. (The
unbudgeted `semantic` variant's import RSS is an outlier and is reported as such,
not as a main claim.)

**Property and 2-hop coverage (W8, SF30).**
`baseline/w8-property-2hop-summary-20260615-cn.md` shows that, for property
presence / equality / absent-default predicates, budgeted and semantic variants
reduce candidate L0, body reads, read bytes, and elapsed time versus `schema`
(candidate L0 about 22%
lower: -21.8% semantic, -22.0% budg-b64). For 2-hop typed expansion, body reads, read bytes, and elapsed time
improve, **but** candidate L0 is higher than `schema`. We therefore do **not**
claim universal 2-hop candidate reduction; the defensible 2-hop result is the
body/read-byte/latency reduction.

**Necessity of composite semantics (W14, partial).**
`baseline/w14-final-verdict-20260617-cn.md` establishes SF100 reuse-store
correctness parity (`mismatches=0`) across edge-type-only, budgeted, and semantic
variants for property-required and degree-class scenarios, and shows that pruned
variants avoid the schema-baseline cliff. It does **not** prove that composite
semantics materially beat edge-type-only on the completed scenarios. The paper
must keep this as a parity / dilution-diagnosis result, not a "richer semantics
is stably better" claim.

**Safe wording for C1.** SemL0 makes graph query signatures visible to LSM
segment metadata and exploits exact summaries, degree-aware routing,
property-presence summaries, and benefit-scored materialization to reduce
candidate segments and read amplification on SNB up to SF100 with zero
correctness mismatch.

**Do not write.** "SemL0 always improves latency"; "composite semantics is
stably better than edge-type-only"; "negligible write overhead"; "arbitrary
property predicates are indexed"; "mixed/unknown metadata can be pruned"; or any
"complete graph database" framing.

### 4.8 Bridge to lifecycle retention (C2)

C1 *creates* a query-semantic pruning surface at flush time and shows it can be
exploited safely. But this surface is a flush-time artifact: naive cross-level
compaction collapses concrete partition keys into the `MIXED_EDGE_TYPE` /
unknown-label sentinels, shifting prune reasons into `mixed_unknown_fallback` and
eroding the surface. Section 5 (C2) makes compaction semantics-aware so the
surface is *retained or rebuilt* across levels, with read benefit accounted
against rewrite cost.

### Evidence map

- `baseline/sf100-matrix-20260613-cn.md` — W6 SF100 matrix: candidate-L0 /
  read-byte reduction vs `naive`, all compares `mismatches=0`, RSS (Gate 2 = GO),
  latency Gate 1 = FALLBACK.
- `baseline/w8-property-2hop-summary-20260615-cn.md` — W8 SF30: property-predicate
  reductions; 2-hop body/read/latency gains with mixed 2-hop candidate behavior.
- `baseline/w14-final-verdict-20260617-cn.md` — W14 (partial): SF100 reuse-store
  correctness parity; composite-vs-edge-type-only necessity unproven.
- `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md` — Path B framing; C1 = reuse +
  re-narrate; main claim = read-amp + safety, latency supporting only.
- Code anchors: `GraphAccessSignature` (`src/semantic.rs`); `CsrSegmentMeta`,
  `signature_pruning_decision`, `SignaturePruningDecision`,
  `SegmentSemanticState`, `PruningSurfaceSummary` (`src/csr/format.rs`);
  `SemanticSummaryCompleteness` (`src/schema.rs`); `SemanticL0Index` +
  degree directory + benefit-scored materialization (`src/graph.rs`).
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
## 6. Schema-Snapshot Correctness of the Pruning Surface (Contribution C3)

> Path B / lifecycle framing. This section is **Contribution C3: keeping the
> query-semantic pruning surface conservative-correct under additive schema
> evolution, tombstones, degree change, and snapshot-visible deltas.** The
> mechanisms are already strong (W13 plus the lifecycle/reopen tests); the Path B
> re-narration adds (i) the three structural invariants and (ii) the
> no-false-negative theorem that bound the entire pruning surface from Section 4
> and that any compaction retention scheme in Section 5 must preserve.

**Reader question.** *If SemL0's pruning surface depends on labels, edge types,
properties, degree classes, and schema metadata, does a schema change — or a
tombstone, a degree change, or a snapshot view — make old semantic storage
unsafe or useless?*

**Lead answer.** No. Additive schema changes advance catalog interpretation and
future-write routing; old segments remain readable under their original
`schema_epoch`. Exact metadata may still prune; uncertain metadata is read
conservatively. The cost of any schema or snapshot uncertainty is *extra reads*,
never a false negative or discarded storage.

### 6.1 Versioned schema catalog and segment `schema_epoch`

SemL0 separates *logical* schema evolution from *physical* storage layout. The
`SchemaCatalog` (`src/schema.rs`) assigns stable physical identifiers to vertex
labels, edge labels, and properties; records aliases, drop epochs, and an
encoding history; and advances a monotone `current_epoch` whenever the logical
schema changes (`add_vertex_label`, `add_edge_label`, `add_property`,
`alias_edge_label`, and the drop/encoding-change operations each bump the epoch).
Catalog entries carry `valid_from_epoch`, `valid_to_epoch`/`dropped_at_epoch`,
and (for properties) an `encoding_epoch`, so the resolver can answer "what did
identifier *x* mean at epoch *e*?" rather than only "what does it mean now."

Each CSR segment independently records the `schema_epoch` and
`property_encoding_epoch` under which its semantic summaries were built
(`CsrSegmentMeta`, `src/csr/format.rs`). A query compiled under the current
catalog therefore interprets an older segment *through that segment's own epoch*,
not through the latest epoch. The segment's `semantic_state(current_epoch)`
derivation classifies its schema dimension as `Current`, `OlderEpoch`, or
`Uncertain` (an epoch ahead of the catalog), and the read path treats
`OlderEpoch`/`Uncertain` as candidates rather than pruning on stale assumptions.

### 6.2 Additive schema change does not invalidate old storage

The central reviewer concern is whether adding a label or property forces a
global rebuild. It does not.

**Add an edge label.** SemL0 creates a catalog entry, exposes the *stable*
physical edge-type identifier, advances the epoch, and routes future writes
through the new epoch. Existing segments keep their older `schema_epoch`. Because
edge-type ids are stable, an old segment that is an *exact* partition for a
different edge type can be auto-skipped for a query over the new label — the
`edge_type` prune reason from Section 4 fires directly, without any rewrite. An
old segment that is mixed, unknown, tombstone-sensitive, or has incomplete
summary completeness is *kept* (the `mixed_unknown_fallback` /
`schema_tombstone_fallback` keep reasons). Adding a label is thus a catalog +
future-write event, not a store-invalidation event:

```text
add edge label  =>  catalog epoch advance + future-write routing
                =>  old exact-disjoint segments auto-skip (stable edge_type id)
                =/= old store invalidation
                =/= mandatory segment rewrite
```

**Add a property.** Adding a property records a property id, its
default-or-null rule, and an `encoding_epoch` for future rows; old rows are not
rewritten to add a physical column. A required-property predicate over the new
property can be exactly pruned only where a segment's exact property summary
proves absence; otherwise the segment is read conservatively, and a projection
returns the catalog-defined default/`NULL` without reading a nonexistent column.
This holds within the implemented fixed-width-property boundary only (W13).

This is the W13 result: `new_edge_label_prunes_exact_segments_but_reads_mixed_segments`
and `schema_epoch_change_keeps_old_segments_readable` both pass, confirming that
additive change shifts pruning *precision* without producing a false negative or
requiring a global rewrite
(`baseline/w13-schema-evolution-summary-20260614.md`).

### 6.3 Exact-proof pruning vs conservative fallback under schema change

The Section 4 contract is unchanged here: prune only on proof of disjointness or
absence; otherwise read conservatively. Schema evolution interacts with it in
exactly one direction — it can *lower* pruning precision by turning some prune
reasons into keep reasons until a later compaction refreshes hot metadata, but it
can never turn a keep into an unsafe prune. Concretely, a segment written before
an additive change resolves its summaries against its own epoch; if that epoch
no longer suffices to prove disjointness for the new predicate, the segment falls
into `mixed_unknown_fallback` (kept) rather than being skipped. Schema evolution
therefore changes *interpretation* before it changes *layout*: the catalog epoch
affects future writes immediately, while old bytes stay correct under their
recorded epoch.

### 6.4 Alias, drop, encoding, and degree-change boundaries (no over-claim)

Non-additive history is *representable* in the catalog, but the paper must not
inflate it into a full migration engine:

- **Alias / rename.** A logical rename is a catalog alias to a canonical
  physical id (`alias_edge_label`; `resolve_edge_label` honors the alias from its
  `valid_from_epoch`). No old segment is rewritten.
- **Drop.** A dropped property is hidden from current public value queries (its
  `dropped_at_epoch` makes `entry_is_visible_at` return false at the query epoch)
  while older topology and historically visible bytes remain readable where
  snapshot rules require them. Physical reclamation after drop is future work.
- **Encoding / type change.** An encoding change advances the property
  `encoding_epoch`, and a row is decoded under *its own* `property_encoding_epoch`,
  so old rows dispatch to the decoder appropriate for their stored encoding
  (W13: `property_encoding_change_advances_catalog_and_segment_epochs`,
  `public_property_value_query_uses_row_encoding_epoch_after_type_change`).
- **Degree change.** A source whose degree class changes over time is handled by
  the same conservative rule: a segment is pruned on a degree predicate only when
  its `degree_class_exact` flag holds; an uncertain or stale degree class is kept.

These cases demonstrate versioned interpretation, not arbitrary migration. Broad
rename/drop rewrite, online full-store rewrite, and general type migration remain
future work.

### 6.5 Snapshot / tombstone interaction and the compaction safe point

Schema and snapshot visibility must be handled together. A single segment can mix
visible inserts, hidden tombstones, old property encodings, and metadata written
under an older epoch. Two rules keep this safe:

1. **Tombstone-sensitive segments are never pruned on property absence.** When an
   exact summary would otherwise prune via `property_absence` but
   `may_contain_tombstones` is set, the decision falls back to
   `schema_tombstone_fallback` (kept). A segment that may delete an edge cannot be
   skipped, because the deletion is part of the visible answer.
2. **Compaction cannot collapse history past the snapshot safe point.** Merge may
   garbage-collect a tombstone only once no live snapshot can still observe the
   pre-deletion state; until then, both the insert and its tombstone must survive
   compaction and reopen. The W13 lifecycle tests
   `schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen` and
   `property_schema_snapshot_mixed_delta_survives_compaction_and_reopen` exercise
   exactly this: a mixed schema + snapshot + tombstone delta survives both
   compaction and engine reopen with correct results.

### 6.6 Three invariants and the no-false-negative theorem

The mechanisms above can be stated as three structural invariants that bound the
entire pruning surface created in Section 4 and that any compaction retention
scheme in Section 5 must preserve.

**Invariant I (Segment Schema).** Every segment records the `schema_epoch` and
`property_encoding_epoch` under which its semantic summaries and property
encodings were produced, and these are immutable for the life of the segment.
*(Anchor: `CsrSegmentMeta.schema_epoch` / `property_encoding_epoch`.)*

**Invariant II (Epoch-Aware Resolution).** A read interprets each segment through
its *own* recorded epoch via the versioned catalog (`valid_from_epoch`,
`dropped_at_epoch`, `encoding_epoch`), never through the current epoch alone; a
segment whose epoch differs from the catalog's is classified `OlderEpoch` or
`Uncertain` rather than assumed current. *(Anchors: `SchemaCatalog::resolve_*`,
`entry_is_visible_at`, `CsrSegmentMeta::semantic_state`.)*

**Invariant III (Conservative Pruning).** A segment is pruned only when an
`Exact`/`Conservative` summary *proves* disjointness or absence for the
signature; mixed, unknown, tombstone-sensitive, older-epoch, or uncertain
metadata is always kept. *(Anchor:
`CsrSegmentMeta::signature_pruning_decision`; `allows_semantic_pruning`.)*

**Theorem (No False Negative under schema and snapshot change).** *Let `S` be a
read whose `GraphAccessSignature` is compiled under catalog epoch `e`, and let
`g` be a segment that contains at least one edge visible to `S` under epoch `e`
and the read's snapshot. Then, under Invariants I–III, `signature_pruning_decision`
does not prune `g`; i.e. `g` is retained as a candidate and its rows are
validated. Consequently, additive schema changes, tombstones, degree changes,
and snapshot-visible deltas can reduce pruning precision (move mass from prune
reasons to keep reasons) but cannot remove a segment that holds a visible match.*

*Proof sketch.* Pruning occurs only through a *prune* reason of the Section 4
taxonomy. Each prune reason requires an `Exact`/`Conservative` summary
(Invariant III) interpreted at `g`'s own epoch (Invariant II) over immutable,
correctly-recorded metadata (Invariant I): `time` requires a disjoint timestamp
window; `src_label`/`dst_label`/`edge_type`/`direction`/`degree` each require a
proven key/class disjointness; `property_absence` requires an exact summary
proving the property is absent *and* the segment to be tombstone-clean (otherwise
`schema_tombstone_fallback` keeps it). If `g` holds an edge visible to `S`, none
of these disjointness proofs can succeed for that segment, so the procedure
reaches a *keep* reason. Schema/snapshot uncertainty can only *invalidate* a
disjointness proof (degrading `Exact` toward `Conservative`/`Unknown`, which
forces `mixed_unknown_fallback`), never manufacture one. Hence `g` is kept. ∎

This theorem is the safety contract for the whole lifecycle: Section 4's surface
is created under it, and any Section 5 compaction-retention scheme is only
*permitted* to change which keep/prune reason a segment lands in — it can blur or
sharpen the surface, but it cannot violate the theorem.

### 6.7 Evidence and claim boundary

The W13 suite (`baseline/w13-schema-evolution-summary-20260614.md`) reports ten
schema-evolution tests passing, covering: epoch advance on logical change; old-segment readability
across epochs; mixed schema + snapshot + tombstone deltas surviving compaction
and reopen; alias resolution; drop-property boundary; epoch-aware fixed-width
decoding; and exact-vs-mixed pruning under a new edge label.

**Safe claims.** Add-label / add-property schema changes do not invalidate old
storage; stable edge-type ids let old exact segments auto-skip; unknown / mixed /
tombstone-sensitive / older-epoch metadata causes *extra reads, not false
negatives*; compaction cannot collapse snapshot-visible history past the safe
point.

**Do not write.** Arbitrary or complete schema migration; online full-store
rewrite; automatic physical reclamation after drop; general type migration;
range / string / compound property predicates; SQL null semantics; negligible
schema-change overhead.

### 6.8 Figure hook and bridge

A timeline figure can show epoch `e0` (old segment written with `{KNOWS}`),
epoch `e1` (catalog adds `LIKES`), future `LIKES` writes entering new L0
segments, a read resolving `LIKES` under `e1`, the old exact-disjoint segment
auto-skipping, an unknown/mixed old segment read conservatively, and a hot mixed
region lazily re-layouted by compaction — without implying any rewrite at the
schema-change boundary. The bridge to Evaluation: schema evolution and
snapshot-correct deltas are treated as a *correctness boundary* on the pruning
surface, evaluated as a no-false-negative property, not as a physical-migration
performance claim.

### Evidence map

- `baseline/w13-schema-evolution-summary-20260614.md` — W13: the schema-evolution
  and lifecycle/reopen test suite (epoch advance, old-segment readability,
  mixed-delta survival across compaction + reopen, alias/drop, encoding-epoch
  decoding, exact-vs-mixed pruning under a new label).
- `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md` — Path B framing; C3 = reuse +
  re-narrate, add the three invariants + the no-false-negative theorem.
- Code anchors: `SchemaCatalog`, `SemanticSummaryCompleteness`,
  `entry_is_visible_at`, `resolve_edge_label`/`resolve_property` (`src/schema.rs`);
  segment `schema_epoch` / `property_encoding_epoch`, `semantic_state`,
  `signature_pruning_decision` with `schema_tombstone_fallback` (`src/csr/format.rs`).
- Cross-section anchors: §4 prune/keep reason taxonomy (the lever this section
  bounds); §5 (C2) compaction retention must preserve the no-false-negative
  theorem.
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
## 8. Related Work

SemL0 sits at the intersection of LSM storage maintenance, query-driven physical
design, graph storage, and schema evolution. We position it against each, and state
the one property that distinguishes it: **the control signal is a property-graph
query signature, and the maintained object is a segment-level semantic pruning
surface that is kept conservative-correct across the LSM lifecycle.**

### LSM / KV compaction and tuning
RocksDB-style engines and the LSM literature optimize compaction for write
amplification, space amplification, and read cost (e.g. the RUM trade-off), and a
large body of work auto-tunes LSM knobs — size ratios, compaction triggers,
bloom-filter bits — including the RocksDB / RUM / Dostoevsky line and learned,
workload-aware tuners. These operate on
*size and access-frequency* signals and on opaque key-value records. SemL0 is not a
general compaction tuner: its compaction decisions and segment layout are driven by
*graph query semantics* (label, edge type, direction, degree, property), and its
outputs carry exact/conservative semantic metadata that the read path can prove
disjointness against. Standard compaction tuning is orthogonal and composable with
SemL0.

### Query-driven physical design / adaptive indexing
Database cracking and adaptive indexing reorganize physical layout in response to
the query stream. SemL0 shares the "let queries shape the bytes" philosophy but
differs in physical object and safety boundary: the object is an LSM-CSR segment's
semantic summary (not an index over a column), and the safety contract is
*exact-proof pruning with conservative fallback* under tombstones, snapshots, and
schema epochs — pruning may only ever cause extra reads, never a missed edge.

### Graph storage and LSM-based graph stores
Topology-aware graph stores and LSM-based graph systems — notably **LSMGraph**
(VLDB'24; a multi-level CSR design, open source) and **BACH** (PVLDB'25; which
transforms layout between adjacency and CSR forms, no public code) — target update
locality and traversal efficiency at the *layout* level. SemL0 is
orthogonal: it exposes query semantics to the LSM read/maintenance path and can be
layered on top of such layouts. In particular, prior LSM-graph layout work does not
treat a property-graph query signature as first-class segment metadata, nor maintain
a prunable semantic surface across flush and compaction.

### External baseline: LiveGraph (measured, SF10)
Beyond positioning, we measure against **LiveGraph**, a transactional graph store,
as an external-system baseline at SF10 (LDBC SNB; 355,185,382 edges / 29,987,835
vertices): load 1,502.6 s, peak RSS ≈45.7 GiB, on-disk 37.58 GB block store + 1 GB
WAL; typed-neighbor scan over positive core edge types averages 2.74 µs (edge_type 1:
avg 3.38 µs, p99 15.2 µs). The full SemL0-vs-LiveGraph SF10 table is in
`baseline/seml0-baseline-defense-20260619/sf10-main-baseline-table.md`; consolidated
numbers in `tables/external-baseline-md.md`. Both systems serve the same typed
neighbor scans, but occupy different points: LiveGraph is an in-memory transactional
store (≈45.7 GiB RSS for SF10), whereas SemL0 is a disk-resident LSM design whose
contribution is *semantic pruning maintained across the lifecycle*; the comparison
establishes that SemL0's pruning operates against a real external system's footprint
and scan profile, not only internal variants. The external comparison is **SF10 only**
— LiveGraph SF100 load was infeasible (extrapolated 17–21 days), an honest scope limit
(§9). We additionally report an internal RocksDB-*style* KV-LSM layout (not official
RocksDB) and an archived KV-style encoding *simulation*; these are an internal baseline
and a simulation, labeled as such, not external systems.

### Schema evolution / versioned interpretation / MVCC
Versioned schema catalogs and snapshot/MVCC systems provide epoch-aware
interpretation and snapshot-consistent reads. SemL0 applies versioned
interpretation specifically to graph-LSM segment metadata and semantic pruning:
old segments keep their `schema_epoch`, additive changes route only future writes,
and pruning degrades to conservative reads whenever an epoch/encoding/tombstone
interaction makes exactness unprovable (§6). The contribution is the *interaction*
of schema versioning with semantic pruning safety, not a general migration engine.

> Do not expand related work into new claims. Each paragraph ends at "orthogonal /
> composable / different object+boundary."
>
> Named comparison points (bibkeys inserted at TeX assembly): LSMGraph (VLDB'24,
> multi-level CSR, open source); BACH (PVLDB'25, adjacency↔CSR layout transformation,
> no public code); the RocksDB / RUM / Dostoevsky and learned-LSM-tuning line; database
> cracking / adaptive indexing; LiveGraph (transactional graph store, our measured SF10
> baseline); versioned-catalog / MVCC systems. All are orthogonal or composable; SemL0's
> distinguishing object is the query-semantic pruning surface maintained across the lifecycle.
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
## 10. Conclusion

A property-graph query signature can act as a lifecycle control signal for an LSM-based dynamic graph store — shaping flush-time pruning surfaces, read pruning, feedback, and cross-level compaction retention — while pruning stays conservative-correct under schema evolution, tombstones, and snapshot-visible deltas. SemL0 cuts read amplification and candidate segments with zero correctness mismatch, retains or rebuilds the pruning surface across compaction at a bounded, explainable write cost, and degrades safely whenever exactness is unprovable; end-to-end latency is workload-dependent and reported as such. The result is not a new graph database but a maintainable, safety-bounded way to let query semantics govern the physical lifecycle of an LSM-based property graph.
## Appendix

### A. Claim → evidence map
- C1 read-amp/candidate reduction → `baseline/sf100-matrix-20260613-cn.md` (W6), `baseline/w8-property-2hop-summary-20260615-cn.md` (W8).
- C2 lifecycle retention → `baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`, `stage5-scale-summary-20260618-cn.md`, raw `stage4-smoke-sf1-20260618.json` / `stage5-scale-sf10class-20260618.json`; real SF30 `remote-logs/c2-sf30-real-20260619/` + `baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md`; tables `tables/table-c2-retention.md` + `tables/external-baseline-md.md`.
- C3 schema/snapshot correctness → `baseline/w13-schema-evolution-summary-20260614.md`; lifecycle/reopen engine tests.
- Gate decisions → `PLAN-SEML0-SIGMOD2027-CODEX-CN.md` §3, `baseline/path-b-c2/`.

### B. Reproduction pointers
- Correctness gate: `cargo test --lib` (69), `cargo test --test engine_tests` (61), `cargo test --bin lsmgraph` (4).
- C2 runner: `cargo run --bin c2-merge-retention -- --sources <N> --edge-types <list> --output <json>`.
- Code anchors: `SemanticL0Index` + `LevelMergePolicy` + `compact_level_to_next` + `split_semantic_compaction_segments` (src/graph.rs); `CsrSegmentMeta` + `signature_pruning_decision` + `SegmentSemanticState` + `PruningSurfaceSummary` (src/csr/format.rs); `SchemaCatalog` (src/schema.rs).

### C. Reviewer question bank
See `reviewer-question-bank.md`.

### D. Open submission steps (Stage 10)
Target venue + template, TeX/PDF compilation (no LaTeX engine in the current environment), page-budget fit, PDF visual inspection, bib citations for related work.

### E. Figures (planned; data in `tables/plot-data-md.md`)
Three figures, drawn at TeX assembly. Each only visualizes data already in §3–§7 and adds no new claim.

- **Figure 1 — Pipeline / lifecycle diagram** (`figures/seml0-pipeline.pdf`): the four cooperating loops of §3 (write/flush → read/prune → feedback/compaction → schema/snapshot safety) over the shared semantic metadata, showing where the pruning surface is created, exploited, maintained, and resolved.
- **Figure 2 — Schema-snapshot timeline** (`figures/schema-snapshot-timeline.pdf`): the §6 timeline — old segment written at epoch `e0`, catalog adds an edge label at `e1`, future writes enter new L0 segments, a read resolves the new label under `e1`, old exact-disjoint segments auto-skip, and unknown/mixed old segments are read conservatively — with no rewrite at the schema-change boundary.
- **Figure 3 — C2 retention before/after bar** (`figures/c2-retention.pdf`): the §5 microbenchmark — `exact_surface_ratio` and typed-neighbor read bytes before vs after an L1→L2 merge under naive vs semantic, with the bounded `write_amp` cost (≈1.85 vs ≈1.14) shown alongside.
