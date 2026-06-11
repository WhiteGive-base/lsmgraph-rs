# SemL0 Background And Problem Section Draft MD

Date: 2026-06-05

## Status

```text
background_problem_section_draft_ready=yes
background_problem_prose_polished=yes
md_only=yes
paper_section_draft=yes
tex_generated=no
experiments_run=0
benchmarks_run=0
stores_deleted=0
final_submission_ready=no
```

## Purpose

This MD document is the prose-polished paper-section draft for Background and
Problem. It defines the mismatch that SemL0 addresses: LSM graph storage is
write-friendly, but the recent-update region often lacks exact storage-visible
summaries for the graph predicates that decide whether a segment can contribute
to a result.

This stage does not edit TeX, generate PDF, run experiments, rerun benchmarks,
delete stores, choose a venue, or mark final submission ready.

## Source Anchors

| Anchor | Role |
|---|---|
| `paper/introduction-section-draft-md.md` | thesis and contribution boundary |
| `paper/system-overview-section-draft-md.md` | pipeline that follows this problem statement |
| `paper/final-paper-outline-md.md` | section placement and paragraph plan |
| `paper/paper-narrative-spine.md` | global problem-to-design narrative |
| `paper/schema-change-decision-matrix.md` | exact-proof pruning and schema-change boundary |
| `paper/mixed-schema-delta-stress-inventory.md` | snapshot, tombstone, and schema/delta correctness boundary |
| `src/semantic.rs` | `GraphAccessSignature`, `DegreeClass`, property predicates |
| `src/csr/format.rs` | `CsrSegmentMeta`, `schema_epoch`, property bitmap, pruning checks |
| `tests/engine_tests.rs` | source-level correctness anchors for pruning, schema, and snapshots |

## Intended Paper Placement

This draft corresponds to:

```text
Section 2. Background And Problem
```

It should appear after the Introduction and before the System Overview. It
answers:

```text
Why does an LSM graph store need query-semantic physical metadata?
```

## Draft Section Text

### 2. Background And Problem

Dynamic property graphs combine high update rates with semantically selective
reads. A single neighborhood query may depend on the source vertex label, edge
type, direction, destination label, source degree class, property presence or
value, snapshot timestamp, and schema epoch. These predicates are not just
logical query-planner details. At the storage boundary, they decide whether a
recent segment can contain a visible answer.

LSM storage is attractive for dynamic graphs because it absorbs updates as
append-heavy deltas and reorganizes them later through compaction. A graph
engine can flush recent edge and property updates into CSR-like L0 segments
while older data is gradually consolidated. This keeps the write path cheap and
avoids rewriting large graph regions for every insert, delete, or property
update.

The same design creates a read-side problem. L0 segments overlap. A query over
one source vertex, edge type, degree class, or property may have to inspect many
recent segments before discovering that most of them are irrelevant. Traditional
LSM metadata usually reasons about keys, timestamps, and compaction levels. For
property-graph access, those dimensions are insufficient: a segment can be
nearby in key space but irrelevant because it contains a different edge label,
an incompatible degree class, no required property, a tombstone that must be
merged carefully, or data written under an older schema interpretation.

We call this mismatch query-semantics blindness:

```text
the storage layer maintains update-local LSM segments, but it cannot directly
use graph query semantics to prove which segments are irrelevant.
```

The problem is therefore not only that L0 contains many segments. The deeper
issue is that graph usefulness is multi-dimensional and dynamic. Edge type,
source label, direction, degree class, property predicates, snapshot visibility,
and schema epoch can all affect the same read. If those dimensions remain
invisible to physical layout and segment metadata, semantic irrelevance becomes
read amplification.

### Storage-Facing Query Semantics

SemL0 represents the storage-facing part of a graph access as a
`GraphAccessSignature`. A signature is not a full logical query plan. It is the
subset of query semantics that the storage layer can use for candidate
selection:

```text
source vertex
source label
edge type
direction
degree class
destination label
snapshot boundary
property predicate
schema interpretation
```

For example, a typed neighborhood lookup over a low-degree `Person` source with
a required property is a different storage access from an untyped scan over a
mixed-degree source. If both accesses use the same L0 probing path, the engine
cannot avoid segments that are semantically disjoint from the query.

The corresponding segment-side object is `CsrSegmentMeta`. It records
storage-visible summaries such as source label, edge type partition, direction,
degree class, whether the degree class is exact, `schema_epoch`,
`property_presence_bitmap`, property summary completeness, and whether the
segment may contain tombstones. These fields are the vocabulary used by the
later C1 physical design and by the schema/snapshot safety rules.

### Why Pruning Is A Correctness Contract

Semantic metadata is useful only if it cannot produce false negatives. A
segment may be skipped only when its metadata proves that no visible record in
the segment can satisfy the query. If the metadata is unknown, mixed, legacy,
incomplete, tombstone-sensitive, or too weak for the predicate, the engine must
keep the segment in the candidate set.

This gives SemL0 its shared pruning contract:

```text
Only prune when metadata proves absence or disjointness.
Otherwise read conservatively.
```

