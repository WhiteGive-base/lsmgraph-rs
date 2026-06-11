# SemL0 Feedback Compaction Section Draft MD

Date: 2026-06-05

## Status

```text
feedback_compaction_section_draft_ready=yes
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

This MD document is the paper-section draft for P3: feedback-driven semantic
compaction. It turns the current workload-shift evidence, rewrite-cost proxy,
and sustained-run approval boundary into prose that can later be translated into
the paper.

This stage does not edit TeX, generate PDF, run experiments, rerun benchmarks,
delete stores, approve cleanup, rerun feedback benchmarks, choose a venue, or
mark final submission ready.

## Source Anchors

| Anchor | Role |
|---|---|
| `paper/introduction-section-draft-md.md` | paper thesis and contribution boundary |
| `paper/query-semantic-physical-design-section-draft-md.md` | C1 layout mechanism that P3 adapts |
| `paper/query-semantic-physical-design-prose-polish-md.md` | C1-to-P3 bridge and semantic pruning surface wording |
| `paper/section-level-prose-polish-map-md.md` | rule that P3 should connect adaptation to C1 and preserve the sustained-run boundary |
| `paper/reviewer-response-draft-md.md` | feedback scope and write-cost response boundaries |
| `paper/final-paper-outline-md.md` | section placement and paragraph plan |
| `paper/paper-narrative-spine.md` | P3 story inside the full paper spine |
| `paper/sustained-feedback-compaction-plan.md` | current P3 plan and paper boundary |
| `paper/sustained-feedback-experiment-approval-packet.md` | unapproved sustained-run boundary |
| `remote-logs/p7-02-feedback-workload-shift-20260604/summary.txt` | controlled workload-shift evidence |
| `remote-logs/p7-07-post-fix-regression-table-regen-20260604/p5-paper-tables-regenerated/table3-feedback-adaptation.md` | Table 3 adaptation/no-feedback view |
| `remote-logs/p7-07-post-fix-regression-table-regen-20260604/p6-write-cost-tables-regenerated/table7-feedback-compaction-cost.md` | Table 7 rewrite-cost proxy view |
| `remote-logs/p6-5-write-cost-extraction-20260604/write_cost_compaction_microbench.csv` | normalized rewrite-cost fields |
| `src/bin/p3_feedback_bench.rs` | deterministic workload-shift microbench |
| `tests/engine_tests.rs` | hot-partition and workload-shift regression anchors |

## Intended Paper Placement

This draft corresponds to:

```text
Section 5. Feedback-Driven Semantic Compaction
```

It should appear after query-semantic physical design and before
schema/snapshot safety. The section answers:

```text
Can semantic layout adapt when workload hot spots shift?
```

It is the maintenance layer for the C1 pruning surface:

```text
C1 defines the semantic pruning surface; P3 decides how that surface is
maintained when workload feedback changes.
```

## Draft Section Text

### 5. Feedback-Driven Semantic Compaction

Query-semantic layout is useful because C1 makes a safe semantic pruning surface
visible to the read path. That surface still needs maintenance. A static layout
can become misaligned with the current workload: a semantic partition that was
valuable during one query phase may be less important after the workload moves
to a different source range, edge type, or property requirement. SemL0 therefore
adds a feedback loop on top of C1's semantic layout. Runtime reads expose which
query-semantic ranges are hot, and compaction can selectively rewrite those
ranges into lower-read-amplification layouts.

This section should be read as an adaptive maintenance mechanism, not as a
production scheduler proof. Its local flow is:

```text
C1 pruning surface -> runtime feedback counters -> hot semantic range selection -> bounded rewrite -> controlled workload-shift evidence -> sustained-run boundary
```

In this section, runtime feedback means storage-level counters collected from
the read path, not a learned query optimizer or a full production scheduling
model.

The feedback loop records storage-facing signals rather than full query plans:

```text
query count
candidate L0 segments
range bucket
edge type
estimated rewrite bytes
selected L0 segments
read bytes before and after compaction
compaction input/output bytes
```

The core decision is benefit per rewrite cost. A hot range is useful only if
rewriting it is likely to save read work under the current query pattern. SemL0
therefore scores candidate L0 partitions and selects a bounded range for
semantic compaction. The compaction changes future physical layout; it does not
change query semantics. If metadata is uncertain, the read path remains
conservative.

This is the same exact-proof contract used by C1. Feedback may decide which
semantic range deserves rewrite work, but it does not make mixed, unknown,
legacy, or tombstone-sensitive metadata safe to skip. Adaptation changes
maintenance priority, not the meaning of a query result.

### Workload-Shift Evidence

The current P3 evidence is a controlled workload-shift microbenchmark. It has
two phases. Phase A repeatedly queries one hot source range. After feedback
metrics are collected, SemL0 selects the hot L0 range and compacts it. Phase B
then resets metrics and shifts the hot query source to a different range. A
successful adaptation requires the selected range to move with the workload.

The current summary records:

```text
test_hot_partition PASS
test_workload_shift PASS
check_feedback_bench PASS
bench_workload_shift PASS
phase_a_before_l0=9
phase_a_after_l0=0
phase_b_before_l0=9
phase_b_after_l0=0
selected_ranges_changed=True
no_feedback_phase_a_after_l0=3
no_feedback_phase_b_after_l0=3
phase_a_selected_l0=3
phase_b_selected_l0=3
phase_a_output_segments=1
phase_b_output_segments=1
```

Table 3 should be interpreted as the adaptation result. With feedback, each
phase reduces the hot query's L0 candidate count from 9 to 0 after compaction.
The selected range changes in phase B, showing that priority follows the new
hot range after the metrics reset. Without feedback, the same query pattern
keeps nonzero L0 candidates after the phase, so the result is not simply an
artifact of repeated reads.

### Rewrite-Cost Proxy

The feedback story must include cost. The current evidence does not claim
production write-stall safety or negligible overhead. Instead, Table 7 and the
normalized rewrite-cost CSV provide a deterministic rewrite-cost proxy for the
microbenchmark.

The current rows record:

```text
selected_l0_segments=3
output_segments=1
estimated_rewrite_bytes=552
compaction_input_bytes=552
compaction_output_bytes=248
io_write_bytes_compaction=248
read_bytes_before=288
read_bytes_after=96
read_bytes_saved_per_compaction_input_mib=192.000
```

These fields let the paper pair read-side improvement with the rewrite work
needed to produce it. They should be described as accounting evidence, not as a
production latency or write-stall characterization. The microbenchmark records
compaction input/output bytes, IO write bytes, and latency proxies; it does not
model production concurrency, background scheduling, or long-running write
pressure.

### No-Feedback Contrast

The no-feedback path is important because it distinguishes adaptation from a
fixed layout side effect. In the controlled summary, no-feedback phase A and
phase B keep nonzero L0 candidates:

```text
no_feedback_phase_a_after_l0=3
no_feedback_phase_b_after_l0=3
```

The paper should use this contrast narrowly. It supports the claim that feedback
compaction reduces candidate L0 reads for the controlled hot ranges. It does not
prove that all future workloads will converge quickly, that the score is
globally optimal, or that production compaction scheduling is solved.

This contrast is also the bridge back to the system chain. C1 alone defines what
could be pruned safely; P3 shows that the system can spend maintenance work on
the currently hot part of that pruning surface. The next correctness sections
explain why schema epochs, tombstones, and snapshot visibility still bound when
that surface may be used.

### Sustained Run Boundary

A longer sustained feedback experiment has been specified but not approved or
executed. The approval packet fixes a fresh log directory, fresh benchmark
stores, `--reset-store` safety rules, and acceptance criteria. The current state
is:

```text
experiment_approval_status=not_approved
safe_to_run_now=no
experiments_run=0
stores_deleted=0
```

Therefore, this section should use the current wording:

```text
controlled workload-shift microbenchmarks show feedback can adapt semantic
compaction priority.
```

It should not say:

```text
feedback compaction has negligible overhead
feedback compaction is production write-stall safe
feedback compaction is globally optimal
feedback compaction was validated on long-running SF30/SF100 workloads
```

### Section Boundary

Safe P3 wording:

```text
SemL0 records runtime read feedback and uses it to select hot semantic L0 ranges
for compaction. In controlled workload-shift microbenchmarks, feedback reduces
candidate L0 segments for each hot phase and moves the selected range after a
metrics reset, while rewrite-cost proxy fields record the bytes and latency
accounting for the rewrite.
```

Unsafe P3 wording:

```text
SemL0 proves production adaptive compaction.
SemL0 has negligible write overhead.
SemL0 always finds the optimal compaction target.
SemL0 validates long-running SF30/SF100 feedback adaptation.
SemL0 can run the sustained experiment without explicit store approval.
```

## Evidence Map

| Claim | Evidence anchor |
|---|---|
| feedback picks a hot L0 partition | `tests/engine_tests.rs::feedback_compaction_picks_hot_l0_partition_and_reduces_candidates` |
| feedback priority shifts after reset | `tests/engine_tests.rs::feedback_compaction_priority_shifts_after_metrics_reset` |
| deterministic workload-shift bench exists | `src/bin/p3_feedback_bench.rs` |
| current workload-shift run passed | `remote-logs/p7-02-feedback-workload-shift-20260604/summary.txt` |
| feedback candidate count reduces from 9 to 0 | Table 3 and workload-shift summary |
| no-feedback keeps nonzero L0 candidates | Table 3 and workload-shift summary |
| selected ranges change | workload-shift summary and `p3_feedback_bench.rs` |
| rewrite-cost proxy fields exist | Table 7 and `write_cost_compaction_microbench.csv` |
| sustained run is not approved | `paper/sustained-feedback-experiment-approval-packet.md` |

## Claims To Avoid In This Section

Do not write:

```text
production write-stall characterization
long-running production adaptation
global optimizer optimality
negligible write overhead
final SF30/SF100 feedback validation
sustained run completed
```

Use this bounded closing:

```text
P3 turns C1 from a static semantic layout into a controlled workload-adaptive
maintenance loop, while leaving production-scale scheduling and long-running
adaptation as future evidence.
```

The intended bridge sentence is:

```text
P3 changes which semantic ranges are maintained first; P2 and P4 explain why
schema and snapshot interpretation still decide which metadata is safe to use.
```

## Next Use

If TeX editing is later approved, translate this MD into the feedback
compaction section without broadening the claim boundary. If the sustained run
is approved first, create a new execution stage, store logs in the approved
fresh directory, and update this draft only after a new summary and claim
boundary decision exist.
