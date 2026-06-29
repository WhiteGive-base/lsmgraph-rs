# SemL0: Query Signatures as a Storage Control Plane for Dynamic Property Graphs

CIDR draft v0.1, 2026-06-24.

## Abstract

Dynamic property-graph stores expose rich query signatures: vertex labels, edge types, directions, degree classes, property predicates, snapshots, and schema epochs. Yet in most LSM-style graph stores, these semantics stop at the query layer. The storage layer still organizes reads and physical rewrites around keys, levels, adjacency blocks, and size-based compaction. This separation makes selective graph reads unnecessarily expensive: a query may know exactly which typed neighborhood it needs, while the storage layer still scans or probes segments whose contents are semantically irrelevant.

We argue that query signatures should become a storage control plane for mutable graph stores. SemL0 is a prototype LSM-based property-graph store that compiles query signatures into segment-level semantic state, uses exact evidence for safe pruning, preserves or rebuilds semantic pruning surfaces during compaction, and falls back conservatively under schema, snapshot, and tombstone uncertainty. The goal is not to build a full graph database, but to study a storage architecture principle: mutable graph storage should be controlled not only by key order and level structure, but also by the semantics of the graph queries it is expected to serve.

Experiments on a remote prototype deployment show that SemL0 reduces candidate segments and read bytes on LDBC SNB workloads up to SF100, preserves semantic pruning surfaces on real SF30 compaction inputs, and maintains no-false-negative behavior under schema and snapshot evolution. We also report the limits of direct comparison with existing dynamic graph systems: many systems optimize transactions, structural analytics, multi-version CSR, or distributed graph services, but do not expose the same property-graph typed-neighbor control surface.

## 1. Introduction

Property graphs are queried through semantics. A typical graph query does not merely ask for the next key range or the next adjacency block; it asks for typed neighborhoods such as outgoing `KNOWS` edges from `Person` vertices, incoming membership edges to `Forum` vertices, recent messages satisfying a property predicate, or snapshot-consistent paths across a changing graph. These queries carry rich signatures: source labels, edge types, directions, degree distributions, property access patterns, temporal visibility, and schema epochs.

Storage systems, however, often do not see these signatures as first-class physical design inputs. In LSM-style graph stores, updates are absorbed into mutable or append-friendly components and later reorganized through flush and compaction. This design is attractive for dynamic graphs, but the storage layer is usually organized around key order, level structure, adjacency representation, and size-based rewrite policies. As a result, query semantics are interpreted above storage, while storage itself remains largely query-blind.

This mismatch creates a specific form of read amplification. A selective property-graph query may know that only a small subset of typed neighborhoods can contribute to the result, but the storage layer may still need to inspect many candidate segments because their metadata cannot prove semantic irrelevance. This is especially visible in LSM-based layouts: as updates accumulate in L0 and are later compacted into lower levels, semantically precise segments can become mixed physical outputs. Once a segment loses its precise semantic identity, future queries must conservatively read it even when most of its contents are irrelevant.

This paper argues for a different architecture. Query signatures should not stop at the query layer; they should become a storage-level control plane. In this architecture, property-graph query semantics are compiled into storage-visible metadata and rewrite decisions. Segment metadata records what kinds of graph neighborhoods a segment can contain. The read path uses this metadata to prune only when absence or disjointness is exact. The compaction path preserves or reconstructs high-value semantic partitions instead of treating all physical rewrites as semantically neutral. The correctness path falls back conservatively under schema changes, tombstones, and snapshot uncertainty.

We explore this idea in SemL0, a prototype LSM-based dynamic property-graph store. SemL0 stores graph data in LSM-style CSR segments enriched with semantic state. A query signature describes the graph access pattern, including labels, edge types, direction, degree class, properties, and visibility constraints. At flush time, SemL0 materializes segment-level semantic summaries. At query time, it uses an exactness-aware pruning contract: a segment can be skipped only when its semantic state proves that it cannot contribute to the query. At compaction time, SemL0 treats physical rewrite as an opportunity to preserve the semantic pruning surface. Under uncertainty, SemL0 prefers correctness over pruning.

