# 干净内核分支清单

> 分支：`codex/k4-clean-kernel`
>
> Worktree：`/data/WorkSpace/lsmgraph-rs-clean-kernel`
>
> 清理基线：`48d44d1 W14 compare: result-digest correctness path + SF1 gate`
>
> 当前 LSM compaction 提交：`9841496 Implement-minimal-LSM compaction-engine-lifecycle`

## 1. 分支目的

这个分支的目的有两个：

1. 建立一个只包含 DB 内核、测试和最小产品入口的干净分支。
2. 在这个干净内核上实现 engine-level LSM lifecycle、显式 LmergePolicy、
   L1+ semantic segment/filter、事件驱动 LSM maintenance scheduler，
   以及 SNB dynamic delta 的自然触发路径。

这个分支不保存论文草稿、历史实验脚本、baseline trace、外部系统报告、
绘图产物和一次性 benchmark runner。

当前已经实现的 LSM lifecycle 链路是：

```text
write -> flush -> semantic sidecar -> read pruning -> feedback
      -> feedback compaction -> L1+ cascade -> schema-safe reopen
```

实现入口：

```rust
Engine::run_lifecycle(signatures)
Engine::run_lifecycle_with_repetitions(signatures, repetitions)
Engine::run_maintenance()
Engine::compact_levels()
Engine::compact_levels_with_policy(policy)
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

## 7. 当前 LSM compaction 状态

| 区域 | clean branch 状态 |
|---|---|
| L0 exact-proof pruning | 已实现 |
| pruning reason observability | 已实现 |
| budgeted materialization machinery | 已保留 |
| DB-native feedback-to-merge loop | 已实现，入口为 `Engine::run_lifecycle*` |
| semantic-aware L0->L1 merge retention | 已实现 |
| 显式 LmergePolicy | 已实现，入口为 `LevelMergePolicy` + `compact_levels*` |
| L1+ semantic segment/filter | 已实现，按 `(src_label, edge_type)` 输出并通过 signature filter 剪枝 |
| 事件驱动 LSM maintenance scheduler | 已实现，flush/read feedback/fanout 触发，默认关闭 |
| schema/tombstone-safe reopen 测试 | 已实现 |
| SNB mixed update 自动接入 LSM compaction | 已实现为 DynamicGraphView/DeltaGraph 自动维护路径自然触发 |
| schema lifecycle cost model | 未实现 |
| paper 级稳定延迟证明 | 不属于本分支直接产物 |

## 8. 新增 LSM compaction 文件变更

| 文件 | 内容 |
|---|---|
| `src/config.rs` | 新增 `with_auto_maintenance` 配置入口 |
| `src/graph.rs` | 新增 LSM lifecycle report、`LevelMergePolicy`、LSM maintenance scheduler、L1+ cascade compaction |
| `src/delta/mod.rs` | 新增 automatic LSM maintenance delta open/create 路径 |
| `src/dynamic_view/mod.rs` | 新增 `open_or_create_delta_with_auto_maintenance()` |
| `src/snb/props.rs` | SNB dynamic server 改为使用 LSM compaction delta 普通路径 |
| `src/lib.rs` | re-export LSM compaction report、maintenance report 和 merge policy 类型 |
| `tests/engine_tests.rs` | 新增 LSM compaction 端到端、L1->L2 semantic filter、自动维护和 DeltaGraph 普通路径测试 |
| `LSMGRAPH-ARCHITECTURE.md` | 中文架构文档和系统流程图 |
| `CLEAN-KERNEL-INVENTORY.md` | 中文分支清单 |

## 9. 验证结果

已经执行并通过：

```text
cargo test --lib
cargo test --bin lsmgraph
cargo test --test engine_tests
cargo test --test engine_tests lifecycle_flushes_feedback_compacts_and_reopens_schema_safe
cargo test --test engine_tests lmerge_cascades_l1_to_l2_with_semantic_filters
cargo test --test engine_tests auto_maintenance
cargo test --test engine_tests delta_graph_normal_api_triggers_auto_maintenance
cargo build --release --bin lsmgraph
```

结果摘要：

| 验证 | 结果 |
|---|---|
| `cargo test --lib` | 64 passed |
| `cargo test --bin lsmgraph` | 4 passed |
| `cargo test --test engine_tests` | 60 passed, 1 ignored |
| LSM lifecycle 单测 | passed |
| L1->L2 semantic filter 单测 | passed |
| automatic LSM maintenance 单测 | passed |
| DeltaGraph 普通路径单测 | passed |
| release build | passed |
| Rust `snb-validate --max-lines 20` | checked=20, passed=20, failed=0 |
| Java LDBC mixed validation smoke, `MAX_LINES=5` | Validation Result: PASS |

保留的 warning：

| warning | 状态 |
|---|---|
| `unused import: OpenOptions` in `src/base_graph/csr.rs` | 原有 warning |
| `unused variable: capacity` in `src/base_graph/csr.rs` | 原有 warning |

## 10. 当前结论

这个分支现在可以作为 后续工作的干净内核基线。

可以主张：

```text
LSMGraph clean kernel 已经具备 DB-native LSM lifecycle、显式 LmergePolicy、
L1+ semantic segment/filter、事件驱动 LSM maintenance scheduler、SNB dynamic delta
自然触发路径，并通过 engine-level 功能正确性测试、Rust SNB validate smoke、
以及 Java LDBC mixed validation smoke。
```

不能主张：

```text
paper 级 cost model / 写放大优化策略已经完成。
```

原因是本分支已经补齐 clean DB kernel 的自动生命周期链路，但还没有把读收益、
重写字节、write amplification 和稳定延迟做成论文级实验矩阵。
