# SemL0 Evaluation Section Draft MD

Date: 2026-06-05

## Status

```text
evaluation_section_draft_ready=yes
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

This MD document is the paper-section draft for the SemL0 evaluation. It turns
the current C1, P3, P2, P4, and write-cost evidence into a coherent RQ-driven
evaluation plan. It is intended as a source draft for future paper editing,
before any TeX rewrite.

This stage does not edit TeX, generate PDF, run experiments, rerun benchmarks,
delete stores, approve cleanup, choose a venue, or mark final submission ready.

## Source Anchors

| Anchor | Role |
|---|---|
| `paper/introduction-section-draft-md.md` | five evaluation questions and contribution boundary |
| `paper/query-semantic-physical-design-section-draft-md.md` | C1 interpretation and SF1 boundary |
| `paper/query-semantic-physical-design-prose-polish-md.md` | C1 mechanism and C1-to-P3 bridge |
| `paper/feedback-compaction-section-draft-md.md` | P3 interpretation and sustained-run boundary |
| `paper/feedback-compaction-prose-polish-md.md` | P3 maintenance-layer and P3-to-P2/P4 bridge |
| `paper/schema-evolution-section-draft-md.md` | P2/P4 schema/snapshot interpretation |
| `paper/schema-evolution-prose-polish-md.md` | schema/snapshot safety-layer and old-store no-invalidation wording |
| `paper/section-level-prose-polish-map-md.md` | evaluation prose order and claim boundaries |
| `paper/reviewer-response-draft-md.md` | reviewer-facing evaluation boundaries |
| `paper/five-step-total-control-summary.md` | current code/evidence/open-work state |
| `remote-logs/p7-07-post-fix-regression-table-regen-20260604/p5-paper-tables-regenerated/` | latest regenerated MD views for Tables 1--5 |
| `remote-logs/p7-07-post-fix-regression-table-regen-20260604/p6-write-cost-tables-regenerated/` | regenerated MD views for Tables 6--7 |
| `remote-logs/p5-normalized-evaluation-20260604/` | normalized CSV package and older extracted rows |
| `paper/c1-ablation-command-bundle-audit.md` | C1 command-bundle and after-fix evidence boundary |
| `paper/mixed-schema-delta-stress-inventory.md` | P2/P4 stress coverage and limitations |
| `paper/sustained-feedback-experiment-approval-packet.md` | unapproved sustained feedback run boundary |

## Intended Paper Placement

This draft corresponds to:

```text
Section 7. Evaluation
```

It should appear after the design sections, because the evaluation is organized
around the same contribution line:

```text
C1 query-semantic physical design
-> benefit-scored materialization
-> P3 feedback-driven semantic compaction
-> P2/P4 schema/snapshot safety
-> P5 source-ready evidence package
```

## Evaluation Questions

The evaluation should answer five questions:

```text
RQ1: Does query-semantic layout reduce read amplification?
RQ2: Does benefit scoring avoid the cost of full semantic materialization?
RQ3: Does feedback compaction adapt to a workload shift?
RQ4: Do schema evolution and dynamic deltas preserve correctness?
RQ5: What write, storage, and rewrite costs are visible in the current evidence?
```

The evaluation should not be written as a list of logs. Each RQ should name the
claim, the table or artifact that supports it, and the boundary that keeps the
claim defensible.

The polished RQ-to-system-chain map is:

| RQ | System role | Evidence role | Boundary |
|---|---|---|---|
| RQ1 | C1 semantic pruning surface | Table 1 | latest-code SF1 only |
| RQ2 | benefit-scored materialization policy | Tables 2 and 5 | not a global optimizer proof |
| RQ3 | P3 adaptive maintenance for C1 | Tables 3 and 7 | controlled workload-shift only |
| RQ4 | P2/P4 schema/snapshot safety layer | Tables 4 and 10 | targeted correctness, not full migration |
| RQ5 | P5 visible cost and artifact accounting | Tables 6 and 7 | proxy accounting, not production write-stall proof |

## Draft Section Text

### 7. Evaluation

The evaluation asks whether query semantics can guide LSM graph physical design
without sacrificing conservative correctness. It follows the system chain rather
than the historical order in which logs were produced. RQ1 and RQ2 evaluate the
C1 pruning surface and benefit-scored materialization policy. RQ3 evaluates P3
as adaptive maintenance for that surface. RQ4 evaluates P2/P4 as the
schema/snapshot safety layer that decides when metadata remains safe to use. RQ5
records the visible cost and artifact accounting that bound the current
source-ready package.

The current package is source-ready and venue-neutral: it contains latest-code
SF1 evidence, controlled feedback evidence, targeted schema/snapshot correctness
evidence, and write/rewrite cost proxies. It is not a final submission package,
and it does not include final latest-code SF30/SF100 refreshes.

### RQ1: Does Query-Semantic Layout Reduce Read Amplification?

RQ1 evaluates the core C1 claim. Table 1 compares schema-only layout, full
semantic materialization, and the current benefit-scored policy on latest-code
SF1 rows.

This RQ should be tied to the C1 mechanism: query-semantic signatures and exact
segment summaries create the semantic pruning surface. Table 1 measures whether
that surface reduces read amplification under the current SF1 evidence package.

Current Table 1 interpretation:

```text
schema baseline:
  core_read_bytes=303,689,504
  alltypes_read_bytes=410,942,896

