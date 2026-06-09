# SemL0 Current Materials Index

Date: 2026-06-05

## Current Working Rule

Linux is the primary workspace:

```text
/data/WorkSpace/lsmgraph-rs
```

Local Windows workspace is a working mirror:

```text
E:\文档\DGS项目复现\lsmgraph-rs
```

For the next phase, do not start from paper polishing. Start from code readiness and
experiment closure.

## Linux State Checked

Checked on 2026-06-05 22:52 CST.

Important Linux directories:

| Path | Status | Decision |
|---|---|---|
| `src/` | implementation | keep |
| `tests/` | correctness tests | keep |
| `examples/` | runnable examples | keep |
| `store/` | about 130 GB, base graphs and experiment stores | keep, do not delete blindly |
| `remote-logs/` | about 297 MB, evidence logs and summaries | keep |
| `paper/` | existing paper package | keep, not active focus now |
| `figures/` | paper/evidence assets | keep |
| `target-codex-*` | 115 generated build caches | deleted on Linux |
| `target/` | standard Cargo build cache, about 3.9 GB before cleanup | deleted on Linux |

Linux disk after target cleanup:

```text
/data size: 2.0T
used: about 1.4T
available: about 517G
```

## Root Directory Policy

The repository root should contain only:

- source/build files such as `Cargo.toml`, `Cargo.lock`, `README.md`;
- entry handoff documents;
- current experiment plan;
- cleanup/material index documents.

Stage-by-stage md files should not stay in the root. They are useful history, but they
make the next window hard to operate.

## Entry Documents To Keep In Root

| File | Purpose |
|---|---|
| `NEXT_WINDOW_HANDOFF_20260605.md` | start here in a new window |
| `experiment-completion-plan.md` | main next execution plan |
| `CURRENT_MATERIALS_INDEX_20260605.md` | this material inventory |
| `workspace-cleanup-retention-20260605.md` | what was cleaned and what must be retained |
| `README.md` | project overview |

## Archived Material Layout

Experiment run scripts:

```text
scripts/experiments/active/
scripts/experiments/optional/
scripts/experiments/legacy/
```

Analysis/table scripts:

```text
scripts/analysis/current/
```

Maintenance helper scripts:

```text
scripts/maintenance/current/
scripts/maintenance/dangerous-archive/
```

Stage documents:

```text
docs/archive/stage-md-20260605/
```

Historical experiment summaries and research notes:

```text
docs/evidence-summaries-20260605/
```

Remote logs remain in place:

```text
remote-logs/
```

Remote log retention details:

```text
docs/current/REMOTE_LOGS_RETENTION_20260605.md
```

Current decision:

```text
do not delete remote-logs/
remote-logs is about 297 MB on Linux
many p5-* entries are document-check logs, but p1/qslsm/p7 entries are evidence
```

Stores remain in place:

```text
store/
```

Cleanup counts after the 2026-06-05 consolidation:

| Workspace | Root md files | Stage archive md | Evidence summary md | target* dirs |
|---|---:|---:|---:|---:|
| Linux primary | 5 | 401 | 19 | 0 |
| Local mirror | 5 | 400 | 24 | 0 |

The Linux counts are authoritative for the next phase.

Root script/source-candidate cleanup after the second consolidation:

| Class | New Location | Decision |
|---|---|---|
| `run-*.sh` | `scripts/experiments/` | keep as experiment command bundles |
| active `run-*.sh` | `scripts/experiments/active/` | keep as next experiment command references |
| optional/legacy `run-*.sh` | `scripts/experiments/optional/`, `scripts/experiments/legacy/` | keep for provenance or later scale work |
| `render-*.py`, `summarize-*.py` | `scripts/analysis/current/` | keep as table/log summarizers |
| non-destructive maintenance helpers | `scripts/maintenance/current/` | keep, not root entry points |
| destructive cleanup helper | `scripts/maintenance/dangerous-archive/` | keep but do not run casually |
| root `.bib` and `.tex` candidates | `paper/archive/root-candidates-20260605/` | keep as paper-source history, not active focus |
| `tmp-sync-files`, `tmp-sync-files-p15` | deleted on Linux | old sync scratch |
| root scratch files such as `output.json`, empty `graph.rs`, and shell-output artifacts | `docs/archive/root-scratch-20260605/` | archived instead of deleted |

Docs directory cleanup after the 2026-06-05 consolidation:

| Path | Purpose |
|---|---|
| `docs/README.md` | docs layout explanation |
| `docs/current/` | current operational notes |
| `docs/evidence-summaries-20260605/` | historical evidence summaries and research notes |
| `docs/archive/stage-md-20260605/` | long-session stage notes |

Removed from docs:

```text
docs/archive/root-scratch-20260605/
```

## Important Archived Files

After root cleanup, read these through the archive path:

| Topic | File |
|---|---|
| P7.5 C1 typed-read bug fix | `docs/archive/stage-md-20260605/seml0-stage-p7-05-edge-type-mismatch-diagnosis-and-fix-20260604.md` |
| implementation/submission boundary | `docs/archive/stage-md-20260605/seml0-stage-p6-61-final-implementation-and-submission-roadmap-20260604.md` |
| C1 ablation command audit | `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-c1-ablation-command-bundle-audit-20260605.md` |
| sustained feedback plan | `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-sustained-feedback-compaction-plan-20260605.md` |
| schema delta stress inventory | `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-mixed-schema-delta-stress-inventory-20260605.md` |

## Current Technical Position

The code has real implementation work, but the experiment closure is not complete.

Current code can support a scoped prototype claim:

- semantic L0 pruning with a fixed typed-read correctness issue;
- additive schema epoch handling;
- property metadata and fixed-width property value prototype paths;
- targeted snapshot/tombstone/schema correctness tests;
- feedback compaction targeted tests and benchmark scaffolding.

Current code should not yet be used for broad claims:

- complete schema migration engine;
- final SF30/SF100 latest-code performance;
- production write-stall safety;
- long sustained feedback stability;
- final paper submission readiness.

## Next Operational Goal

Run the experiment closure in this order:

1. E0 code readiness gate.
2. E1 targeted correctness suite.
3. E2/E3 latest-code SF1 C1 refresh and ablation.
4. E4 sustained feedback workload shift if P3 remains a main contribution.
5. E5 schema delta stress.
6. Decide whether SF30/SF100 are needed for the target venue.
