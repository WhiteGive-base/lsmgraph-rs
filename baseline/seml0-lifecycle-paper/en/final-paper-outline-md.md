# SemL0 Final Paper Outline MD

Date: 2026-06-05

> **Updated 2026-06-17 — S0 mainline freeze, Path B (lifecycle).** This outline is
> upgraded from *Physical Design* to *Lifecycle Management* framing per
> `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md`. Main claim = read-amplification / candidate
> reduction + **lifecycle retention** + safety; **latency is supporting/observation**
> (Gate 1 = FALLBACK). `K4` is a paper codename only; kernel APIs use neutral names
> (`LevelMergePolicy`, `run_maintenance`, `split_semantic_compaction_segments`, …).
> **Load-bearing dependency: Contribution C2 (semantic merge retention) is NOT yet run.**

## Purpose

This is the MD-only working outline for turning the SemL0 evidence package into
a paper draft. It follows `paper/paper-narrative-spine.md` and should be used
before any TeX rewrite. It does not generate TeX/PDF, run experiments, delete
stores, choose a venue, or mark final submission ready.

## Paper Positioning

Working title:

```text
SemL0: Query-Semantic Lifecycle Management for LSM-Based Dynamic Property Graphs
```

Fallback title (Path A safety net only):

```text
SemL0: Query-Semantic L0 Design for LSM-Based Dynamic Property Graphs
```

Core thesis:

```text
A property-graph query signature should act as a lifecycle control signal for an
LSM-based dynamic graph store: flush materializes a per-segment semantic pruning
surface, reads exploit it, feedback finds costly partitions, and compaction retains
or rebuilds it across levels -- while pruning stays conservative-correct under
tombstones, additive schema evolution, and snapshot-visible deltas. SemL0 reduces
read amplification; latency benefit is workload-dependent and reported as such.
```

Do not position the paper as:

```text
a complete graph DBMS
a full schema-migration engine
a production write-stall study
an end-to-end / production performance report
```

## Abstract Plan

The abstract has five moves (Path B / lifecycle framing):

1. LSM-based stores keep dynamic property graphs write-friendly, but the read path is
   largely query-semantics-blind: overlapping L0 -- and, after compaction, cross-level
   -- segments are probed even when the graph predicate (label, edge type, direction,
   degree, property) proves them irrelevant.
2. SemL0 turns a property-graph query signature into a lifecycle control signal: flush
   materializes a per-segment semantic pruning surface, and a segment is pruned only
   when its metadata proves it cannot hold a visible match, else it is read
   conservatively. [evidence: W6/W8/W14 -- candidate/read-byte reduction, mismatches=0]
3. Because naive compaction collapses semantic partitions into mixed segments and
   erodes this surface, SemL0 makes compaction semantics-aware: feedback selects costly
   partitions and semantic merge retains/rebuilds the surface across levels, with read
   benefit accounted against rewrite cost. [KEYSTONE; evidence = C2 controlled + real SF30 retention/write cost + real SF30 metadata replay]
4. Under tombstones, degree change, additive schema evolution, and snapshot-visible
   deltas, SemL0 prunes only under exact evidence and otherwise reads conservatively,
   preserving a no-false-negative invariant across compaction and reopen. [evidence:
   W13 + lifecycle/reopen tests]
5. On LDBC SNB up to SF100, SemL0 cuts candidate segments and read bytes with zero
   correctness mismatches; budgeted/schema variants keep memory on par with a
   semantics-blind baseline, while unbudgeted full semantic materialization is a
   stress point. Latency gains are workload-dependent, so we report pruning-surface
   retention and its write-amplification cost rather than claiming uniform speedups.
   [latency wording bound by Gate 1 = FALLBACK; final venue/PDF packaging remains outside the package]

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
5. Contributions (Path B / lifecycle):
   - C1 query-semantic pruning surface: signatures + exact/conservative/unknown
     metadata + prune-or-keep reason taxonomy.
   - C2 lifecycle retention of that surface across flush AND compaction: semantic-aware
     merge, read-benefit-vs-rewrite-cost accounting. [keystone; evidence not yet run]
   - C3 snapshot/schema-correct pruning: no-false-negative invariant + theorem.