The central lesson is that compaction is not semantically neutral. Conventional LSM compaction is usually discussed in terms of level size, key overlap, space amplification, and write amplification. In a property-graph store, compaction also changes the semantic shape of the read path. A naive merge can combine previously exact semantic partitions into mixed outputs, destroying the evidence needed for safe pruning. Conversely, a semantic-aware merge can preserve or rebuild exact partitions, trading a modest amount of rewrite work for lower future read amplification.

SemL0 is not intended to be a complete graph database or a universal replacement for existing dynamic graph systems. Instead, it is a prototype used to evaluate a systems principle: query semantics can act as a storage-level control signal in mutable graph stores. This framing also affects evaluation. Rather than claiming a complete state-of-the-art comparison against systems with different abstractions, we focus on whether the control plane reduces read amplification, whether full semantic materialization is too expensive, whether compaction can destroy semantic surfaces, and whether conservative fallback prevents false negatives under schema and snapshot changes.

This paper makes four contributions.

First, we propose a storage-level query-semantic control plane for dynamic property graphs. SemL0 exposes graph query signatures to the storage layer and uses them to shape segment metadata, pruning, and physical rewrite decisions.

Second, we define an exactness-aware pruning contract for mutable graph storage. SemL0 distinguishes exact, conservative, and unknown semantic states. It only prunes with exact evidence and falls back to reading when schema, snapshot, tombstone, or encoding uncertainty could otherwise cause false negatives.

Third, we identify semantic surface retention as a physical rewrite problem. We show that naive compaction can destroy query-relevant semantic evidence, while semantic-aware compaction can preserve or reconstruct the pruning surface.

Fourth, we provide prototype evidence and comparison boundaries. On LDBC SNB workloads, SemL0 reduces candidate segments and read bytes, exposes the memory cliff of full semantic materialization, preserves semantic surfaces on real compaction inputs, and maintains correctness under schema and snapshot evolution. We also document why several existing dynamic graph systems are valuable design baselines but do not directly provide the same property-graph typed-neighbor workload and storage-control interface.

## 2. Why Query Signatures Belong in Storage

The query signature of a property-graph access captures more than a key range. For a typed-neighbor scan, the signature may include a source label, an edge type, a direction, a degree class, an optional property predicate, a snapshot timestamp, and the schema epoch under which labels and properties are interpreted. This signature is a compact representation of what evidence the query needs from storage.

Existing LSM-style graph storage usually treats this information as a query-layer concern. The storage layer can find keys, segments, adjacency blocks, or CSR slices, but it rarely knows whether a segment is irrelevant because it contains the wrong source label, the wrong edge type, or data from a schema epoch that requires conservative interpretation. This is a missed control opportunity. A segment that is exact for `(source label, edge type)` is not merely a physical object; it is a proof object for future pruning.

SemL0 treats query signatures as physical control inputs in three places.

At flush time, SemL0 records segment-level semantic state. A segment can be exact for a specific signature component, conservative because it mixes several components, or unknown because schema or visibility information is insufficient.

At read time, SemL0 prunes only with exact evidence. If a segment's exact semantic state is disjoint from the query signature, the segment can be skipped. If the segment is conservative or unknown, the read path scans it. This rule is intentionally strict: the optimization may lose opportunities, but it must not lose answers.

At compaction time, SemL0 uses query signatures to decide whether physical rewrite should preserve or rebuild semantic partitions. A compaction policy that merges everything into one lower-level output may reduce file count, but it can also erase the evidence that selective graph reads depend on.

## 3. SemL0 Design Overview

SemL0 is a prototype LSM-based property-graph store. It uses LSM-style ingestion and CSR-like segment bodies, but augments each segment with semantic metadata derived from property-graph query signatures. The design is organized around a conservative contract: semantic metadata is allowed to improve pruning, but correctness must never depend on it being present or precise.

**Query signatures.** A query signature represents the storage-visible part of a graph access. The current prototype focuses on typed-neighbor and property-aware graph reads over LDBC SNB-style data. A signature can express source label, edge type, direction, degree class, property predicate class, snapshot, and schema epoch. SemL0 does not require every query to have a fully exact signature. When some component is unknown, the system keeps the corresponding part conservative.

**Segment semantic state.** Each segment stores compact semantic state that describes what it can contain. For example, a segment may be exact for `(Person, KNOWS)`, exact for an edge type only, conservative over several labels, or unknown after schema uncertainty. Exactness is the key property. Exact metadata can prove absence for disjoint signatures; conservative metadata cannot.

