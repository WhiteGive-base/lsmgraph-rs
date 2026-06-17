# SemL0/K4 SIGMOD 实验计划

> 日期：2026-06-17  
> 主仓库：`/data/WorkSpace/lsmgraph-rs`  
> 执行环境：Linux 端为准  
> 目标：围绕 Query-semantic LSM 管理动态属性图全生命周期，补齐可投稿 SIGMOD 的系统证据链。

## 1. 论文主线

论文不能只讲 “L0 按 edge type / source label 切分以后读更快”。更稳的主线应该是：

```text
Query semantics can guide an LSM-based dynamic property graph through its lifecycle:
write -> flush -> semantic metadata -> read pruning -> feedback -> K4 merge -> recovery
```

因此实验要证明三件事：

1. 读路径：semantic metadata 和 segment layout 真的减少 read amplification。
2. 动态路径：mixed update/read 下，K4 能自动维护 L0/L1+，而不是手动离线整理。
3. 代价边界：读收益没有用不可接受的写放大、compaction 频率或恢复复杂度换来。

## 2. 当前已迁移的内核能力

这次从 `lsmgraph-rs-clean-kernel` 迁移到主仓库的能力包括：

| 能力 | 状态 |
|---|---|
| K4 lifecycle API | 已迁移 |
| `K4MergePolicy` | 已迁移 |
| L0 feedback merge | 已迁移 |
| L1+ fanout cascade | 已迁移 |
| L1+ `(src_label, edge_type)` semantic segment layout | 已迁移 |
| L1+ semantic metadata filter | 已迁移 |
| 跨层 semantic metadata 重新推导 | 已迁移 |
| 事件驱动 K4 maintenance scheduler | 已迁移 |
| SNB dynamic delta 普通路径自然触发 K4 | 已迁移 |
| paper 级 cost model / 写放大优化策略 | 后续补 |

重要边界：

```text
DB 内核应该包含：K4 scheduler、merge policy、semantic metadata、filter、metrics。
实验 runner 应该包含：LDBC 数据准备、矩阵运行、日志汇总、画图、baseline 对比。
```

## 3. 系统变体

实验矩阵至少要包含以下变体，避免只证明一个局部优化：

| 变体 | 含义 | 目的 |
|---|---|---|
| `Naive-LSM` | 普通 L0/L1 CSR，不使用语义 pruning | 最低基线 |
| `Schema-L0` | 只使用 schema/source label 粗粒度语义 | 证明单维语义不足 |
| `EdgeType-L0` | 只按 edge type 暴露语义 | 证明 edge type 贡献 |
| `SemL0` | query-semantic L0 layout/filter | 证明最初 L0 假设 |
| `SemL0+Feedback` | 加 L0 feedback merge | 证明热分区自适应 |
| `K4-No-L1Filter` | 有 K4 merge，但 L1+ 不做 semantic split/filter | L1+ filter 消融 |
| `K4` | 完整 K4：auto maintenance + L1+ layout/filter | 主系统 |
| `Oracle` | oracle semantic layout 或理想上界 | 给上界参考 |

## 4. Correctness Gate

任何性能实验前必须先通过 correctness gate：

```text
cargo test --lib
cargo test --bin lsmgraph
cargo test --test engine_tests
cargo build --release --bin lsmgraph
Rust snb-validate --max-lines 20
Java LDBC mixed validation smoke, MAX_LINES=5
```

新增 K4 correctness 必须单独保留：

```text
k4_lifecycle_flushes_feedback_compacts_and_reopens_schema_safe
k4_lmerge_cascades_l1_to_l2_with_semantic_filters
k4_auto_maintenance_read_feedback_compacts_hot_l0_partition
k4_auto_maintenance_flush_cascades_l1_to_l2
k4_delta_graph_normal_api_triggers_auto_maintenance
```

通过标准：

```text
所有 DB-level tests 通过；
SNB validate 无 incorrect / crash；
reopen 后 schema、manifest、sidecar、L1+ segment 查询结果一致。
```

## 5. 核心实验

### E1: L0 Semantic Read Amplification

问题：

```text
只在 L0 增加 query semantics，是否显著减少读取放大？
```

数据集：

```text
SF1 correctness/smoke
SF10 调参
SF30 主结果
SF100 规模趋势，资源允许时跑完整矩阵
```

指标：

```text
candidate_l0_segments
matched_l0_segments
range_filtered_segments
bloom_filtered_segments
signature pruning reason counts
CSR header/offset/body reads
query latency p50/p90/p99
```

结论目标：

```text
SemL0 相比 Naive-LSM / Schema-L0 / EdgeType-L0，在高选择性查询上显著降低 L0 candidate segment 和 body read。
```

### E2: K4 Auto Maintenance Under Mixed Workload

问题：

```text
在 SNB mixed update/read 中，K4 是否能自动触发 flush/feedback/fanout merge，并稳定读延迟？
```

工作负载：

```text
LDBC SNB mixed validation/workload
读写比例：read-heavy、balanced、update-heavy
热点：固定热点、阶段迁移热点、均匀访问
```

