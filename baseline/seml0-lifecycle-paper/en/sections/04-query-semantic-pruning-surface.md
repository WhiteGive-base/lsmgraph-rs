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
