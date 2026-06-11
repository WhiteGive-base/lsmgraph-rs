# SemL0 Query-Semantic Physical Design Section Draft MD

Date: 2026-06-05

## Status

```text
query_semantic_physical_design_section_draft_ready=yes
md_only=yes
paper_section_draft=yes
tex_generated=no
experiments_run=0
benchmarks_run=0
stores_deleted=0
cleanup_approved=no
safe_to_run_now=no
safe_to_delete_now=no
final_submission_ready=no
```

## Purpose

This MD document is the paper-section draft for C1: query-semantic LSM physical
design. It turns the current C1 source behavior, SF1 tables, threshold ablation,
and command-bundle audit into prose that can later be translated into the paper.

This stage does not edit TeX, generate PDF, run experiments, rerun benchmarks,
delete stores, approve cleanup, rerun C1 scripts, choose a venue, or mark final
submission ready.

## Source Anchors

| Anchor | Role |
|---|---|
| `paper/introduction-section-draft-md.md` | thesis, contribution boundary, and exact-proof contract |
| `paper/intro-conclusion-prose-polish-md.md` | current opening/closing chain that C1 must match |
| `paper/section-level-prose-polish-map-md.md` | rule that C1 should read as the first technical mechanism |
| `paper/reviewer-response-draft-md.md` | C1 scale, ablation, and benefit-scoring response boundaries |
| `paper/final-paper-outline-md.md` | section placement and paragraph plan |
| `paper/paper-narrative-spine.md` | C1 story inside the full paper spine |
| `paper/five-step-total-control-summary.md` | current P1/C1 status and missing SF30/SF100 boundary |
| `paper/c1-ablation-command-bundle-audit.md` | command bundle and current C1 evidence audit |
| `remote-logs/p7-05-edge-type-mismatch-diagnosis-20260604/ablation-refresh-summary-after-fix.tsv` | after-fix SF1 threshold and benefit-scored metrics |
| `src/semantic.rs` | `GraphAccessSignature`, `DegreeClass`, and property predicates |
| `src/graph.rs` | semantic L0 index, degree directory, and materialization policy |
| `src/csr/format.rs` | segment metadata and safe `may_contain_signature` pruning |
| `tests/engine_tests.rs` | executable C1 regression anchors |

## Intended Paper Placement

This draft corresponds to:

```text
Section 4. Query-Semantic Physical Design
```

It should appear after the Introduction and system overview, before feedback
compaction and schema/snapshot safety. The section answers:

```text
Can query semantics materially reduce LSM graph read amplification?
```

It is the first technical mechanism in the polished paper chain:

```text
C1 query-semantic physical design
-> benefit-scored materialization
-> P3 feedback-driven semantic compaction
-> P2 schema-evolution-aware metadata
-> P4 snapshot-correct semantic deltas
-> P5 source-ready evidence package
```

## Draft Section Text

### 4. Query-Semantic Physical Design

LSM graph stores are update-friendly, but their read path can be forced to probe
many overlapping L0 segments. SemL0's first design step is to make the graph
predicates that matter to a storage lookup visible to the LSM layout. Instead of
treating all recent edge segments as equally plausible candidates, SemL0 maps a
storage-facing graph access into a query-semantic signature.

This section should therefore be read as a mechanism section, not as a table
walkthrough. Its local flow is:

```text
problem pressure -> query-semantic signature -> exact segment summary -> bounded materialization -> SF1 evidence boundary -> bridge to feedback
```

The signature is not a full logical query plan. It is the subset of query
semantics that can guide physical pruning:

```text
source label
edge type
direction
degree class
required property presence
property value predicate within the implemented boundary
snapshot/schema safety state
```

In the implementation, this role is represented by `GraphAccessSignature`.
Neighbor scans can bind an edge type, attach a `DegreeClass`, and require
property presence or value predicates. The segment metadata then answers a
storage question: can this segment contain an edge visible to this signature?

SemL0's pruning contract is conservative:

```text
exact summaries may prune; mixed, unknown, legacy, or tombstone-sensitive
metadata must stay in the candidate set.
```

