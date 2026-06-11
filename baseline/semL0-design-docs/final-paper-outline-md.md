# SemL0 Final Paper Outline MD

Date: 2026-06-05

## Purpose

This is the MD-only working outline for turning the SemL0 evidence package into
a paper draft. It follows `paper/paper-narrative-spine.md` and should be used
before any TeX rewrite. It does not generate TeX/PDF, run experiments, delete
stores, choose a venue, or mark final submission ready.

## Paper Positioning

Working title:

```text
SemL0: Query-Semantic Physical Design for LSM-Based Dynamic Property Graphs
```

Core thesis:

```text
Property-graph query semantics should shape LSM physical layout, compaction, and
metadata safety. SemL0 uses query signatures to reduce read amplification while
preserving conservative correctness under feedback compaction, additive schema
evolution, and snapshot-visible deltas.
```

Do not position the paper as:

```text
a complete graph DBMS
a full schema-migration engine
a production write-stall study
a final SF30/SF100 performance report
```

## Abstract Plan

The abstract should have five moves:

1. LSM-based graph stores handle updates but make L0 reads expensive because
   layout is mostly query-semantics blind.
2. SemL0 exposes graph query signatures to physical layout and segment metadata.
3. Benefit scoring and feedback compaction keep semantic layout selective and
   adaptive.
4. Schema epochs, exact-proof pruning, tombstone summaries, and conservative
   metadata keep old storage readable and prevent false negatives.
5. Current evidence shows SF1 read-amplification reduction, controlled feedback
   adaptation, write-cost proxy accounting, and targeted schema/snapshot
   correctness; final venue/PDF and SF30/SF100 refresh remain outside the current
   package.

## Section Outline

### 1. Introduction

Reader question:

```text
Why does an LSM graph store need query-semantic physical design?
```

Paragraph plan:

1. Dynamic property graphs need update-friendly storage, so LSM-style deltas are
   attractive.
2. The read path is expensive because L0 overlap forces repeated probing of
   segments that are irrelevant to the actual graph predicate.
3. Property graph queries are not just topology reads: they involve labels, edge
   types, directions, degree classes, properties, and schema epochs.
4. SemL0's thesis: expose those semantics to layout, metadata, compaction, and
   safe pruning.
5. Contributions: C1 layout, benefit policy, P3 feedback compaction, P2/P4
   schema/snapshot safety, evidence package.

Evidence to cite:

```text
paper/tables/table1-main-sf1-latest.tex
paper/tables/table2-budget-policy-comparison.tex
paper/tables/table4-correctness-summary.tex
paper/paper-narrative-spine.md
```

Do not say:

```text
SemL0 is a complete graph database.
SemL0 proves production-scale adaptive compaction.
```

### 2. Background And Problem

Reader question:

```text
What exactly is query-semantics blindness in LSM graph storage?
```

Paragraph plan:

1. Explain LSM graph layout: update locality and overlapping L0 segments.
2. Explain graph query signatures: source label, edge type, direction, degree,
   property presence/value, snapshot, schema epoch.
3. Explain false-negative risk: pruning is useful only if metadata proves a
   segment cannot contain a visible match.
4. State the shared contract used by all later sections.

Core contract:

```text
Only prune when metadata proves disjointness or absence; otherwise read
conservatively.
```

Evidence to cite:

```text
paper/schema-change-decision-matrix.md
paper/mixed-schema-delta-stress-inventory.md
tests/engine_tests.rs
```

### 3. System Overview

Reader question:

```text
How do the four loops fit together?
```

Paragraph plan:

1. Show the write path emits CSR-like L0 segments with semantic metadata.
2. Show the read path compiles query predicates into `GraphAccessSignature`.
3. Show metadata-driven pruning and fallback over-probing.
4. Show feedback-driven compaction as a maintenance loop.
5. Show schema catalog and snapshot metadata as safety layers.

Figure:

```text
paper/figures/seml0-pipeline.pdf
```

Section must connect:

```text
C1 layout
P3 feedback compaction
P2 schema epochs
P4 snapshot/tombstone deltas
```

### 4. Query-Semantic Physical Design

Reader question:

```text
Can query semantics materially reduce read amplification?
```

Paragraph plan:

1. Define `GraphAccessSignature` as a storage-facing signature, not a logical
   query plan.
2. Explain segment summaries over labels, edge types, degree classes, and
   property requirements.
3. Explain exact summaries, conservative summaries, and unknown summaries.
4. Explain semantic L0 index and degree-aware pruning.
5. Explain why benefit-scored materialization is needed instead of full
   semantic materialization.

Evidence:

```text
paper/tables/table1-main-sf1-latest.tex
paper/tables/table2-budget-policy-comparison.tex
paper/tables/table5-c1-threshold-ablation-after-fix.tex
paper/c1-ablation-command-bundle-audit.md
remote-logs/p7-05-edge-type-mismatch-diagnosis-20260604/ablation-refresh-summary-after-fix.tsv
```

Safe claims:

```text
SF1 latest-code read-amplification reduction
benefit scoring preserves useful semantic partitions
raw thresholds expose fanout/read-benefit cliffs
```

Boundary:

```text
No latest-code SF30/SF100 final performance claim.
```

### 5. Feedback-Driven Semantic Compaction

Reader question:

```text
Can semantic layout adapt when workload hot spots shift?
```

Paragraph plan:

