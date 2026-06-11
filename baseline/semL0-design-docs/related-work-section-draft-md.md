# SemL0 Related Work Section Draft MD

Date: 2026-06-05

## Status

```text
related_work_section_draft_ready=yes
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

This MD document is the paper-section draft for Related Work. It positions
SemL0 against five lines of prior work without expanding the paper's claims:
LSM and key-value storage, dynamic graph storage, adaptive physical design,
materialized views, and schema evolution.

This stage does not edit TeX, generate PDF, run experiments, rerun benchmarks,
delete stores, choose a venue, add new bibliography entries, or mark final
submission ready.

## Source Anchors

| Anchor | Role |
|---|---|
| `paper/references.bib` | citation keys already present in the package |
| `paper/final-paper-outline-md.md` | Related Work paragraph plan and claim boundary |
| `paper/paper-narrative-spine.md` | global contribution positioning |
| `paper/introduction-section-draft-md.md` | contribution wording and scope |
| `paper/evaluation-prose-polish-md.md` | system-chain evidence order |
| `paper/limitations-prose-polish-md.md` | system-chain claim boundaries |
| `paper/query-semantic-physical-design-prose-polish-md.md` | C1 positioning and SF1 boundary |
| `paper/feedback-compaction-prose-polish-md.md` | P3 positioning and sustained-run boundary |
| `paper/schema-evolution-prose-polish-md.md` | P2/P4 positioning and no-full-migration boundary |
| `paper/background-problem-section-draft-md.md` | query-semantics blindness problem statement |
| `paper/system-overview-section-draft-md.md` | SemL0 pipeline and exact-proof contract |
| `paper/query-semantic-physical-design-section-draft-md.md` | C1 physical-design mechanism |
| `paper/feedback-compaction-section-draft-md.md` | P3 adaptive maintenance boundary |
| `paper/schema-evolution-section-draft-md.md` | P2/P4 schema and snapshot safety boundary |
| `paper/evaluation-section-draft-md.md` | evidence scope and limitations |

## Intended Paper Placement

This draft corresponds to:

```text
Section 8. Related Work
```

It should appear after the Evaluation and before Limitations. It answers:

```text
What is SemL0 different from?
```

## Draft Section Text

### 8. Related Work

SemL0 draws on several mature storage and database research lines, but its
claim is narrower than each of them. The paper is not proposing a new generic
LSM merge policy, a complete dynamic-graph DBMS, a logical materialized-view
system, or a full schema-migration engine. It argues that property-graph query
semantics can be made visible to the LSM delta region through segment metadata,
exact-proof pruning, feedback compaction, and schema/snapshot-aware
interpretation.

This positioning follows the same system chain used in Evaluation and
Limitations:

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

Related Work should therefore explain what SemL0 borrows from adjacent areas
and what it does not claim to solve.

### LSM-Based Storage

The log-structured merge-tree introduced the core idea of write-friendly storage
through buffered updates and background merging [@oneil1996lsm]. Production
systems such as RocksDB show the practical importance of compaction, memory
management, and tuning for large-scale key-value deployments [@dong2021rocksdb].
Subsequent work explores LSM tradeoffs through memory allocation and filters
[@dayan2017monkey], merge policy design [@dayan2018dostoevsky], key-value
separation [@lu2016wisckey], and fragmented LSM structures [@raju2017pebblesdb].

SemL0 is complementary to this line of work. It does not introduce a new
generic LSM policy or claim optimal merge behavior for arbitrary key-value
stores. Instead, it changes the physical-design signal for an LSM-based graph
store: recent graph segments are organized and pruned using graph access
signatures such as edge type, degree class, property presence, snapshot
visibility, and schema epoch. The LSM background explains why L0 overlap creates
read amplification; SemL0's contribution is to make graph query semantics usable
inside that overlap.

### Dynamic Graph Storage

Dynamic graph systems optimize mutable graph storage, transactional access, and
fresh analytics. LiveGraph uses a transactional edge log to support sequential
adjacency-list scans [@zhu2020livegraph]. Teseo targets structural dynamic
graphs and update/scan performance [@leo2021teseo]. LLAMA uses large
multiversioned arrays to support mutable graph analytics over CSR-like
representations [@macko2015llama]. The LDBC Social Network Benchmark provides a
standard interactive workload for evaluating graph systems [@erling2015ldbc].

SemL0 shares the dynamic-graph motivation, but its object of optimization is
the LSM delta region rather than a complete graph execution engine. Its segment
metadata exposes labels, edge types, degree classes, property summaries, schema
epochs, and tombstone sensitivity to candidate pruning and compaction. This is a
storage-layer physical-design question: can query semantics reduce the number
of recent segments read without violating snapshot or schema correctness?

### Adaptive Physical Design

Database cracking and adaptive indexing show that access paths can be refined
incrementally by the workload rather than fixed entirely offline
[@idreos2007cracking; @idreos2012adaptive]. SemL0 follows the same broad idea
that observed reads should influence physical layout. The difference is the
physical object and safety boundary. SemL0 does not adapt a relational index in
place. It uses runtime feedback to identify hot graph access signatures and to
choose L0 semantic ranges for compaction.

The feedback loop is deliberately scoped. Runtime feedback may change future
layout and reduce future candidates, but it does not change query semantics. A
segment remains prunable only when metadata gives an exact proof of absence or
disjointness. This makes adaptive maintenance compatible with schema epochs,
tombstones, and snapshot-visible deltas.

### Materialized Views

Materialized-view systems maintain logical query results or derived relations
and study the cost of keeping those results up to date [@gupta1995views]. View
redefinition work studies whether existing materializations can be adapted when
logical definitions change [@gupta1995viewredef].

SemL0 materializes a different object. A semantic L0 segment is not a cached
query answer and does not replace query execution. It is a physical access
layout that lets future reads skip irrelevant LSM records when exact segment
metadata proves the skip is safe. Schema changes may make some old metadata less
selective, but they do not invalidate the old bytes. Lazy compaction can later
repair high-value layouts; it is not a general view-redefinition engine.

### Schema Evolution

Schema-evolution systems preserve access to existing data while logical
definitions change. PRISM supports legacy queries through schema modification
operators [@curino2008prism]. PRIMA connects transaction-time data with
historical schema versions [@moon2008prima]. F1 provides online asynchronous
schema change for a distributed relational system [@rae2013f1schema].
Tesseract models schema evolution in snapshot databases as
data-definition-as-modification [@hu2022tesseract].

SemL0 applies the versioning principle to graph LSM segment metadata. A schema
change advances the catalog epoch and affects future writes, while old segments
remain interpreted under their original `schema_epoch`. Semantic pruning remains
safe because missing, legacy, mixed, tombstone-sensitive, or unrepresentable
metadata is not treated as proof of absence. The current paper claim is
therefore additive no-rebuild schema evolution for the implemented boundary,
not complete physical migration for all schema changes.

### Positioning Summary

| Related line | What it contributes | SemL0's different focus |
|---|---|---|
| LSM/key-value storage | write-friendly deltas, compaction, and space/time tradeoffs | graph query signatures as physical-design signals for the LSM delta region |
| Dynamic graph storage | mutable graph representation, transactional graph access, fresh analytics | semantic pruning and compaction of recent LSM graph segments |
| Adaptive indexing | workload-driven refinement of access paths | feedback-driven semantic compaction with exact-proof pruning |
| Materialized views | maintenance of logical query results and view redefinition | physical access layouts, not cached query answers |
| Schema evolution | versioned interpretation across logical schema changes | graph LSM segment `schema_epoch` and conservative pruning fallback |

This positioning keeps the paper's claim specific:

```text
Property-graph query semantics can safely guide LSM delta layout, candidate
pruning, feedback compaction, and schema/snapshot metadata.
```

## Positioning Boundary Map

| Related line | SemL0 system role | Boundary |
|---|---|---|
| LSM/key-value storage | explains L0 overlap and compaction pressure for the C1 pruning surface | not a new generic LSM merge policy |
| Dynamic graph storage | motivates mutable graph storage and snapshot-aware reads | not a complete graph DBMS |
| Adaptive physical design | motivates workload-driven maintenance for P3 | not production long-running adaptive scheduling |
| Materialized views | clarifies the difference between physical layout and logical result maintenance | not a cached query-answer or view-redefinition system |
| Schema evolution | motivates catalog-versioned interpretation for P2/P4 | not a full physical schema-migration engine |
| Artifact/evaluation systems | motivates evidence mapping and source-ready packaging | not final-submission readiness |

## Citation Key Ledger

| Topic | Citation keys used |
|---|---|
| LSM foundation and systems | `oneil1996lsm`, `dong2021rocksdb` |
| LSM tradeoffs | `dayan2017monkey`, `dayan2018dostoevsky`, `lu2016wisckey`, `raju2017pebblesdb` |
| Dynamic graph storage | `zhu2020livegraph`, `leo2021teseo`, `macko2015llama` |
| Graph benchmark context | `erling2015ldbc` |
| Adaptive physical design | `idreos2007cracking`, `idreos2012adaptive` |
| Materialized views | `gupta1995views`, `gupta1995viewredef` |
| Schema evolution | `curino2008prism`, `moon2008prima`, `rae2013f1schema`, `hu2022tesseract` |

## Claims To Avoid In Related Work

Do not write:

```text
SemL0 is a new general LSM merge policy.
SemL0 is a complete dynamic graph DBMS.
SemL0 replaces graph query processing with materialized views.
SemL0 implements full schema migration.
SemL0 proves production-scale adaptive compaction.
Prior graph stores cannot use semantics at all.
Prior LSM work is irrelevant to graph storage.
```

## Bridge To Limitations

The next section should make the boundaries explicit rather than leaving them
implicit in Related Work:

```text
no final latest-code SF30/SF100 refresh
controlled feedback evidence only
no production write-stall characterization
no full schema-migration engine
range/string/compound predicates are future work
final submission readiness still blocked by venue, TeX/PDF, page, and visual gates
```