指标：

```text
read latency p50/p90/p99/p99.9
update latency p50/p90/p99
throughput
L0/L1/L2 segment count over time
K4 maintenance count
feedback compaction count
L1+ cascade count
pruning rate before/after maintenance
```

结论目标：

```text
K4 不依赖手动整理，在 mixed workload 下能自动把热 L0 分区和过宽 L1+ 层收敛，降低 tail latency 抖动。
```

### E3: LmergePolicy 与 L1+ Filter 消融

问题：

```text
L1+ semantic segment layout/filter 是否是 K4 必要组成，而不是只有 L0 有用？
```

对比：

```text
K4-No-L1Filter
K4 with semantic_partition_outputs=true
不同 fanout：2 / 4 / 8 / 10
不同 max_output_level：L2 / L3 / L4
```

指标：

```text
L1+ candidate segments
L1+ semantic pruning reason
L1+ body reads
segment count by level
compaction input/output bytes
query latency
```

结论目标：

```text
仅 L0 优化不够；热点长期运行后，L1+ 如果退化为 mixed segment，会重新带来读放大。K4 的跨层 semantic metadata 传播能避免这个退化。
```

### E4: Write Amplification 与 Maintenance Cost

问题：

```text
K4 的读收益是否以过高写放大为代价？
```

需要补的内核指标：

```text
per-maintenance input_bytes / output_bytes
per-level rewrite bytes
segments deleted / created
write amplification = compaction_output_bytes / logical_update_bytes
maintenance latency p50/p90/p99
foreground operation blocked time
```

实验输出：

```text
读收益 vs 写放大曲线
fanout sensitivity
semantic split granularity sensitivity
```

结论目标：

```text
K4 的 policy 在可控写放大下换取稳定读收益；如果某些 workload 不划算，需要明确边界。
```

### E5: Schema Evolution / Tombstone / Recovery

问题：

```text
动态图属性、schema 变更、tombstone 和 reopen 是否破坏 semantic pruning 正确性？
```

场景：

```text
新增 edge label
新增/drop property
property encoding change
insert 后 delete
旧 snapshot 和当前 snapshot 并存
K4 merge 后 reopen
```

指标：

```text
correctness pass/fail
conservative fallback count
exact semantic segment count
mixed/unknown metadata count
reopen recovery time
```

结论目标：

```text
K4 的 semantic metadata 是 DB-safe 的：无法证明 exact 时保守读取，不因 schema/tombstone 出错。
```

### E6: End-to-End Baseline

问题：

```text
完整系统相比外部 baseline 或内部 baseline 是否有端到端收益？
```

候选 baseline：

```text
DGS / TuGraph validation path
内部 Naive-LSM
内部 RocksDB-style conservative metadata
内部 FullCompact / OracleSemantic
```

指标：

```text
LDBC query latency
mixed workload throughput
update latency
memory footprint
storage footprint
build/load time
```

结论目标：

```text
K4 不只是 microbenchmark 优化，而能支撑 SNB 端到端动态图 workload。
```

## 6. 运行顺序

| 阶段 | 内容 | 成功标准 |
|---|---|---|
| P0 | correctness gate | 全部通过 |
| P1 | SF1/SF10 小矩阵 | 指标链路完整，日志可复现 |
| P2 | SF30 主实验 | 形成 paper 主图 |
| P3 | SF100 趋势/压力测试 | 至少得到规模趋势或资源边界 |
| P4 | 消融与 sensitivity | 证明 L1+ / fanout / policy 必要性 |
| P5 | 写放大与 cost | 回答 reviewer 对维护成本的质疑 |

## 7. 需要补齐的代码/指标

当前 clean DB kernel 已经补齐 1、2；paper 实验还需要补：

```text
K4 cost/benefit report
per-level rewrite bytes
logical update bytes
write amplification counter
maintenance foreground blocking time
K4 policy reason 字段
实验 runner 输出统一 JSON/TSV
图表汇总脚本
```

这些不应该污染内核语义，但底层 counters/report 应该在 Engine 里，runner 只负责收集和画图。

## 8. Paper Claim 对应证据

| 论文 claim | 必须实验 |
|---|---|
| Query semantics reduce graph LSM read amplification | E1 |
| Feedback makes layout adaptive to hot workload | E2/E3 |
| K4 prevents semantic dilution across levels | E3 |
| K4 is safe for dynamic property graph lifecycle | E5 |
| K4 cost is bounded and explainable | E4 |
| End-to-end SNB dynamic workload benefits | E6 |

## 9. 当前不能写死的说法

现在不能直接声称：

```text
K4 在所有 workload 都提升性能。
K4 写放大已经被最优控制。
后台调度器已经是生产级复杂 scheduler。
SF100 全矩阵已经完成。
```

当前可以声称：

```text
DB 内核已经具备 K4 自动生命周期链路；
SNB dynamic delta 可以通过普通 Engine API 自然触发 K4；
功能正确性已经由 unit/engine/SNB validate smoke 覆盖；
paper 级性能和 cost 证据需要按本计划补齐。
```
