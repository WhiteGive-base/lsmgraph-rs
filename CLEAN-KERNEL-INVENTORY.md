# 干净内核分支清单

> 分支：`codex/k4-clean-kernel`
>
> Worktree：`/data/WorkSpace/lsmgraph-rs-clean-kernel`
>
> 清理基线：`48d44d1 W14 compare: result-digest correctness path + SF1 gate`
>
> 当前 K4 提交：`9841496 Implement-minimal-K4-engine-lifecycle`

## 1. 分支目的

这个分支的目的有两个：

1. 建立一个只包含 DB 内核、测试和最小产品入口的干净分支。
2. 在这个干净内核上实现 engine-level K4 lifecycle、显式 LmergePolicy、
   以及 L1+ semantic segment/filter。

这个分支不保存论文草稿、历史实验脚本、baseline trace、外部系统报告、
绘图产物和一次性 benchmark runner。

当前已经实现的 K4 链路是：

```text
write -> flush -> semantic sidecar -> read pruning -> feedback
      -> feedback compaction -> L1+ cascade -> schema-safe reopen
```

实现入口：

```rust
Engine::run_k4_lifecycle(signatures)
Engine::run_k4_lifecycle_with_repetitions(signatures, repetitions)
Engine::compact_k4_levels()
Engine::compact_k4_levels_with_policy(policy)
```

## 2. 保留内容

| 路径 | 保留原因 |
|---|---|
| `src/` | DB engine、storage、query、server、IO、schema、metrics |
| `tests/` | 内核功能正确性和回归测试 |
| `Cargo.toml`, `Cargo.lock` | Rust 构建定义 |
| `.gitignore` | 仓库卫生 |
| `README.md`, `README-cn.md` | 项目入口 |
| `LSMGRAPH-ARCHITECTURE.md` | 中文系统架构文档 |
| `LSMGRAPH-STARTUP-GUIDE.md` | 启动和验证说明 |
| `CLEAN-KERNEL-INVENTORY.md` | 本清单 |

## 3. 移除内容

| 移除路径 / 类型 | 原因 |
|---|---|
| `baseline/` | 实验脚本、结果、trace、历史进度文档 |
| `scripts/` | paper/evaluation/experiment runner |
| `figures/` | 论文图片，不是内核源码 |
| `docs/` | 历史证据归档，不是当前内核文档 |
| 顶层 planning markdown | 项目管理/论文计划材料 |
| external-system reports | 外部 baseline 调研，不是内核代码 |
| `src/bin/p3_feedback_bench.rs` | 实验 runner |
| `src/bin/p3_feedback_sustained.rs` | 实验 runner |
| `src/bin/w3_workload_shift.rs` | 实验 runner |
| `src/bin/w5_steady_state_real_store.rs` | 实验 runner |

## 4. 产品 Binary 边界

当前干净分支只保留一个产品 binary：

```text
lsmgraph -> src/bin/lsmgraph.rs
```

Cargo 不再声明 paper/experiment binaries。

## 5. 从研究分支带入的内核改动

以下改动被认为是稳定内核能力，已经带入 clean branch：

| 文件 | 改动 |
|---|---|
| `src/csr/format.rs` | `signature_pruning_decision()` 和 pruning reason 测试 |
| `src/metrics.rs` | pruning-reason metrics，纳入 metrics snapshot/reset |
| `src/semantic.rs` | `GraphAccessSignature::with_dst_label()` |

这些改动已经和原研究 worktree 做过逐字节核对：

```text
src/csr/format.rs
src/metrics.rs
src/semantic.rs
```

## 6. 没有带入的内容

| 文件 / 改动 | 未带入原因 |
|---|---|
| `src/bin/w7_sf30_workload_shift.rs` | 实验 runner |
| `src/bin/lsmgraph.rs import-many` 脏改动 | W14 实验路径，不是干净内核 API |
| `src/snb/full_loader.rs import_snb_full_multi` 脏改动 | W14 import 优化，尚未沉淀为核心 DB API |
| W6/W7/W8/W9/W13/W14 baseline scripts | 实验层 |
| W10 paper/table renderer | 论文层 |

## 7. 当前 K4 状态

| 区域 | clean branch 状态 |
|---|---|
| L0 exact-proof pruning | 已实现 |
| pruning reason observability | 已实现 |
| budgeted materialization machinery | 已保留 |
| DB-native feedback-to-merge loop | 已实现，入口为 `Engine::run_k4_lifecycle*` |
| semantic-aware L0->L1 merge retention | 已实现 |
| 显式 LmergePolicy | 已实现，入口为 `K4MergePolicy` + `compact_k4_levels*` |
| L1+ semantic segment/filter | 已实现，按 `(src_label, edge_type)` 输出并通过 signature filter 剪枝 |
| schema/tombstone-safe reopen 测试 | 已实现 |
| SNB mixed update 自动接入 K4 | 未实现 |
| schema lifecycle cost model | 未实现 |
| paper 级稳定延迟证明 | 不属于本分支直接产物 |

## 8. 新增 K4 文件变更

| 文件 | 内容 |
|---|---|
| `src/graph.rs` | 新增 K4 lifecycle report、`K4MergePolicy`、L1+ cascade compaction |
| `src/lib.rs` | re-export K4 report 和 merge policy 类型 |
| `tests/engine_tests.rs` | 新增 K4 端到端测试和 L1->L2 semantic filter 测试 |
| `LSMGRAPH-ARCHITECTURE.md` | 中文架构文档和系统流程图 |
| `CLEAN-KERNEL-INVENTORY.md` | 中文分支清单 |

## 9. 验证结果

已经执行并通过：

```text
cargo test --lib
cargo test --bin lsmgraph
cargo test --test engine_tests
cargo test --test engine_tests k4_lifecycle_flushes_feedback_compacts_and_reopens_schema_safe
cargo test --test engine_tests k4_lmerge_cascades_l1_to_l2_with_semantic_filters
cargo build --release --bin lsmgraph
```

结果摘要：

| 验证 | 结果 |
|---|---|
| `cargo test --lib` | 64 passed |
| `cargo test --bin lsmgraph` | 4 passed |
| `cargo test --test engine_tests` | 57 passed, 1 ignored |
| K4 lifecycle 单测 | passed |
| K4 L1->L2 semantic filter 单测 | passed |
| release build | passed |
| Rust `snb-validate --max-lines 20` | checked=20, passed=20, failed=0 |
| Java LDBC mixed validation smoke, `MAX_LINES=5` | Validation Result: PASS |

保留的 warning：

| warning | 状态 |
|---|---|
| `unused import: OpenOptions` in `src/base_graph/csr.rs` | 原有 warning |
| `unused variable: capacity` in `src/base_graph/csr.rs` | 原有 warning |

## 10. 当前结论

这个分支现在可以作为 K4 后续工作的干净内核基线。

可以主张：

```text
LSMGraph clean kernel 已经具备 DB-native K4 lifecycle、显式 LmergePolicy、
L1+ semantic segment/filter，并通过 engine-level 功能正确性测试、Rust SNB validate smoke、
以及 Java LDBC mixed validation smoke。
```

不能主张：

```text
完整 SNB mixed update workload 已经自动走 K4 lifecycle。
```

原因是 SNB HTTP server 目前还没有自动把 mixed update workload 接到
`Engine::run_k4_lifecycle*`。这是下一阶段的集成工作。