**Read pruning.** The read path evaluates a query signature against segment metadata. It skips a segment only when the segment's metadata proves non-overlap with the requested signature. This gives SemL0 a no-false-negative contract. The system can over-read when metadata is coarse, but it does not skip a segment based on a guess.

**Lifecycle control.** SemL0's main distinction is that query signatures are not used only for L0 filtering. The same control signal is carried across the physical lifecycle: flush creates the initial pruning surface, reads consume it, feedback identifies useful partitions, compaction preserves or reconstructs it, and schema/snapshot logic decides when exactness must be downgraded.

## 4. Compaction is Not Semantically Neutral

LSM compaction is often treated as a physical maintenance operation. It merges runs, removes obsolete entries, reduces overlap, and enforces level-size constraints. Under this view, a compaction policy is judged mainly by write amplification, space amplification, and its effect on future key-range lookups. For dynamic property graphs, this view is incomplete. Compaction does not only rewrite bytes; it rewrites the semantic surface that future graph queries rely on.

Consider a set of L0 graph segments produced by query-semantic flush. Some segments may be exact with respect to `(source label, edge type)`: one segment may contain only outgoing `KNOWS` edges from `Person` vertices, another only `HAS_MEMBER` edges involving `Forum` vertices, and another only message-related edges. A typed-neighbor query can safely skip a segment if the segment's exact semantic state is disjoint from the query signature. The pruning decision is safe because it is based on absence, not speculation.

A naive compaction can destroy this property. If the compactor merges several exact semantic partitions into a single mixed output segment, the bytes are still correct and the graph contents are still present. However, the output segment may no longer provide exact evidence for any one query signature. Its semantic state becomes conservative or unknown. Future queries must read it because the system can no longer prove that the segment is irrelevant. In other words, compaction can preserve logical graph contents while destroying the physical evidence needed for efficient reads.

This observation changes the role of compaction in a mutable graph store. Compaction should not be controlled only by level size and key overlap. It should also consider whether a rewrite preserves, weakens, or reconstructs the semantic pruning surface. SemL0 therefore treats compaction as semantic rewrite. When a partition is valuable for the workload and can be kept exact at acceptable cost, SemL0 emits semantic-aware outputs, such as partitions grouped by `(source label, edge type)`. When exact preservation is too expensive or unsafe, SemL0 marks the output conservatively and lets the read path fall back to scanning.

This design deliberately separates correctness from optimization. Correctness does not depend on successful semantic partitioning. If semantic metadata is exact, the read path can prune. If semantic metadata is conservative or unknown, the read path reads. This means that semantic-aware compaction is an optimization over a safe baseline, not a risky shortcut. The worst case is reduced pruning, not a false negative.

Our C2 experiment isolates this effect. On controlled inputs, we compare naive physical merge with semantic-aware merge under the same logical graph contents and query workload. On real SF30 compaction inputs, naive merge collapses exact semantic partitions into mixed outputs, while semantic-aware merge preserves the pruning surface. The result is a simple but important lesson: in LSM-based property-graph stores, physical rewrite policy determines not only write cost, but also the future availability of semantic evidence for safe pruning.

This lesson is the main reason SemL0 is not just a query-time filter. If semantic metadata is produced only at flush time and then ignored by compaction, the optimization decays as the LSM tree evolves. A storage-level query-semantic control plane must therefore cover the full physical lifecycle: flush, read, compaction, schema evolution, tombstones, and snapshots. The control plane is useful precisely because it keeps query semantics visible across physical rewrites.

## 5. Correctness Under Uncertainty

Semantic pruning is useful only if it is safe. A storage engine that skips a segment because it is "probably irrelevant" can silently lose edges. SemL0 therefore treats exactness as part of the storage contract.

The contract is simple. Exact metadata can prove disjointness. Conservative metadata cannot. Unknown metadata cannot. A segment may be skipped only in the first case. This makes schema evolution, snapshots, tombstones, and encoding epochs correctness boundaries rather than ad hoc exceptions.

For schema evolution, old segments may have been written under a different interpretation of labels or properties. If the old interpretation remains readable and can be mapped exactly, SemL0 can continue to prune. If aliasing, dropping, or encoding changes make the mapping uncertain, the segment is read conservatively.

