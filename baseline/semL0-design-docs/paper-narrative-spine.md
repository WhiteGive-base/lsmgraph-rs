# SemL0 Paper Narrative Spine

Date: 2026-06-05

## Purpose

This MD document is the paper-story blueprint for SemL0. It turns the current
implementation and evidence package into one narrative spine instead of a list of
logs. It is not a TeX rewrite, does not run experiments, does not delete stores,
and does not promote final submission readiness.

## One-Sentence Thesis

```text
SemL0 makes property-graph query semantics a first-class input to LSM physical
design, so layout, compaction, schema evolution, and delta correctness are all
driven by the predicates that real graph queries need.
```

The paper should not be framed as "we optimized LSMGraph" or "we added many graph
database features." The core frame is:

```text
query semantics -> physical layout choices -> adaptive maintenance -> safe
schema/snapshot interpretation
```

## Narrative Spine

| Narrative step | Reader question | SemL0 answer | Evidence anchor |
|---|---|---|---|
| Problem | Why are LSM graph reads expensive? | L0 overlap is topology/update friendly but query-semantics blind. | intro/problem framing; Table 1 baseline |
| C1 layout | Can query semantics reduce read amplification? | Encode labels, edge types, degree classes, and property requirements into semantic segment metadata and candidate pruning. | Tables 1, 2, 5; C1 command-bundle audit |
| Budget policy | Can the system avoid materializing everything? | Benefit-scored materialization preserves high-value semantic partitions while bounding fanout. | Table 2; after-fix threshold ablation |
| P3 feedback | Can layout adapt after workload shift? | Runtime feedback identifies hot semantic ranges and selects compaction candidates. | Table 3; Table 7; sustained feedback plan |
| P2 schema | Does schema change invalidate old storage? | No. Catalog epochs and exact-proof pruning keep old stores readable; uncertain metadata over-probes. | schema-change decision matrix; Table 10 |
| P4 delta/snapshot | Does semantic pruning stay correct under updates? | Tombstones, schema epochs, property epochs, and snapshot rules preserve no-false-negative pruning. | Table 4; mixed schema-delta inventory |
| Boundary | What does the paper not claim? | No final SF30/SF100 refresh, no production write-stall proof, no broad schema migration engine, no final PDF readiness. | limitations; total-control summary |

## Section-Level Story

### 1. Introduction

The introduction should lead with the mismatch:

```text
LSM graph storage handles updates well, but L0 overlap makes query-time graph
semantics invisible to the storage layout.
```

Then state SemL0's idea:

```text
Expose query signatures to segment metadata, layout selection, feedback
compaction, and schema/snapshot-safe pruning.
```

The introduction should name four connected loops, not four unrelated features:

1. C1 query-semantic physical design;
2. P3 feedback-driven semantic compaction;
3. P2 schema-evolution-aware metadata;
4. P4 snapshot-correct semantic deltas.

### 2. System Design

The design section should use one shared safety contract:

```text
Metadata may reduce the candidate set only when it proves that a segment cannot
contain a visible match.
```

This contract connects C1, P2, P3, and P4:

- C1 uses exact semantic metadata to prune irrelevant segments.
- P3 changes future layout by selective compaction, not by changing query
  semantics.
- P2 keeps old segments readable through schema epochs.
- P4 keeps inserts, deletes, tombstones, and snapshots from creating false
  negatives.

### 3. Query-Semantic Layout

This section should make C1 the first technical pillar:

```text
GraphAccessSignature maps graph query predicates to storage-facing dimensions.
```

Core claims:

- labels, edge types, direction, degree, and property requirements are storage
  metadata;
- exact summaries prune;
- unknown summaries read conservatively;
- benefit scoring decides which semantic partitions are worth preserving.

Evidence:

```text
paper/tables/table1-main-sf1-latest.tex
paper/tables/table2-budget-policy-comparison.tex
paper/tables/table5-c1-threshold-ablation-after-fix.tex
paper/c1-ablation-command-bundle-audit.md
```

### 4. Feedback-Driven Compaction

This section should not overclaim long-running adaptation. The safe story is:

```text
Controlled workload-shift evidence shows feedback can move compaction priority
toward the current hot semantic range.
```

Core claims:

- runtime feedback records query counts, candidate L0 counts, and rewrite cost;
- feedback chooses hot semantic ranges for compaction;
- current evidence is controlled microbenchmark evidence;
- sustained production-style adaptation remains a planned extension.

Evidence:

```text
paper/tables/table3-feedback-adaptation.tex
paper/tables/table7-feedback-compaction-cost.tex
paper/sustained-feedback-compaction-plan.md
remote-logs/p7-02-feedback-workload-shift-20260604/summary.txt
remote-logs/p6-5-write-cost-extraction-20260604/write_cost_compaction_microbench.csv
```

### 5. Schema Evolution

This section should answer the reviewer concern directly before details:

```text
Adding an edge label or property does not make old stores useless.
```

Core mechanism:

- schema catalog advances epochs and records logical-to-physical mappings;
- future writes use current epoch;
- old segments keep their own `schema_epoch`;
- exact old metadata can prune when it proves disjointness;
- unknown/mixed/legacy metadata reads conservatively;
- lazy compaction repairs hot layouts later but is not needed for correctness.