Exact-disjoint summaries may remove a segment. Exact-absent property summaries
may remove a segment for a required-property predicate. But a tombstone-bearing
segment cannot be discarded merely because a positive property summary appears
absent: the tombstone may still be needed to hide an older visible version.
Likewise, mixed or unknown degree metadata cannot prune a segment that may
contain the requested source under the query snapshot.

The cost of uncertainty is extra probing. The cost must not be an incorrect
answer.

### Schema Evolution In The Problem Model

Query-semantics blindness becomes more subtle when the schema changes. If
physical layout uses labels, edge types, and properties, it is natural to ask
whether adding a new edge label or property makes old stores useless. In
SemL0's problem model, it does not.

The reason is that logical schema interpretation and physical segment bytes are
separated. The catalog advances a `schema_epoch` when the logical schema
changes. Future writes use the current catalog state, while old segments keep
the epoch and metadata completeness state under which they were written. A
query compiled under the current catalog can still read old segments by
applying the exact-proof rule through catalog history.

For an additive edge-label, vertex-label, or property change, exact old
metadata may prove that an old segment is disjoint from the newly introduced
semantic region and can be skipped. If the old metadata is unknown, mixed, or
tombstone-sensitive, the segment is read conservatively. Lazy compaction can
later rebuild hot metadata under the new interpretation, but it is a
performance repair path rather than a correctness prerequisite.

```text
Schema changes do not automatically invalidate old storage.
```

This boundary is important for the rest of the paper. SemL0 does not claim a
complete physical migration engine for every rename, drop, type change, or
encoding change. It claims that additive schema evolution and mixed old/new
metadata can preserve correctness through catalog epochs, exact-proof pruning,
and conservative fallback.

### Snapshot And Delta Visibility

Dynamic graph reads are snapshot reads. Inserts, deletes, property changes, and
compaction interact with query semantics because a segment may contain records
that are invisible at the current snapshot but visible at another one.
Tombstones are especially important: a tombstone segment may be required to
remove an older edge from the visible result even if the tombstone row itself
does not satisfy a positive property predicate.

The storage problem has two coupled requirements:

1. reduce L0 candidates when semantic metadata gives an exact proof;
2. preserve visible-history correctness when such a proof is unavailable.

This is why the later design treats C1 query-semantic layout, P3 feedback
compaction, P2 schema epochs, and P4 snapshot/tombstone handling as one system
rather than independent optimizations.

### Problem Statement

The problem addressed by SemL0 is:

```text
How can an LSM graph store make property-graph query semantics visible to L0
layout, candidate selection, and compaction, while preserving exact no-false-
negative behavior under schema changes, tombstones, and snapshot-visible deltas?
```

This problem statement implies three requirements:

1. Storage-facing query semantics must be represented explicitly, not inferred
   accidentally from key ranges alone.
2. Segment metadata must distinguish exact, mixed, unknown, legacy, incomplete,
   and tombstone-sensitive summaries.
3. Maintenance must improve future layout without changing query semantics or
   invalidating old storage.

SemL0's technical sections answer these requirements in order. The System
Overview connects the write, read, maintenance, and schema/snapshot safety
paths. The C1 section defines the query-semantic pruning surface. The P3 section
shows how feedback can maintain that surface under workload shift. The
schema/snapshot section explains why additive schema evolution and dynamic
deltas do not require global rebuilds or unsafe pruning.

## Prose-Polish Notes

This stage tightens the section around the current system chain:

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

The section now answers the schema-change concern before the System Overview:

```text
Schema changes do not automatically invalidate old storage.
```

## Paper-Safe Wording

Safe wording:

```text
SemL0 targets query-semantics blindness in LSM graph storage: the L0 region is
update-friendly but lacks exact storage-visible summaries for the graph
predicates that determine whether a segment can contribute to a result. SemL0
therefore treats semantic pruning as an exact-proof optimization with
conservative fallback.
```

Unsafe wording:

```text
SemL0 eliminates all L0 read amplification.
SemL0 can prune any segment whose summary looks unlikely to match.
Schema changes require no extra reads.
Tombstones can be ignored for positive property predicates.
Feedback compaction changes query semantics.
```

## Evidence Hooks For This Section

| Paper hook | Evidence anchor |
|---|---|
| graph access signatures | `src/semantic.rs`; `paper/query-semantic-physical-design-section-draft-md.md` |
| segment summaries | `src/csr/format.rs`; `paper/system-overview-section-draft-md.md` |
| exact-proof pruning contract | `paper/schema-change-decision-matrix.md`; `tests/engine_tests.rs` |
| schema no-rebuild concern | `paper/schema-evolution-section-draft-md.md`; `paper/schema-change-decision-matrix.md` |
| snapshot/tombstone risk | `paper/mixed-schema-delta-stress-inventory.md`; `tests/engine_tests.rs` |
| evaluation mapping | `paper/evaluation-section-draft-md.md` |

## Bridge To Next Section

This section defines the problem. The next section should not re-argue the
motivation. It should show how SemL0 organizes the solution around four paths:

```text
write path
read path
maintenance path
schema/snapshot safety path
```
