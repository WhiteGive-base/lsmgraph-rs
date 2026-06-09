# SemL0 Experiment Completion Plan

Date: 2026-06-05

## Goal

Finish the implementation and experiment closure needed before returning to paper
writing.

The target is not "more logs". The target is:

```text
each paper claim has current-code support, a reproducible command, and a retained evidence directory
```

## Current Status

Code has been improved, but the experiment closure is incomplete.

Current usable evidence:

- latest-code SF1 C1 pruning and ablation evidence;
- P7.5 typed-read pruning bug diagnosis and fix;
- targeted schema/snapshot/tombstone/property correctness tests;
- feedback compaction targeted tests and microbench scaffolding;
- write/rewrite cost proxy material.

Missing or not final:

- latest-code SF30/SF100 performance refresh;
- sustained feedback workload-shift run;
- full code readiness gate after cleanup;
- final evidence retention map;
- one-command reproduction bundle;
- final experiment table freeze.

## Code Readiness Gate

Before running large experiments, verify the current code can support the intended
claims.

Required checks:

| Gate | Purpose | Status |
|---|---|---|
| cargo formatting | ensure source is mechanically clean | not run in this handoff |
| focused engine tests | schema/snapshot/tombstone/property/feedback correctness | required |
| C1 typed-read regression | ensure P7.5 false negative stays fixed | required |
| p3 feedback bench smoke | ensure benchmark binary works before long run | required |
| command/log stamping | preserve binary/config/path metadata | required |

The next window should not start SF30/SF100 before this gate passes.

## Experiment Matrix

| ID | Experiment | Why It Matters | Output Rule | Current Decision |
|---|---|---|---|---|
| E0 | code readiness gate | prevents running unusable experiments | new log dir | must run first |
| E1 | targeted correctness suite | supports schema/snapshot/tombstone safety | new log dir | required |
| E2 | latest-code SF1 C1 refresh | confirms main C1 claim on current code | new log dir | required |
| E3 | C1 ablation table refresh | separates semantic index, degree policy, threshold policy | new log dir | required |
| E4 | sustained feedback workload shift | supports P3 maintenance/adaptation claim | new log dir, no old overwrite | required if P3 stays a main contribution |
| E5 | schema delta stress | strengthens additive schema evolution claim | new log dir | required for schema section |
| E6 | SF30 refresh | scale evidence beyond SF1 | new log dir | recommended |
| E7 | SF100 refresh | large-scale evidence | new log dir | optional unless claiming large-scale |
| E8 | write/rewrite cost proxy refresh | bounds maintenance cost | new log dir | recommended |

## Claim-To-Evidence Map

| Claim | Minimum Evidence Needed | Current Risk |
|---|---|---|
| C1 semantic pruning reduces L0 read amplification | latest-code SF1 plus ablation | mostly available, refresh recommended |
| C1 does not break typed reads | P7.5 regression plus targeted checks | available but should rerun |
| additive schema change does not invalidate old stores | schema epoch tests plus mixed delta stress | partial, needs final run |
| property predicates are safe under absent/default/alias/encoding epoch | targeted property tests | available but prototype-scoped |
| feedback compaction adapts to workload shift | sustained trend logs | not complete |
| snapshot/tombstone correctness survives compaction/reopen | targeted tests | available but should rerun |
| write cost is bounded | proxy accounting | partial, not production write-stall |

## Experiment Logging Rules

Every new run must use a fresh directory. Do not overwrite existing stores or logs.

Naming convention:

```text
remote-logs/e0-code-readiness-YYYYMMDD-HHMMSS
remote-logs/e1-targeted-correctness-YYYYMMDD-HHMMSS
remote-logs/e2-c1-sf1-refresh-YYYYMMDD-HHMMSS
remote-logs/e4-sustained-feedback-YYYYMMDD-HHMMSS
```

Each evidence directory should contain:

- command used;
- git/source status if available;
- binary path or build profile;
- config file or CLI flags;
- start/end timestamps;
- stdout/stderr logs;
- summary table;
- pass/fail marker.

## Existing Command Helpers

Existing run scripts were moved out of the repository root:

```text
scripts/experiments/active/
```

Existing analysis scripts were moved to:

```text
scripts/analysis/current/
```

Use these as references for E1/E2/E3/E4, but do not treat an old script name as proof
that the corresponding experiment is complete. Each new run still needs a fresh log
directory and a fresh pass/fail summary.

## Workspace Organization Rules

Do not place new scripts, logs, paper drafts, or scratch files directly in the
repository root unless they are one of the approved entry documents.

Root may contain:

```text
Cargo.toml
Cargo.lock
README.md
NEXT_WINDOW_HANDOFF_20260605.md
experiment-completion-plan.md
CURRENT_MATERIALS_INDEX_20260605.md
workspace-cleanup-retention-20260605.md
```

Experiment scripts must go under:

```text
scripts/experiments/active/
scripts/experiments/optional/
scripts/experiments/legacy/
```

Analysis and table-generation scripts must go under:

```text
scripts/analysis/current/
```

Maintenance scripts must go under:

```text
scripts/maintenance/current/
scripts/maintenance/dangerous-archive/
```

Experiment logs must go under:

```text
remote-logs/<experiment-id>-<timestamp>/
```

Paper/reference material must go under:

```text
paper/
paper/archive/
```

Stage notes, scratch outputs, and historical working notes must go under:

```text
docs/archive/
docs/evidence-summaries-20260605/
```

Stores must stay under:

```text
store/
```

Never create a new store by overwriting an old one. Use a fresh, named directory for
each experiment.

Before creating a new file, classify it as one of:

| Class | Location |
|---|---|
| experiment command | `scripts/experiments/` |
| analysis/table script | `scripts/analysis/current/` |
| maintenance helper | `scripts/maintenance/current/` |
| destructive cleanup helper | `scripts/maintenance/dangerous-archive/` |
| experiment log | `remote-logs/<id>/` |
| experiment store | `store/<id>/` |
| material index or handoff | repository root |
| scratch/history note | `docs/archive/` |
| paper/reference source | `paper/` or `paper/archive/` |

## Immediate Next Steps

1. Run E0 code readiness gate.
2. Run E1 targeted correctness suite.
3. Run E2/E3 latest-code C1 SF1 refresh and ablation.
4. Decide whether P3 feedback remains a main contribution.
5. If yes, run E4 sustained feedback workload-shift experiment.
6. Run E5 schema delta stress.
7. Only then decide whether SF30/SF100 are needed for the paper target.

## Stop Conditions

Stop and fix code before continuing if any of these happen:

- typed read mismatch appears again;
- schema epoch test fails after compaction or reopen;
- tombstone segment is pruned when it may affect snapshot correctness;
- feedback compaction increases candidate segments without explanation;
- experiment logs cannot identify command, config, and output store.