This contract is what makes C1 compatible with later schema and snapshot
sections. Query-semantic metadata is allowed to reduce reads only when it proves
that a segment is disjoint from the requested signature. Otherwise the system
over-probes rather than risking a false negative.

The rest of the paper keeps this same contract. P3 can change which semantic
ranges are compacted first, but it cannot make pruning less conservative. P2 can
advance catalog epochs for future writes, but old segment summaries remain
interpreted through their recorded schema state. P4 can compact and hide
tombstones only when snapshot visibility stays safe.

### Segment Summaries

Each CSR-like segment carries semantic metadata about the range it contains. The
current implementation tracks dimensions such as source label, edge type, degree
class, and property presence bitmap. For exact semantic segments, these metadata
fields can prove that a segment contains only a specific edge-type and degree
class combination. For mixed or unknown segments, the same fields deliberately
lose pruning power.

This creates three operational cases:

| Segment metadata state | Read-path action |
|---|---|
| exact match | keep as a candidate |
| exact disjointness or exact absent required property | prune |
| mixed, unknown, legacy, or tombstone-sensitive summary | keep conservatively |

The useful point is that SemL0 can reduce read amplification without changing
the logical query semantics. The physical design changes which L0 files are
considered first, but correctness still depends on exact-proof pruning and
row-level validation.

This is the main distinction from a benchmark-only optimization. C1 does not
claim that a particular threshold always wins. It claims that making graph
semantics storage-visible creates a safe pruning surface; the later policy and
feedback machinery decide which parts of that surface are worth maintaining.

### Semantic L0 Index and Degree Directory

SemL0 builds a semantic L0 index over recent CSR segment metadata. The index
maps signature dimensions to candidate L0 files, so a lookup over a specific
edge type and degree class can avoid scanning unrelated exact segments.

Degree-aware pruning is important because graph neighborhoods are skewed. A
high-degree source and a low-degree source have different read-amplification
profiles. SemL0 therefore records `DegreeClass` summaries and maintains a degree
directory that routes degree-sensitive lookups to the right segment classes. The
read path still includes mixed and unknown classes when needed, preserving the
same conservative fallback rule.

The C1 section should describe this as a physical-design mechanism, not as a
new logical query language feature. The user asks for neighbors; SemL0 decides
which L0 semantic partitions are safe candidates.

### Property Presence and Value Boundaries

Property graph queries often require more than topology. A segment that lacks a
required property cannot satisfy a required-property predicate. SemL0 records a
property presence bitmap for representable property ids and uses it only for
safe absence reasoning.

The current implementation boundary is intentionally narrow:

```text
fixed-width property equality and required-property presence
```

The paper should not generalize this to string/range/compound predicates or full
SQL null semantics. If a property id is not representable in the bitmap, or if
the segment's property metadata is incomplete, SemL0 reads conservatively.

### Selective Materialization

Full semantic materialization can reduce reads, but it can also increase segment
fanout and metadata cost. SemL0 therefore uses a benefit-scored materialization
policy. The policy estimates whether preserving an exact semantic partition is
worth the additional file and metadata fanout, using dimensions such as edge
type, degree class, segment size, and benefit score threshold.

This turns C1 from "materialize everything" into a bounded physical-design
policy:

```text
materialize high-value semantic partitions
merge low-benefit partitions into mixed segments
preserve correctness by reading mixed segments conservatively
```

The after-fix SF1 ablation makes this tradeoff visible. Full semantic
materialization is an upper-bound row with high read reduction. Fine threshold
variants preserve many exact files and can achieve strong read reductions, but
with high fanout. Micro threshold variants keep correctness but provide
negligible read benefit. The benefit-scored candidate is the current balanced
paper row, retaining zero core and all-types mismatches while reducing read
bytes without fully materializing every semantic partition.

The current after-fix metrics show the boundary:

```text
No latest-code SF30/SF100 final performance claim.
```