Evidence:

```text
paper/schema-change-decision-matrix.md
paper/schema-evolution-reviewer-brief.md
paper/tables/table10-schema-evolution-claim-gap-map.tex
remote-logs/p5-schema-delta-consolidation-20260604/summary-final.txt
remote-logs/p6-20-schema-report-smoke/schema-evolution-report-sf1-schema.json
```

### 6. Snapshot-Correct Deltas

This section should show why semantic pruning is not just an optimization but a
correctness-sensitive decision:

```text
Deletes, tombstones, degree changes, schema epochs, and snapshot visibility can
all make aggressive pruning unsafe unless metadata is conservative.
```

Core claims:

- tombstones can suppress older visible inserts;
- compaction cannot collapse history past the snapshot safe point;
- property/schema epochs compose with snapshot visibility;
- correctness evidence is targeted, not exhaustive dynamic graph proof.

Evidence:

```text
paper/tables/table4-correctness-summary.tex
paper/mixed-schema-delta-stress-inventory.md
remote-logs/p5-schema-delta-consolidation-20260604/summary-final.txt
tests/engine_tests.rs
```

### 7. Evaluation

The evaluation should answer six questions:

1. Does semantic layout reduce read amplification?
2. Does benefit scoring avoid the cost of full materialization?
3. Does feedback compaction adapt to a workload shift?
4. Do schema evolution and dynamic deltas preserve correctness?
5. What write/storage/rewrite costs are visible in current evidence?
6. What end-to-end latency and throughput does the system deliver under concurrent workloads?

The evaluation should avoid implying:

- latest-code SF30/SF100 final results;
- production write-stall characterization;
- global optimizer optimality;
- full schema-migration completeness.

#### RQ6: End-to-End Query Latency and Throughput Under Concurrent Workloads

RQ6 evaluates the system's end-to-end behavior under realistic concurrent workloads,
addressing the latency and throughput questions that motivate the storage-level
design.

End-to-end query latency (RQ6) measures the full request path from driver submission
to response receipt, including HTTP transport, storage access, and serialization.
The LDBC SNB interactive workload provides 29 operation types (14 complex reads,
7 short reads, 8 updates) with ground-truth answers. We report P50, P95, and P99
latency for each operation class across multiple concurrency levels.

Mixed workload evaluation varies the read/write ratio from 100:0 to 0:100 to
determine whether the semantic L0 layout affects update throughput or introduces
write stalls under concurrent reader pressure.

Current evidence supports SF10-scale end-to-end latency characterization across
multiple concurrency levels (4, 8, 16 threads) for the benefit-scored layout.
Scale-up evidence (SF30) and cross-variant comparison (schema vs. semantic vs.
full-compact) are available through the E11-E17 experiment matrix.

Boundary:
  No production write-stall characterization under true concurrent multi-tenant load.
  No long-running sustained-write pressure test.

### 8. Limitations

Limitations should be explicit and defensible:

- no final SF30/SF100 latest-code refresh in the current package;
- feedback evidence is controlled, not production long-run;
- schema migration is bounded to additive claims plus boundary/prototype support;
- range/string/compound predicates and SQL null semantics are future work;
- final submission still needs venue/template, TeX/PDF, page budget, and visual
  inspection.

## Contribution Wording

Use this contribution shape:

1. A query-semantic LSM physical design for dynamic property graphs.
2. A benefit-scored materialization policy that balances read reduction and
   semantic fanout.
3. A feedback-driven compaction loop that adapts semantic layout under controlled
   workload shifts.
4. A schema/snapshot correctness model that keeps old storage readable and
   prevents false-negative pruning under additive schema changes and dynamic
   deltas.
5. An evidence package with source-ready tables, write-cost proxies, claim
   boundaries, and reproducibility artifacts.

Avoid this shape:

```text
We implemented a graph database.
We added schema evolution, compaction, deltas, and many features.
We support all schema migrations.
We prove production-scale adaptive compaction.
```

## Paper-Safe Claim Ledger

| Claim | Safe? | Required qualifier |
|---|---|---|
| Query-semantic layout reduces SF1 read amplification | yes | latest-code SF1 scope |
| Benefit scoring is needed for useful materialization | yes | current C1 ablation scope |
| Feedback compaction adapts to workload shifts | yes | controlled microbenchmark |
| Additive schema changes do not invalidate old storage | yes | add-label/add-property/fixed-width equality boundary |
| Unknown schema metadata causes extra reads, not false negatives | yes | exact-proof-only pruning |
| Snapshot/tombstone mixed deltas preserve correctness | yes | targeted regression scope |
| End-to-end query latency under SF10 mixed workload | partial | SF10 scope, benefit-scored layout |
| Metadata/manifest storage overhead | yes | bounded by current evidence |
| Write stall risk under concurrent query/update | partial | microbenchmark-level, not production load |
| Rename/drop/type-change full physical migration | no | future work |
| Long-running production write-stall safety | no | future work |
| Final submission readiness | no | blocked by venue/TeX/PDF/page/visual gates |

## Immediate Use

Use this MD as the writing guide before editing TeX. The next TeX pass, if
requested later, should copy the narrative spine and safe claim ledger without
broadening claims beyond the evidence above.

