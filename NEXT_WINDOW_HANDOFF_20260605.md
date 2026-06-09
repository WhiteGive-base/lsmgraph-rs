# SemL0 New Window Handoff

Date: 2026-06-05

## Current Decision

We are not continuing paper polishing now.

The immediate goal is:

```text
Finish the code-and-experiment closure needed to support the paper claims.
```

The paper can stay as a reference package, but the next window should work from
implementation readiness and experiment evidence first.

## What Has Actually Been Improved In Code

The repository is not document-only. The code has been changed in several areas:

- C1 semantic L0 pruning and degree-aware candidate selection:
  - `src/graph.rs`
  - important fix record: `seml0-stage-p7-05-edge-type-mismatch-diagnosis-and-fix-20260604.md`
- additive schema catalog and schema epoch handling:
  - `src/schema.rs`
  - `src/graph.rs`
- CSR property metadata, property value sections, and encoding epoch hooks:
  - `src/csr/format.rs`
  - `src/csr/writer.rs`
  - `src/csr/reader.rs`
  - `src/property_encoding.rs`
- targeted correctness tests:
  - `tests/engine_tests.rs`
- feedback compaction benchmark binary:
  - `src/bin/p3_feedback_bench.rs`

The current code is best described as:

```text
scoped research prototype with targeted correctness coverage and partial experiment evidence
```

It should not yet be described as:

```text
complete production-ready schema-evolving LSM graph system
```

## Current Supported Claim Boundary

Supported with current code and existing evidence:

- additive schema changes do not automatically invalidate old storage;
- adding an edge label or property can be handled by catalog epoch plus conservative reads;
- unknown, legacy, incomplete, or tombstone-sensitive metadata must be read conservatively;
- C1 semantic pruning has a latest-code SF1 evidence path;
- a real typed-read pruning false negative was diagnosed and fixed;
- targeted snapshot/tombstone/schema/property correctness tests exist;
- feedback compaction has targeted tests and microbench scaffolding.

Not yet fully supported:

- latest-code SF30/SF100 final performance claims;
- sustained feedback stability over long workload shifts;
- production write-stall or rewrite backpressure claims;
- full schema migration for rename/drop/type-change physical rewrite;
- final one-command reproducibility for all tables and logs;
- final submission readiness.

## Five-Step Mainline From Here

1. Code readiness gate

   Confirm which claims are executable in the current code. Run focused tests first.

2. Evidence and retention map

   Identify which stores/logs are paper evidence and which are disposable build or scratch output.

3. Complete missing experiments

   Prioritize latest-code C1 refresh, sustained feedback, and schema/snapshot stress evidence.

4. Freeze experiment evidence

   Create stable log directories, command records, binary/config stamps, and table inputs.

5. Return to paper only after evidence freeze

   Paper writing should summarize frozen evidence instead of driving the work.

## First Files To Read In The New Window

Read these first, in this order:

1. `NEXT_WINDOW_HANDOFF_20260605.md`
2. `experiment-completion-plan.md`
3. `workspace-cleanup-retention-20260605.md`
4. `CURRENT_MATERIALS_INDEX_20260605.md`
5. `docs/archive/stage-md-20260605/seml0-stage-p7-05-edge-type-mismatch-diagnosis-and-fix-20260604.md`
6. `docs/archive/stage-md-20260605/seml0-stage-p6-61-final-implementation-and-submission-roadmap-20260604.md`
7. `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-c1-ablation-command-bundle-audit-20260605.md`
8. `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-sustained-feedback-compaction-plan-20260605.md`
9. `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-mixed-schema-delta-stress-inventory-20260605.md`

## Do Not Do In The Next Window

- Do not continue paper prose polishing first.
- Do not generate TeX or PDF.
- Do not delete stores or logs without a retention decision.
- Do not overwrite old experiment stores.
- Do not claim SF30/SF100 or sustained feedback as complete until fresh evidence exists.

## Recommended First Action In The New Window

Start with:

```text
Read NEXT_WINDOW_HANDOFF_20260605.md and experiment-completion-plan.md.
Then run the code readiness gate only.
Do not write the paper yet.
```