Evidence to cite:

```text
paper/tables/table1-main-sf1-latest.tex
paper/tables/table2-budget-policy-comparison.tex
paper/tables/table4-correctness-summary.tex
paper/paper-narrative-spine.md
baseline/sf100-matrix-20260613-cn.md            # W6 SF100 read-amp / RSS / correctness
baseline/w14-final-verdict-20260617-cn.md       # W14 necessity (partial)
SEML0-K4-STATUS-AND-PLAN-20260617-CN.md         # status + Path B plan
# C2 lifecycle-retention evidence: to be produced
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

> S0/Path B reframe: this section = **Contribution C1 (the query-semantic pruning
> surface)**. Keep the layout/metadata content; retitle around "generating and
> exploiting the pruning surface." Evidence largely reuses W6/W8/W14.

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
baseline/sf100-matrix-20260613-cn.md            # W6 SF100 matrix (read-amp / candidate / RSS)
baseline/w14-final-verdict-20260617-cn.md       # W14 edge-type-only vs composite (partial)
```

Safe claims:

```text
SF1 and SF100 read-amplification / candidate reduction (W6), zero correctness mismatch
benefit scoring preserves useful semantic partitions
raw thresholds expose fanout/read-benefit cliffs
```

Boundary:

```text
SF100 read-amp / candidate / correctness / RSS exist (W6); latency is workload-dependent
and reported as supporting only (Gate 1 = FALLBACK); composite semantics is NOT claimed
to beat edge-type-only (W14 = partial).
```

### 5. Feedback-Driven Semantic Compaction

> S0/Path B reframe -> retitle **"Lifecycle Retention of the Pruning Surface"** =
> **Contribution C2 (keystone)**. Upgrade from "L0 feedback adaptation" to "retaining/
> rebuilding the pruning surface across flush AND cross-level compaction": absorb the
> existing L1+ semantic split/filter, add the C2 experiment (retention vs degradation +
> write-amplification cost). **This is the one section whose evidence is NOT yet run.**

Reader question:

```text
Can the semantic pruning surface survive LSM compaction without excessive rewrite cost?
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

> S0/Path B reframe: this section = **Contribution C3**. Content already strong (W13 +
> lifecycle/reopen tests); add the 3 invariants (Segment Schema / Epoch-Aware
> Resolution / Conservative Pruning) + the no-false-negative theorem.

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
6. Does semantic-aware compaction RETAIN the pruning surface across levels (vs naive
   merge degradation), at bounded write amplification? [C2 keystone; not yet run]

Table placement:

```text
RQ1: Table 1
RQ2: Tables 2 and 5
RQ3: Tables 3 and 7
RQ4: Table 4 and Table 10
RQ5: Tables 6 and 7
RQ6: C2 lifecycle-retention / cost table (to be produced)
```

Evaluation boundary:

```text
SF100 matrix is done (W6: read-amp / candidate / RSS / correctness). C2 now has
controlled full-read rows, real SF30 retention/write-cost rows, and a real SF30
metadata read-amp proxy; the remaining boundary is full end-to-end SF30 body-read
execution and production scheduler characterization.
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
latency benefit is workload-dependent, not a uniform speedup (Gate 1 = FALLBACK)
composite semantics not proven to beat edge-type-only (W14 = partial)
C2 lifecycle-retention experiment and its write-amplification cost not yet run
controlled feedback evidence only
no production write-stall characterization
no full schema-migration engine
range/string/compound predicates future work
final submission readiness blocked by venue/TeX/PDF/page/visual gates
```

### 10. Conclusion

Conclusion should return to the thesis:

```text
a property-graph query signature can act as a lifecycle control signal for an LSM-based
dynamic graph store -- shaping flush-time pruning surfaces, read pruning, feedback, and
cross-level compaction retention -- while pruning stays conservative-correct under schema
evolution, tombstones, and snapshot-visible deltas.
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