| Variant | Role | Core read reduction | All-types read reduction | Correctness |
|---|---|---:|---:|---|
| `schema` | baseline | 0.00% | 0.00% | baseline row |
| `full_semantic` | upper bound | 99.35% | 94.88% | upper-bound row |
| `fine64k` | high-fanout threshold | 99.32% | 94.84% | 0 core / 0 all-types mismatches |
| `fine512k` | coarser threshold | 97.06% | 92.64% | 0 core / 0 all-types mismatches after fix |
| `micro544k` | low-benefit threshold | 0.18% | 0.15% | 0 core / 0 all-types mismatches |
| `benefit_scored` | current balanced candidate | 72.86% | 58.89% | 0 core / 0 all-types mismatches |

These rows support a bounded claim:

```text
query-semantic physical design can reduce SF1 read amplification, and benefit
scoring is needed to avoid the cost of indiscriminate semantic fanout.
```

They do not prove latest-code SF30/SF100 final performance, global optimizer
optimality, or negligible write overhead.

This sets up the next section. A static benefit-scored layout can still become
stale when the workload shifts. Feedback compaction keeps the same semantic
metadata but changes the maintenance priority: hot signatures receive compaction
effort first, while the exact-proof read rule remains unchanged.

### Correctness Boundary

C1 is useful only if it preserves query correctness. The important regression
history is the edge-type mismatch diagnosis and repair: earlier threshold
variants exposed all-types mismatches, and the later fix preserved conservative
degree behavior by expanding mixed segment classes where necessary. The
after-fix summary records:

```text
core_mismatches=0
alltypes_mismatches=0
failed=0
```

This is why the paper should emphasize exact-proof pruning instead of aggressive
semantic pruning. A segment can be skipped only when its metadata proves it
cannot contain a matching visible edge. Otherwise, extra reads are acceptable.
False negatives are not.

### Section Boundary

Safe C1 wording:

```text
SemL0 exposes graph query signatures to LSM segment metadata and uses exact
semantic summaries, degree-aware routing, property-presence summaries, and
benefit-scored materialization to reduce SF1 read amplification under the
current evidence package.
```

Unsafe C1 wording:

```text
SemL0 always chooses the optimal semantic layout.
SemL0 has negligible write overhead.
SemL0 proves final latest-code SF30/SF100 performance.
SemL0 can safely prune all mixed or unknown metadata.
SemL0 supports arbitrary property predicates in the semantic index.
```

## Evidence Map

| Claim | Evidence anchor |
|---|---|
| `GraphAccessSignature` is the storage-facing signature | `src/semantic.rs`; `tests/engine_tests.rs` |
| segment summaries implement exact/conservative pruning | `src/csr/format.rs`; `src/graph.rs` |
| semantic L0 index and degree directory route candidates | `src/graph.rs`; degree-routing tests |
| property presence bitmap supports safe absence reasoning | `src/csr/format.rs`; property-presence tests |
| benefit scoring bounds materialization | `src/graph.rs`; `src/config.rs`; Table 2 and Table 5 |
| after-fix C1 threshold rows are clean | `remote-logs/p7-05-edge-type-mismatch-diagnosis-20260604/ablation-refresh-summary-after-fix.tsv` |
| command bundle is auditable | `paper/c1-ablation-command-bundle-audit.md` |
| SF30/SF100 latest-code refresh is out of scope | `paper/five-step-total-control-summary.md` |

## Claims To Avoid In This Section

Do not write:

```text
global optimizer optimality
negligible write overhead
final latest-code SF30/SF100 performance
all property predicates are indexed
mixed metadata can always be pruned
schema evolution is solved by C1 alone
```

Use this bounded closing:

```text
C1 shows that query semantics can be made visible to LSM physical design, but
the safety and adaptivity story depends on the later feedback, schema, and
snapshot sections.
```

The intended bridge sentence is:

```text
C1 defines the semantic pruning surface; P3 decides how that surface is
maintained when workload feedback changes.
```

## Next Use

If TeX editing is later approved, translate this MD into the query-semantic
physical design section without broadening the evidence boundary. If a fresh
C1/SF30/SF100 refresh is approved later, create a new run stage and update this
draft only after the new logs, tables, and claim-boundary audit exist.