For snapshots and tombstones, visibility is also part of the query signature. A segment that contains tombstones or mixed-version deltas may still be exact for a label and edge type but conservative for snapshot visibility. In that case, SemL0 can use the exact parts of the signature only where they remain sound, and it reads the segment when visibility uncertainty could hide an answer.

The important design point is that conservative fallback is not a failure mode. It is the mechanism that lets query signatures control storage without turning semantic metadata into a correctness risk.

## 6. Prototype Deployment and Experiments

SemL0 is evaluated as a remote prototype deployment, not as a production graph database. The implementation and experiment artifacts are hosted under `/data/WorkSpace/lsmgraph-rs` on a remote server. The paper uses this deployment to answer systems questions: does the control plane reduce read amplification, when is semantic materialization too expensive, does compaction preserve semantic evidence, and where do direct external comparisons become non-comparable?

### W6: SF100 read-amplification and budget tradeoff

On LDBC SNB SF100, the semantics-blind `naive` and `kv-lsm` baselines inspect 49,257,601 L0 candidates and read 3,461.6 MiB. Semantic variants reduce L0 candidates to about 5.9M to 7.9M and reduce read bytes to about 642.7 to 820.0 MiB, depending on the budget and metadata granularity. All checked rows report zero correctness mismatches.

The same experiment shows why the control plane must be budgeted. Full unbudgeted semantic materialization reaches 118.03 GiB peak import RSS, while budgeted/schema variants stay around the naive memory envelope. This is an important negative result: query semantics are useful as a storage control signal, but materializing every semantic distinction without a budget can create a memory cliff.

### W9: SF30 dynamic mixed read/write evidence

On a 30-minute SF30 mixed read/write run with six checkpoints, the system sustains about 163 queries/s with zero writer errors. Tail latency is workload-dependent and should not be reported as a universal speedup, but the dynamic run shows a strong lifecycle signal. At 1800s, schema p99 reaches 8,740.2 us, while semantic p99 is 1,274.0 us. This supports the claim that preserving query-semantic state over time can protect the read path under a changing LSM layout.

### C2: compaction surface retention

C2 is the core evidence for the paper's thesis. Controlled experiments show that naive merge destroys exact semantic surfaces and causes a 4x to 6x typed-neighbor read blow-up, while semantic-aware merge keeps the read cost flat with zero mismatches.

The real SF30 compaction input is the stronger systems result. It contains 1.09B directed edges, 40 `(source label, edge type)` partitions, and 528 exact L1 segments. Naive compaction has semantic retention 0.0; semantic-aware compaction has retention 1.0. The write cost is bounded: write amplification is 1.24 for semantic-aware merge versus 1.07 for naive merge. A metadata replay over the real SF30 post-merge segment metadata shows a 6.52x weighted candidate-byte proxy for naive merge versus 1.00x for semantic-aware merge.

This is not claimed as a full end-to-end SF30 body-read workload. The controlled rows measure a full read workload and correctness; the real SF30 rows measure retention, write cost, and metadata-level candidate replay. That boundary is intentional.

### W13: schema and snapshot correctness

W13 exercises the conservative fallback path. The test set covers old segment readability, mixed deltas across compaction and reopen, alias/drop behavior, encoding epoch handling, and new-label exact-vs-mixed pruning. These tests support the correctness side of the design: SemL0 can use exact metadata where it remains valid and can fall back when schema or snapshot uncertainty would make pruning unsafe.

### External baselines: measured systems with a digest gate

We also ran external baselines under the same typed-neighbor gate. A system enters the numeric table only if it loads the same LDBC dense edge set, runs a fixed-seed sampled typed-neighbor workload, and passes a per-query count/hash digest check against the dense edge-list truth. The current measured rows are LiveGraph, Aster RocksGraph, Neo4j Community, TuGraph, and NebulaGraph. LSMGraph-style remains an internal layout row rather than an official external artifact.

LiveGraph is the scope-limited dynamic graph storage baseline. SF1 completed with 34,692,699 edges, 3,181,724 vertices, 21.155 s load time, 4,850,512 KB peak RSS, and an edge-count gate showing `scan_edges = dense_edges = LiveGraph edge_count`. SF10 completed with 355,185,382 edges, 29,987,835 vertices, 1,457.36 s load time, 47,908,544 KB peak RSS, and 38,654,705,664 bytes footprint. The digest verifier checks 16,140 SF1 sampled queries and 32,140 SF10 sampled queries with zero mismatches. LiveGraph is useful as a typed-neighbor scan baseline, not as a complete graph-database head-to-head.