full semantic:
  core_reduction=99.35%
  alltypes_reduction=94.88%
  correctness=pass

benefit scored:
  core_reduction=72.86%
  alltypes_reduction=58.89%
  correctness=pass
```

The RQ1 answer is:

```text
Yes for the current SF1 evidence package: exposing graph query signatures to L0
segment metadata materially reduces read amplification.
```

Boundary:

```text
No final latest-code SF30/SF100 performance claim.
```

Historical SF30/SF100 candidate numbers may remain motivation or future-scope
context, but they should not be used as the final evaluation result until an
approved refresh exists.

### RQ2: Does Benefit Scoring Avoid Full Materialization Cost?

RQ2 evaluates whether SemL0 can avoid the cost of materializing every semantic
combination. Table 2 compares full semantic, default budgeted, and benefit
scored policies. Table 5 and the C1 command-bundle audit preserve the after-fix
threshold context.

Current Table 2 interpretation:

```text
full semantic:
  l0_files=3,727
  manifest_bytes=2,033,009
  core_reduction=99.35%
  alltypes_reduction=94.88%

default budgeted:
  l0_files=2,059
  manifest_bytes=1,126,473
  core_reduction=0.00%
  alltypes_reduction=0.00%

benefit scored:
  l0_files=3,251
  manifest_bytes=1,774,004
  core_reduction=72.86%
  alltypes_reduction=58.89%
```

The RQ2 answer is:

```text
Benefit scoring is needed because a fanout-only budget can preserve low file
count while losing read benefit, whereas indiscriminate full semantic layout has
higher fanout.
```

The after-fix threshold rows show the sensitivity more sharply: fine threshold
variants can achieve strong reductions with high fanout, while micro threshold
variants remain correct but provide negligible read benefit. The benefit-scored
row is the current balanced policy, not a proof of global optimality.

This RQ should be read as policy evidence for maintaining the C1 pruning
surface. It explains why SemL0 does not materialize every semantic combination
and why a fanout-only budget can lose the read benefit.

Boundary:

```text
No global optimizer optimality claim.
No claim that benefit scoring is universally best for every workload.
```

### RQ3: Does Feedback Compaction Adapt To A Workload Shift?

RQ3 evaluates P3. Table 3 is the adaptation table; Table 7 is the rewrite-cost
proxy table. They should be discussed together but not conflated.

This RQ should be tied to the P3 prose polish: C1 defines the semantic pruning
surface, and P3 decides how that surface is maintained when workload feedback
changes. Table 3 measures adaptation; Table 7 records rewrite-cost proxy fields.

Current Table 3 interpretation:

```text
feedback phase A:
  before_l0=9
  after_l0=0
  selected_l0=3
  output_segments=1

feedback phase B:
  before_l0=9
  after_l0=0
  selected_l0=3
  output_segments=1
  selected_ranges_changed=True

no-feedback phase A/B:
  before_l0=9
  after_l0=3
  selected_l0=0
  output_segments=0
```

The RQ3 answer is:

```text
Controlled workload-shift evidence shows that feedback can move compaction
priority to the current hot semantic range and reduce hot-phase candidate L0
segments.
```

Boundary:

```text
No production write-stall characterization.
No long-running production adaptation claim.
No sustained run result yet.
```

The sustained feedback experiment is currently approval-gated:

```text
experiment_approval_status=not_approved
safe_to_run_now=no
experiments_run=0
```

### RQ4: Do Schema Evolution And Dynamic Deltas Preserve Correctness?

RQ4 evaluates P2/P4. Table 4 summarizes targeted correctness coverage, and Table
10 separates additive schema evolution claims from future migration work.

This RQ should answer the safety question left by the design sections: P3 may
change which semantic ranges are maintained first, but schema and snapshot
interpretation still decide which metadata is safe to use.

Current Table 4 interpretation:

```text
schema:
  cases=5
  passed=5

snapshot/delta:
  cases=4
  passed=4

schema+snapshot:
  cases=3
  passed=3