1. Static semantic layout can become misaligned with current query hot spots.
2. SemL0 records runtime feedback: query counts, candidate fanout, read bytes,
   and rewrite-size proxies.
3. The compaction loop chooses hot semantic ranges when the rewrite cost is
   justified.
4. Controlled workload-shift evidence shows candidate reduction after feedback.
5. Rewrite-cost accounting pairs read savings with compaction input/output bytes.

Evidence:

```text
paper/tables/table3-feedback-adaptation.tex
paper/tables/table7-feedback-compaction-cost.tex
paper/sustained-feedback-compaction-plan.md
remote-logs/p7-02-feedback-workload-shift-20260604/summary.txt
remote-logs/p6-5-write-cost-extraction-20260604/write_cost_compaction_microbench.csv
```

Safe claim:

```text
controlled workload-shift microbenchmarks show feedback can adapt semantic
compaction priority.
```

Do not say:

```text
negligible write overhead
production write-stall safety
long-running SF30/SF100 adaptive validation
global optimizer optimality
```

### 6. Schema Evolution And Snapshot-Correct Deltas

Reader question:

```text
Does a schema change make old semantic storage unsafe or useless?
```

Lead answer:

```text
No. Additive schema changes advance catalog interpretation and future-write
routing; old segments remain readable under their original schema_epoch.
```

Paragraph plan:

1. Explain versioned schema catalog and segment `schema_epoch`.
2. Explain add edge label/property: catalog epoch plus future-write routing, not
   global rebuild.
3. Explain exact-proof pruning versus conservative reads.
4. Explain alias/drop/encoding boundaries without overclaiming migration.
5. Explain snapshot/tombstone interaction and why compaction cannot collapse
   history past the safe point.
6. State the no-false-negative invariant.

Figure:

```text
paper/figures/schema-snapshot-timeline.pdf
```

Evidence:

```text
paper/schema-change-decision-matrix.md
paper/schema-evolution-reviewer-brief.md
paper/tables/table4-correctness-summary.tex
paper/tables/table10-schema-evolution-claim-gap-map.tex
paper/mixed-schema-delta-stress-inventory.md
remote-logs/p5-schema-delta-consolidation-20260604/summary-final.txt
remote-logs/p6-20-schema-report-smoke/schema-evolution-report-sf1-schema.json
```

Safe claims:

```text
add-label/add-property schema changes do not invalidate old storage
unknown/mixed metadata causes extra reads, not false negatives
targeted schema/snapshot/tombstone regressions pass
```

Boundary:

```text
full rename/drop/type-change physical migration remains future work
range/string/compound predicates and SQL null semantics remain future work
```

### 7. Evaluation

Reader question:

```text
Which evidence supports each claim?
```

Evaluation questions:

1. Does semantic layout reduce read amplification?
2. Does benefit scoring avoid the cost of full semantic materialization?
3. Does feedback compaction adapt to a workload shift?
4. Do schema evolution and dynamic deltas preserve correctness?
5. What write/storage/rewrite costs are visible?

Table placement:

```text
RQ1: Table 1
RQ2: Tables 2 and 5
RQ3: Tables 3 and 7
RQ4: Table 4 and Table 10
RQ5: Tables 6 and 7
```

Evaluation boundary:

```text
SF30/SF100 latest-code refresh is not part of the current frozen package.
```

### 8. Related Work

Reader question:

```text
What is SemL0 different from?
```

Position against:

- LSM/KV systems: compaction and write-optimized layout, but not graph query
  signatures as first-class metadata.
- Graph stores: topology-aware storage, but less focus on LSM L0 semantic
  pruning under dynamic updates.
- Adaptive indexing: query-driven physical design, but different physical
  object and safety boundary.
- Schema evolution systems: versioned interpretation, but SemL0 applies it to
  graph LSM segment metadata and semantic pruning.

Do not turn related work into broad claim expansion.

### 9. Limitations

Must include:

```text
no final SF30/SF100 latest-code refresh
controlled feedback evidence only
no production write-stall characterization
no full schema-migration engine
range/string/compound predicates future work
final submission readiness blocked by venue/TeX/PDF/page/visual gates
```

### 10. Conclusion

Conclusion should return to the thesis:

```text
query semantics can guide LSM physical design and maintenance while preserving
conservative correctness under schema evolution and dynamic deltas.
```

Do not add new claims in the conclusion.

## Appendix Plan

Appendix should contain:

```text
claim/evidence map
artifact path map
schema-evolution boundary matrix
reproduction command pointers
final-submission blockers
```

Existing anchors:

```text
paper/tables/table8-claim-evidence-map.tex
paper/tables/table9-artifact-path-map.tex
paper/tables/table10-schema-evolution-claim-gap-map.tex
paper/artifact-checklist.md
paper/package-manifest.md
```

## Drafting Order

Recommended writing order:

1. Introduction and contributions.
2. System overview and shared safety contract.
3. C1 physical design.
4. Schema/snapshot correctness section.
5. Feedback compaction section.
6. Evaluation.
7. Limitations.
8. Related work and conclusion.
9. Appendix alignment.

Reason:

```text
The introduction and safety contract constrain all later claims. Schema/snapshot
correctness should be locked before broadening feedback or evaluation wording.
```

## Final-Submission Boundary

This outline is not a submission-ready artifact by itself.

Remaining final-submission gates:

```text
target venue
venue template
TeX/PDF backend
compiled PDF
page budget
PDF visual inspection
```