Neo4j Community is the general property-graph database baseline. We import each SemL0 dense edge type as a distinct outgoing Neo4j relationship type, such as `E_Px` and `E_Nx`, to avoid conflating negative edge types with ordinary incoming traversal. SF1 checks 1,700 sampled queries with zero mismatches and reports all-types avg/p50/p90/p99 of 180,922.681/2,899.281/43,831.280/3,458,485.918 us. SF10 also checks 1,700 sampled queries with zero mismatches and reports all-types avg/p50/p90/p99 of 1,974,806.086/3,442.775/70,662.558/34,522,340.128 us. The SF10 high tail is driven by negative/high-fanout edge types, which return 84,017,646 neighbors in the sampled workload.

TuGraph is the embedded graph database baseline. We use the TuGraph runtime image with an embedded C++ typed-neighbor driver over the same dense edge set. SF1 loads 3,181,724 vertices and 34,692,699 edges, checks 1,700 queries with zero mismatches, and reports avg/p50/p90/p99 of 420.925/6.358/151.398/17,602.600 us. SF10 loads 29,987,835 vertices and 355,185,382 edges, checks 1,700 queries with zero mismatches, and reports avg/p50/p90/p99 of 3,775.450/12.630/633.490/158,870 us. This is an embedded API typed-neighbor baseline, not a full TuGraph LDBC Interactive benchmark.

Aster RocksGraph is the LSM-adjacent bridge baseline. We use the lightweight `NTU-Siqiang-Group/Aster` implementation at commit `6abb258e577c479325092a8ac0e7691fdfd154c2`. The driver maps each `(edge_type, src)` pair to a compact logical vertex id, which avoids sparse-id expansion in Aster's MorrisCounter path. SF1 checks 1,700 queries with zero mismatches and reports avg/p50/p90/p99 of 150.985/10.680/381.904/3,411.36 us. SF10 checks 1,700 queries with zero mismatches and reports avg/p50/p90/p99 of 1,408.270/580.529/1,061.240/15,809.60 us. This row should be described as an Aster/RocksGraph typed-neighbor bridge, not as a full AsterDB Gremlin benchmark.

NebulaGraph is the distributed open-source graph database baseline. We use NebulaGraph v3.8.0 server images and model each dense edge type as a separate nGQL edge type, such as `E_Px` and `E_Nx`. SF1 checks 1,700 queries with zero mismatches and reports avg/p50/p90/p99 of 61,553.966/581.010/10,736.324/1,189,334.146 us. SF10 checks 1,700 queries with zero mismatches and reports avg/p50/p90/p99 of 744,146.618/741.524/20,218.339/14,228,169.276 us. This is a same-workload typed-neighbor baseline, not a production deployment or a full LDBC Interactive benchmark.

## 7. Comparison with Existing Systems

SemL0 is closest in spirit to systems that rethink dynamic graph storage layout, including LSM-style graph stores, multi-version CSR designs, transactional graph stores, and dynamic graph analytics containers. These systems are valuable design baselines, but they optimize different interfaces.

LiveGraph is a transactional graph store with efficient adjacency scans. It is the closest measured external system for typed-neighbor scans, but it is an in-memory transactional graph store rather than an LSM-based property-graph storage prototype with a semantic compaction control plane. Aster RocksGraph gives an LSM-adjacent measured row through a typed-neighbor bridge. Neo4j, TuGraph, and NebulaGraph broaden the comparison to mainstream property-graph database implementations, while still remaining scoped to the typed-neighbor workload and digest gate.

LSMGraph and BACH remain useful qualitative comparisons for multi-level CSR, LSM graph layout, and adjacency/CSR transformation. SemL0's distinct claim is not another layout alone, but the use of query signatures as a storage control plane that shapes pruning and physical rewrites.

Teseo, GraphOne, LLAMA, and Aspen are important dynamic graph systems, but their public artifacts and workloads are not direct matches for LDBC property-graph typed-neighbor reads with schema/snapshot semantics. LLAMA is primarily analytics-oriented; Teseo and GraphOne focus on dynamic graph containers and structural analytics; Aspen focuses on low-latency graph streaming. They should appear in the design matrix and related work, not as forced apples-to-apples latency baselines.

