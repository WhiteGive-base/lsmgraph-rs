# SemL0 Introduction Section Draft MD

Date: 2026-06-05

## Status

```text
introduction_section_draft_ready=yes
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

This MD document is the paper Introduction draft for SemL0. It turns the
five-step project line, narrative spine, final outline, and current section
drafts into a coherent opening argument. It is intended as the source draft for
future paper editing, before any TeX rewrite.

This stage does not edit TeX, generate PDF, run experiments, rerun benchmarks,
delete stores, approve cleanup, choose a venue, or mark final submission ready.

## Source Anchors

| Anchor | Role |
|---|---|
| `paper/paper-narrative-spine.md` | global paper story and safe contribution wording |
| `paper/final-paper-outline-md.md` | section order, abstract plan, and introduction paragraph plan |
| `paper/five-step-total-control-summary.md` | current five-step status and open-work boundary |
| `paper/schema-evolution-section-draft-md.md` | latest schema/snapshot section draft |
| `paper/sustained-feedback-experiment-approval-packet.md` | P3 sustained feedback approval boundary |
| `paper/c1-ablation-command-bundle-audit.md` | C1 evidence reproducibility boundary |
| `paper/mixed-schema-delta-stress-inventory.md` | P2/P4 correctness evidence boundary |
| `paper/section-level-prose-polish-map-md.md` | prose-polish chain and bridge wording |
| `paper/reviewer-response-draft-md.md` | reviewer-risk response wording |

## Intended Paper Placement

This draft corresponds to:

```text
Section 1. Introduction
```

It should constrain later sections by making the core thesis, contribution
shape, evaluation questions, and limitations explicit at the start.

## Draft Section Text

### 1. Introduction

Dynamic property graphs are updated continuously, but they are queried through
semantic predicates: vertex labels, edge types, directions, degree conditions,
property requirements, property values, snapshots, and schema versions. LSM-style
storage is attractive for this setting because it absorbs writes through
sequential deltas and background compaction. However, the same L0 overlap that
makes writes cheap can make reads expensive. A graph query often probes segments
whose topology is present but whose labels, edge types, properties, or snapshot
visibility make them irrelevant.

The core problem is that conventional LSM graph storage is mostly blind to the
query semantics that decide whether a segment can contribute to a result. It may
know where recent graph updates reside, but it does not necessarily know whether
a segment can satisfy a `Person`-to-`Person` edge traversal, whether a required
edge type is absent, whether a property predicate is impossible, or whether a
schema/snapshot boundary makes a summary unsafe to prune. This mismatch turns
semantic irrelevance into read amplification.

SemL0 is built around a simple thesis:

```text
Property-graph query semantics should be first-class inputs to LSM physical
layout, compaction, and metadata safety.
```

SemL0 represents storage-facing graph access patterns as query-semantic
signatures. A signature captures the dimensions that matter to pruning and
layout selection, including labels, edge types, direction, degree class, property
presence or value requirements, snapshot visibility, and schema epoch. The write
path attaches semantic summaries to CSR-like L0 segments. The read path uses
those summaries to skip segments only when metadata proves disjointness or
absence. Unknown, mixed, legacy, or tombstone-sensitive metadata remains in the
candidate set.

This exact-proof rule is the safety contract that ties the whole system
together:

```text
metadata may reduce the candidate set only when it proves that a segment cannot
contain a visible match.
```

On top of this contract, SemL0 develops one storage chain rather than a bundle
of unrelated features. C1 query-semantic physical design makes graph access
signatures visible to segment layout and candidate pruning. Benefit-scored
materialization keeps those layouts selective under a bounded budget. P3
feedback-driven semantic compaction adapts the same layout when runtime counters
show that hot semantic ranges have shifted. P2 schema-evolution-aware metadata
keeps additive catalog changes from making old segments useless. P4
snapshot-correct semantic deltas keep tombstones, snapshots, and mixed epochs
inside the same no-false-negative pruning contract. P5 then packages the
evidence and open gates so paper claims do not outrun the artifact.

The result is not a general-purpose graph database feature checklist. SemL0 is a
storage-facing design that asks how graph query semantics should shape the LSM
delta region and its compaction policy. This scope is deliberate. The current
artifact supports additive schema evolution, targeted snapshot/tombstone
correctness, controlled feedback adaptation, and latest-code SF1 evidence for
query-semantic layout. It does not claim a complete schema-migration engine,
production write-stall characterization, global optimizer optimality, or final
SF30/SF100 latest-code performance.

The resulting paper spine is:

```text
query semantics -> physical layout choices -> adaptive maintenance -> safe schema/snapshot interpretation -> source-ready evidence package
```

This paper makes the following contributions:

1. A query-semantic LSM physical design for dynamic property graphs. SemL0
   exposes labels, edge types, directions, degree classes, property requirements,
   snapshots, and schema epochs as storage-facing metadata rather than treating
   them only as query-plan predicates.
2. A selective materialization policy for semantic L0 layouts. Benefit scoring
   preserves high-value semantic partitions while bounding metadata and fanout,
   avoiding the cost of fully materializing every semantic combination.
3. A feedback-driven semantic compaction loop. Runtime feedback identifies hot
   graph-access signatures and can redirect compaction priority under controlled
   workload shifts, with rewrite-cost proxy accounting kept separate from
   production write-stall claims.
4. A schema/snapshot safety model for semantic pruning. Versioned schema
   catalogs, segment `schema_epoch`, tombstone-sensitive summaries, and
   exact-proof-only pruning keep old storage readable and prevent false
   negatives under additive schema changes and targeted dynamic deltas.
5. A source-ready evidence package. The current package maps claims to tables,
   commands, source tests, store-retention decisions, and final-submission
   blockers, keeping paper evidence separate from unapproved future experiments.

The evaluation is organized around five questions. First, does query-semantic
layout reduce LSM read amplification? Second, does benefit scoring avoid the
cost of full semantic materialization? Third, can feedback compaction adapt to a
controlled workload shift? Fourth, do schema evolution and dynamic deltas
preserve correctness under exact-proof pruning? Fifth, what write, storage, and
rewrite costs are visible in the current evidence?

By answering these questions, SemL0 argues that property-graph query semantics
can guide LSM physical design beyond topology and write locality. The paper's
central point is not that every graph database feature is implemented. It is
that labels, edge types, properties, schema epochs, snapshots, and runtime
feedback can be made storage-visible without sacrificing conservative
correctness.

## Contribution Boundary Ledger

| Contribution item | Safe wording | Boundary |
|---|---|---|
| C1 query-semantic layout | query semantics reduce read amplification under the current SF1 evidence package | no final latest-code SF30/SF100 claim |
| selective materialization | benefit scoring bounds semantic fanout and preserves useful partitions | no claim that the policy is globally optimal |
| P3 feedback compaction | controlled workload-shift evidence shows priority can move with hot signatures | no production write-stall or long-run adaptation claim |
| P2/P4 schema/snapshot safety | additive schema changes and targeted deltas preserve old-store readability and no-false-negative pruning | no full schema-migration engine |
| P5 evidence package | claims are mapped to tables, commands, source anchors, and blockers | not final-submission ready |

## Evidence Hooks For Introduction

| Paper hook | Evidence anchor |
|---|---|
| semantic layout benefit | `paper/tables/table1-main-sf1-latest.tex`; `paper/tables/table2-budget-policy-comparison.tex` |
| materialization sensitivity | `paper/tables/table5-c1-threshold-ablation-after-fix.tex`; `paper/c1-ablation-command-bundle-audit.md` |
| feedback adaptation | `paper/tables/table3-feedback-adaptation.tex`; `paper/tables/table7-feedback-compaction-cost.tex` |
| schema/snapshot correctness | `paper/tables/table4-correctness-summary.tex`; `paper/tables/table10-schema-evolution-claim-gap-map.tex` |
| schema no-rebuild answer | `paper/schema-evolution-section-draft-md.md`; `paper/schema-change-decision-matrix.md` |
| dynamic-delta boundary | `paper/mixed-schema-delta-stress-inventory.md` |
| final boundary | `paper/five-step-total-control-summary.md`; `paper/store-cleanup-approval-packet.md`; `paper/sustained-feedback-experiment-approval-packet.md` |

## Claims To Avoid In Introduction

Do not write:

```text
SemL0 is a complete graph DBMS.
SemL0 implements full schema migration.
SemL0 proves negligible write overhead.
SemL0 proves production write-stall safety.
SemL0 validates long-running SF30/SF100 adaptive compaction.
SemL0 is final-submission ready.
```

Use this bounded closing instead:

```text
SemL0 shows that property-graph query semantics can guide LSM physical design
and maintenance while preserving conservative correctness under additive schema
evolution and targeted dynamic deltas.
```

## Next Use

If TeX editing is later approved, translate this MD into the Introduction
without broadening the claim boundary. If more compute is approved first, update
this MD only after the new evidence has its own log directory, summary, and
claim-boundary decision.
