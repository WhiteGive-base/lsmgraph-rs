# Clean Kernel Inventory

> Branch: `codex/k4-clean-kernel`
>
> Worktree: `/data/WorkSpace/lsmgraph-rs-clean-kernel`
>
> Base commit: `48d44d1 W14 compare: result-digest correctness path + SF1 gate`

## 中文边界摘要

这个清单用于说明干净分支和原研究分支的差距。当前分支只保留 DB 内核、
测试和最小产品入口，不保留论文材料、实验脚本、baseline trace、
历史计划文档和一次性 benchmark binary。

已经带入的内核改动是 L0/semantic pruning 方向的稳定代码：
pruning decision/reason、pruning reason metrics、以及目标点 label 查询签名。
没有带入 W7/W14 等实验 runner，也没有实现 K4 完整链路。

## Purpose

This branch is a clean DB-kernel branch. It removes paper material, experiment
logs, baseline runners, historical plans, and one-off research binaries from the
tracked tree.

It does not implement K4. It prepares a smaller kernel base for future K4 work.

## Kept

| Path | Reason |
|---|---|
| `src/` | DB engine, storage, query, server, IO, schema, metrics |
| `tests/` | Kernel regression coverage |
| `Cargo.toml`, `Cargo.lock` | Build definition |
| `.gitignore` | Repository hygiene |
| `README.md`, `README-cn.md` | Project entry |
| `LSMGRAPH-ARCHITECTURE.md` | Clean-kernel architecture |
| `LSMGRAPH-STARTUP-GUIDE.md` | Startup instructions |

## Removed

| Removed path/type | Reason |
|---|---|
| `baseline/` | Experiment scripts, summaries, raw evidence, dated progress docs |
| `scripts/` | Paper/evaluation/experiment runners |
| `figures/` | Paper figures |
| `docs/` | Historical evidence archive |
| top-level planning markdown | Research/project management material |
| external-system reports | External baseline notes, not kernel code |
| `src/bin/p3_feedback_bench.rs` | Experiment runner |
| `src/bin/p3_feedback_sustained.rs` | Experiment runner |
| `src/bin/w3_workload_shift.rs` | Experiment runner |
| `src/bin/w5_steady_state_real_store.rs` | Experiment runner |

## Product Binary Surface

The branch keeps one binary:

```text
lsmgraph -> src/bin/lsmgraph.rs
```

Cargo no longer declares paper/experiment binaries.

## Selected Kernel Changes Carried In

The clean branch intentionally carries the non-experiment kernel changes from
the active research worktree:

| File | Change |
|---|---|
| `src/csr/format.rs` | `signature_pruning_decision()` and pruning reason tests |
| `src/metrics.rs` | pruning-reason metrics in metrics snapshot/reset |
| `src/semantic.rs` | `GraphAccessSignature::with_dst_label()` |

Not carried in:

| File/change | Reason |
|---|---|
| `src/bin/w7_sf30_workload_shift.rs` | experiment runner |
| `src/bin/lsmgraph.rs import-many` dirty change | W14 experiment path, not clean kernel |
| `src/snb/full_loader.rs import_snb_full_multi` dirty change | W14 import optimization, not core DB API yet |
| W6/W7/W8/W9/W13/W14 baseline scripts | experiment layer |
| W10 paper/table renderer | paper layer |

## Current K4 Status

| Area | Clean branch state |
|---|---|
| L0 exact-proof pruning | present |
| pruning reason observability | present |
| budgeted materialization machinery | present in kernel/config history |
| stable latency proof | not a branch concern |
| semantic-aware merge retention | not implemented |
| DB-native feedback-to-merge loop | not implemented |
| schema lifecycle cost model | not implemented |

## Validation Target

Minimum checks for this branch:

```text
cargo test --lib
cargo test --bin lsmgraph
cargo build --release --bin lsmgraph
```

Full benchmark and paper evidence checks belong to the research branch, not this
clean kernel branch.