Industrial graph systems such as ByteGraph, BG3, Galaxybase, GES, and Nebula Graph are useful writing models for motivation, deployment framing, and lessons learned. The NebulaGraph row in our table is a reproducible same-workload baseline, not evidence of production deployment or direct production-scale comparability with these systems.

## 8. Experience and Lessons Learned

**Lesson 1: query semantics are useful only with an exactness discipline.** The main risk in semantic pruning is not missing an optimization; it is skipping data incorrectly. SemL0's exact/conservative/unknown state model made the implementation easier to reason about because every pruning decision is a proof obligation.

**Lesson 2: semantic materialization needs a budget.** SF100 shows that semantics can reduce candidates and read bytes substantially, but full semantic materialization can create an import-memory cliff. A practical system should treat query signatures as a budgeted control plane rather than a request to materialize every possible semantic partition.

**Lesson 3: compaction can erase the evidence created by flush.** The C2 results changed the center of the story. If compaction ignores semantics, the benefits of query-semantic flush decay as the LSM tree evolves. The system must carry semantic control through the lifecycle.

**Lesson 4: conservative fallback is a feature, not an apology.** Schema evolution, tombstones, and snapshots are not rare corner cases in mutable graph storage. SemL0's fallback rule limits the blast radius of uncertain metadata: the system reads more, but it does not lose answers.

**Lesson 5: external graph systems are difficult to compare without changing the question.** Many existing systems are strong baselines for transactions, analytics, CSR snapshots, or distributed graph services. Fewer expose the exact property-graph typed-neighbor interface and semantic rewrite control that SemL0 studies. CIDR is a better venue than a pure benchmark paper because the contribution is a storage architecture principle and a set of systems-building lessons.

## 9. Limitations and Next Steps

SemL0 is a prototype, not a production graph database. The deployment evidence is a remote experimental deployment. It should not be described as production deployment, a full graph query engine, or a complete replacement for existing dynamic graph stores.

Latency improvements are workload-dependent. The paper's strongest claims are read-amplification reduction, semantic surface retention, and conservative correctness. Latency should be presented as supporting evidence, especially in the dynamic SF30 run.

The C2 real SF30 evidence is a three-layer story: controlled full-read workload and correctness, real SF30 retention/write cost, and real SF30 metadata replay. It is not a full SF30 body-read execution trace.

LiveGraph, Aster RocksGraph, Neo4j Community, TuGraph, and NebulaGraph now have SF1/SF10 measured evidence with digest correctness. The verifier samples `(edge_type, src)` pairs with a fixed seed, computes neighbor counts and hashes against dense edge-list truth, and reports zero mismatches for the rows in the numeric table. A separate clean-checkout reproduction remains optional if we want an even stricter artifact story for Aster and NebulaGraph.

## 10. Conclusion

SemL0 argues that query signatures should become a storage control plane for dynamic property graphs. The core idea is not to add another query-time filter, but to make graph semantics visible across flush, read, compaction, schema evolution, tombstones, and snapshots. The prototype shows that this control plane can reduce read amplification, expose the need for budgeted semantic materialization, preserve semantic pruning surfaces through compaction, and remain conservative under correctness uncertainty. The broader lesson is that in mutable graph storage, physical lifecycle policy determines not only where bytes are placed, but also whether future queries retain the evidence needed to avoid reading them.

## TODOs Before Submission

- Convert this Markdown draft into the target CIDR LaTeX template if the submission path requires LaTeX.
- Add citations and tighten related work once the final bibliography is chosen.
- Add a figure for the storage control plane lifecycle: query signature -> flush metadata -> read pruning -> semantic compaction -> schema/snapshot fallback.
- Add a figure for C2: exact-surface retention and write amplification under naive vs semantic merge.
- Keep external comparisons scope-limited: typed-neighbor workload, digest PASS, not a complete graph-database head-to-head.
- Place LiveGraph, Aster RocksGraph, Neo4j, TuGraph, and NebulaGraph measured rows in the evaluation table or in an external-baseline sidebar, with scope notes for Aster and NebulaGraph.