```

The normalized correctness rows and mixed schema-delta inventory cover:

```text
old segments remain readable across schema_epoch
new edge labels prune exact old segments but read mixed segments
required-property predicates prune exact-absent records
legacy missing property summaries stay conservative
tombstones and snapshots preserve visible history
schema epoch plus snapshot-visible deltas survive compaction and reopen
property schema epoch composes with snapshot/delta behavior
```

The RQ4 answer is:

```text
Targeted regressions support the no-false-negative pruning invariant under
additive schema evolution and dynamic deltas.
```

This includes the direct schema-change answer:

```text
Schema changes do not automatically invalidate old storage.
```

Old segments remain readable under their stored `schema_epoch`; unknown, mixed,
legacy, or tombstone-sensitive metadata is read conservatively; lazy compaction
is a performance repair rather than a correctness prerequisite.

Boundary:

```text
No exhaustive dynamic-graph correctness proof.
No full rename/drop/type-change physical migration claim.
No range/string/compound predicate or SQL null semantics claim.
```

### RQ5: What Write, Storage, And Rewrite Costs Are Visible?

RQ5 keeps the cost discussion bounded to the metrics we actually have. Table 6
records SF1 import/store proxies. Table 7 records feedback compaction rewrite
proxies.

This RQ is the P5 evidence-package boundary inside the evaluation. It records
what the current artifact can account for without converting proxy metrics into
production write-path claims.

Current Table 6 interpretation:

```text
schema:
  import_s=86.58
  store_bytes=3,794,892,645
  store_overhead=0.00%
  l0_files=2,059

full semantic:
  import_s=83.06
  store_bytes=3,802,298,981
  store_overhead=0.20%
  l0_files=3,727

benefit scored:
  import_s=83.25
  store_bytes=3,802,003,921
  store_overhead=0.19%
  l0_files=3,251
```

Current Table 7 interpretation:

```text
selected_l0_segments=3
output_segments=1
estimated_rewrite_bytes=552
compaction_input_bytes=552
compaction_output_bytes=248
io_write_bytes_compaction=248
read_bytes_before=288
read_bytes_after=96
```

The RQ5 answer is:

```text
The current evidence reports storage overhead, file-count fanout, import-time
proxies, and deterministic rewrite accounting for feedback compaction.
```

Boundary:

```text
No production write-stall proof.
No negligible-overhead claim.
No full workload-concurrency characterization.
```

### Evaluation Flow

The section should present tables in this order:

```text
RQ1: Table 1
RQ2: Table 2 and Table 5
RQ3: Table 3 and Table 7
RQ4: Table 4 and Table 10
RQ5: Table 6 and Table 7
```

This ordering keeps the paper narrative coherent:

```text
read amplification reduction
selective materialization policy
adaptive maintenance
correctness under schema/snapshot/delta changes
visible cost accounting
```

The same order should be described in prose as:

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

### Evaluation Boundary

Safe evaluation wording:

```text
The current evaluation supports latest-code SF1 query-semantic layout claims,
controlled feedback adaptation, targeted schema/snapshot correctness, and
write/rewrite cost proxy accounting.
```

Unsafe evaluation wording:

```text
SemL0 is final-submission ready.
SemL0 proves final latest-code SF30/SF100 performance.
SemL0 proves production write-stall safety.
SemL0 implements a full schema-migration engine.
SemL0 proves exhaustive dynamic-graph correctness.
```

## Evidence Map

| RQ | Primary tables | Main anchors | Boundary |
|---|---|---|---|
| RQ1 | Table 1 | latest-code SF1 main result; C1 section draft | no final SF30/SF100 claim |
| RQ2 | Tables 2 and 5 | C1 after-fix ablation TSV; C1 command-bundle audit | no global optimality claim |
| RQ3 | Tables 3 and 7 | P7.2 workload-shift summary; P3 bench source; feedback section draft | sustained run not approved |
| RQ4 | Tables 4 and 10 | mixed schema-delta inventory; schema section draft; correctness CSV | targeted correctness only |
| RQ5 | Tables 6 and 7 | SF1 import/store proxies; feedback rewrite-cost CSV | no production write-stall proof |

## Claims To Avoid In This Section

Do not write:

```text
final submission ready
final latest-code SF30/SF100 performance
production write-stall safety
negligible write overhead
full schema migration
exhaustive dynamic-graph correctness
sustained feedback experiment completed
store cleanup completed
```

Use this bounded closing:

```text
The evaluation shows that SemL0's current source-ready package supports the
paper's SF1, controlled feedback, targeted correctness, and cost-proxy claims,
while leaving final venue/PDF readiness, SF30/SF100 refreshes, sustained
feedback, production write-stall characterization, and broad migration as
explicit open work.
```

## Next Use

If TeX editing is later approved, translate this MD into the Evaluation section
without broadening the claim boundary. If new experiments are approved first,
create separate execution stages and update this draft only after each new run
has logs, tables, summaries, and a claim-boundary decision.
