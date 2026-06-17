# LSMGraph 干净内核架构文档

> 分支：`codex/k4-clean-kernel`
>
> 目录：`/data/WorkSpace/lsmgraph-rs-clean-kernel`
>
> 范围：本文只描述 DB 内核、存储格式、查询路径、写入路径、语义剪枝边界、
> K4 lifecycle API 和当前验证边界。论文草稿、实验脚本、baseline trace、
> 绘图文件和一次性 runner 不属于这个干净分支。

## 1. 主线目标

这个分支先完成了“干净 DB 内核分支”，随后在内核里补了一个最小完整的
K4 lifecycle API。当前目标不是把所有论文实验都放进这个分支，而是让
LSMGraph 内核本身具备一条可验证的 DB-native 生命周期链路：

```text
write -> flush -> semantic metadata -> read pruning -> feedback
      -> merge/compaction -> schema-safe recovery/reopen
```

当前实现落点是：

```rust
Engine::run_k4_lifecycle(signatures)
Engine::run_k4_lifecycle_with_repetitions(signatures, repetitions)
```

它会使用真实 Engine 路径完成：

1. flush 当前 MemGraph 写入。
2. 持久化 semantic sidecar。
3. 执行 query signatures，触发 segment-level read pruning。
4. 记录 L0 partition feedback。
5. 根据 feedback 选择 hot L0 partition 并 merge 到 L1。
6. 如果 L1+ 超过 K4 fanout，则按 LmergePolicy cascade 到下一层。
7. 再次持久化 semantic sidecar。
8. 通过 manifest、schema catalog、sidecar 支持 reopen 后恢复。

注意：SNB HTTP server 的 mixed update workload 目前还没有自动触发这条
K4 lifecycle。当前 K4 是 Engine 层能力，SNB 集成是后续工作。

## 2. 系统流程图

```mermaid
flowchart TD
    A["应用 / LDBC SNB / 内核测试"] --> B["Engine API"]

    B --> C["写入路径<br/>insert_edge / delete_edge / property insert"]
    C --> D["MemGraph<br/>内存写缓冲"]
    D --> E["flush_active()"]
    E --> F["L0 CSR Segment<br/>拓扑 + 属性 + semantic metadata"]
    F --> G["MANIFEST<br/>CreateFile / DeleteFile"]
    F --> H["Semantic Sidecar<br/>degree directory"]

    B --> I["读取路径<br/>get_neighbors_by_signature()"]
    I --> J["GraphAccessSignature<br/>src label / edge type / degree / dst label / property"]
    J --> K["Semantic L0 Index<br/>候选 L0 segment"]
    K --> L{"signature_pruning_decision"}
    L -->|pruned| M["跳过 segment<br/>记录 pruning reason"]
    L -->|kept| N["CSR Reader<br/>bloom / offset / body read"]
    N --> O["MVCC merge_visible<br/>insert/delete/tombstone"]
    O --> P["查询结果"]

    N --> Q["Metrics Feedback<br/>L0 partition query/probe counters"]
    Q --> R["compact_best_l0_partition_by_score()"]
    R --> S["L0 + overlapping L1 read all versions"]
    S --> T["snapshot/tombstone-safe retention"]
    T --> U["L1 CSR Segment"]
    U --> G
    U --> H
    U --> X{"L1+ fanout 超限?"}
    X -->|yes| Y["K4 LmergePolicy<br/>按 (src_label, edge_type) 重分区"]
    Y --> Z["L2/L3... semantic CSR Segment"]
    Z --> G
    Z --> H

    G --> V["Engine::open()"]
    H --> V
    V --> W["恢复 Version / Index / Semantic Index / Schema Catalog"]
```

## 3. 分支边界

保留内容：

| 路径 | 作用 |
|---|---|
| `src/` | DB 内核、SNB 服务路径、存储引擎、IO backend |
| `tests/` | 内核回归测试 |
| `Cargo.toml`, `Cargo.lock` | Rust 构建定义 |
| `README.md`, `README-cn.md` | 项目入口说明 |
| `LSMGRAPH-ARCHITECTURE.md` | 本架构文档 |
| `LSMGRAPH-STARTUP-GUIDE.md` | 启动和验证说明 |
| `CLEAN-KERNEL-INVENTORY.md` | 干净分支清单 |

移除内容：

| 路径 / 类型 | 移除原因 |
|---|---|
| `baseline/` | 历史实验脚本、结果、报告、trace |
| `scripts/` | 一次性实验和分析 runner |
| `figures/` | 论文图片，不是内核源码 |
| `docs/` | 历史证据归档，不是当前内核文档 |
| 顶层论文/计划 markdown | 论文和项目管理材料 |
| external-system reports | 外部 baseline 调研材料 |
| W/P experiment binaries | 不属于干净 DB 产品入口 |

产品 binary 只保留：

```text
target/release/lsmgraph
```

## 4. 模块分层

```text
Application / LDBC SNB / 内核测试
        |
        v
SNB server / query layer
  - DGS-compatible HTTP endpoints
  - LDBC SNB query/update mapping
        |
        v
Dynamic graph view
  - BaseGraph immutable CSR snapshot
  - DeltaGraph LSM update region
        |
        v
Engine
  - MemGraph write buffer
  - VersionManager MVCC snapshots
  - L0/L1 CSR segment metadata
  - semantic pruning / feedback / K4 lifecycle
        |
        v
CSR storage + IO backend
  - manifest
  - offset arrays / edge bodies / property sections
  - blocking / direct / io_uring backends
```

## 5. 核心模块

| 文件 | 责任 |
|---|---|
| `src/graph.rs` | Engine 主体，写入、flush、读取、semantic index、feedback compaction、K4 lifecycle、LmergePolicy |
| `src/memgraph/mod.rs` | 内存写缓冲 |
| `src/csr/format.rs` | CSR segment metadata、semantic summary、pruning decision |
| `src/csr/writer.rs` | CSR segment 写入 |
| `src/csr/reader.rs` | CSR segment 读取、bloom/offset/body probe |
| `src/csr/manifest.rs` | manifest replay 和文件生命周期 |
| `src/schema.rs` | schema catalog、epoch、alias/drop/change |
| `src/semantic.rs` | GraphAccessSignature、degree/direction/property predicate |
| `src/metrics.rs` | storage/CSR/SNB/feedback/pruning metrics |
| `src/snb/` | LDBC SNB 查询、HTTP adapter、属性辅助结构 |
| `tests/engine_tests.rs` | Engine 级功能正确性回归 |

## 6. 写入与 Flush

写入入口：

```rust
insert_edge()
delete_edge()
insert_edge_with_properties_prototype()
insert_edge_with_property_values_prototype()
```

写入先进入 `MemGraph`。当 `MemGraph` 满，或者调用 `flush_active()` 时，
Engine 会把当前 MemGraph freeze，创建新的 active MemGraph，并把旧 MemGraph
异步写成 L0 CSR segment。

L0 flush 会写入：

| 内容 | 说明 |
|---|---|
| topology records | `src/dst/edge_type/ts/marker` |
| property sections | property-aware CSR section |
| semantic metadata | src label、dst label、edge type partition、degree class |
| schema epoch | segment 对应的 schema epoch |
| property encoding epoch | property value 的编码 epoch |
| manifest record | `CreateFile { meta }` |

flush 后会更新：

1. `VersionManager` 当前可见版本。
2. L1+ 多级索引。
3. L0 semantic index。
4. degree directory sidecar。

## 7. 查询语义与剪枝

查询入口：

```rust
get_neighbors()
get_neighbors_typed()
get_neighbors_by_signature()
get_neighbors_matching_property_value()
```

语义签名由 `GraphAccessSignature` 表达：

| 字段 | 作用 |
|---|---|
| `src` | 查询源点 |
| `src_label` | 源点 label |
| `edge_type` | 边类型 |
| `direction` | 出边/入边/未知 |
| `degree_class` | Low / Medium / High / Mixed / Unknown |
| `dst_label` | 目标点 label |
| `min_ts`, `max_ts` | 时间范围 |
| `property_predicate` | required property / absent-or-default property |

每个 CSR segment 通过：

```rust
CsrSegmentMeta::signature_pruning_decision()
```

返回：

```rust
SignaturePruningDecision {
    pruned: bool,
    reason: &'static str,
}
```

典型 prune reason：

| reason | 含义 |
|---|---|
| `time` | segment 时间范围不可能匹配 |
| `src_label` | 源点 label disjoint |
| `edge_type` | edge type partition disjoint |
| `direction` | 方向 disjoint |
| `degree` | degree class disjoint |
| `dst_label` | 目标点 label disjoint |
| `property_absence` | segment 精确缺少 required property |

典型 keep/fallback reason：

| reason | 含义 |
|---|---|
| `kept_candidate` | 没有安全剪枝证明 |
| `mixed_unknown_fallback` | metadata mixed/unknown，必须保守读取 |
| `budgeted_not_materialized` | budgeted layout 没有物化该 exact degree partition |
| `schema_tombstone_fallback` | tombstone/schema 风险阻止 property absence pruning |

这些 reason 会进入 `Metrics`，用于解释 pruning 是否真的发生，以及为什么没有发生。

## 8. Feedback、LmergePolicy 与 L1+ Filter

读取 L0 时，Engine 会记录：

| 指标 | 说明 |
|---|---|
| `candidate_l0_segments` | 查询候选 L0 segment 数 |
| `matched_l0_segments` | 实际命中结果的 L0 segment 数 |
| `range_filtered_segments` | 被 src range 过滤的 segment |
| `bloom_filtered_segments` | 被 source bloom 过滤的 segment |
| `l0_partitions` | 按 src label、edge type、range bucket 统计的反馈 |

K4 的 merge policy 分为两段。

第一段是 L0 feedback merge：

```rust
compact_best_l0_partition_by_score()
```

它会从 metrics 中选择 hot L0 partition，读取对应 L0 和 overlap 的 L1，
再通过 snapshot/tombstone-safe retention 生成新的 L1 CSR segment。

第二段是 L1+ cascade merge：

```rust
K4MergePolicy
compact_k4_levels()
compact_k4_levels_with_policy(policy)
```

当前策略是保守正确的 fanout policy：

| 策略项 | 当前实现 |
|---|---|
| 触发条件 | 某个 L1+ source level 的 segment 数超过 `fanout` |
| 输入 | source level 全部 segment + target level 全部 segment |
| 输出层 | `source_level + 1` |
| 输出布局 | 默认按 `(src_label, edge_type)` 语义分区 |
| 保留规则 | snapshot/tombstone-safe retention |
| metadata | 由 CSR writer 对输出 segment 重新推导 exact semantic metadata |
| manifest | 新 segment 写 `CreateFile`，旧 segment 写 `DeleteFile` |

这个策略牺牲一部分 write amplification，换取实现边界清楚和功能正确：
只要 L1+ 被 cascade，输出就不会退化成一个 mixed 大段，而是保持可被
`GraphAccessSignature` 过滤的 semantic segment。

L1+ filter 路径如下：

1. `MultiLevelIndex` 根据 `src` 找到包含该 source 的 L1+ segment。
2. 每个候选 segment 再调用 `signature_pruning_decision()`。
3. 如果 edge type、src label、dst label、degree、property absence 等证明 disjoint，
   直接跳过该 segment。
4. 只有保留下来的 segment 才进入 CSR reader 的 bloom/offset/body probe。

因此 L1+ 的 filter 不是单独新增一个 paper-only 索引，而是复用并制度化了：

```text
source index -> semantic metadata filter -> CSR bloom/offset/body probe
```

新增 L1+ 测试：

```text
k4_lmerge_cascades_l1_to_l2_with_semantic_filters
```

该测试构造同一个 source 的 3 个不同 edge type 的 L1 segment，触发
K4 L1->L2 cascade，然后验证：

1. L1 被清空，L2 生成 3 个 segment。
2. L2 segment 保持 exact `src_label` 和 exact `edge_type_partition`。
3. 查询某一个 edge type 时，其他 L2 segment 会产生 `edge_type` pruning。
4. reopen 后 L2 查询结果仍正确。

这个路径不是 paper runner，而是 Engine 内核 API。新增测试：

```text
k4_lifecycle_flushes_feedback_compacts_and_reopens_schema_safe
```

`k4_lifecycle_flushes_feedback_compacts_and_reopens_schema_safe` 验证：

1. schema 变更能持久化。
2. 未 flush 的 tombstone 由 K4 lifecycle flush。
3. query signatures 能产生 L0 feedback。
4. feedback compaction 能选择 hot partition。
5. L0 merge 到 L1 后结果仍正确。
6. property value 查询仍正确。
7. reopen 后 schema epoch、manifest、L1 segment、查询结果仍正确。

## 9. Schema 与 Tombstone 安全

Schema catalog 持久化在 store 目录中。每个 segment 带 schema epoch。
查询在新 schema 下访问旧 segment 时，必须遵守 conservative fallback：
只有 metadata 足够精确且不会受到 alias/drop/encoding/tombstone 影响时才允许剪枝。

Tombstone 会隐藏旧版本 insert，因此 compaction 不能简单保留最新记录。
当前 compaction 使用 snapshot/tombstone-safe retention，确保旧 snapshot 和当前
snapshot 都不会读错。

## 10. Recovery / Reopen

`Engine::open()` 会恢复：

| 状态 | 恢复来源 |
|---|---|
| live files | `MANIFEST` replay |
| next file id | manifest 最大 file id |
| max timestamp | live segment max timestamp |
| schema catalog | schema catalog 文件 |
| L1+ index | segment offsets |
| L0 semantic index | L0 metadata + degree directory |
| degree directory | sidecar，缺失时可 rebuild |

K4 的正确性必须包含 reopen，因为只在内存里正确不能证明 DB 生命周期正确。

## 11. LDBC / SNB 验证边界

当前 clean branch 不跟踪 `deps/ldbc_snb_interactive_impls`，这是为了保持分支干净。
验证时可以使用外部已有 LDBC driver 目录，但 binary 使用 clean branch 产物：

```text
/data/WorkSpace/lsmgraph-rs-clean-kernel/target/release/lsmgraph
```

已经完成的验证：

| 验证 | 结果 |
|---|---|
| `cargo test --lib` | 64 passed |
| `cargo test --bin lsmgraph` | 4 passed |
| `cargo test --test engine_tests` | 56 passed, 1 ignored |
| K4 lifecycle 单测 | passed |
| release build | passed |
| Rust `snb-validate --max-lines 20` | checked=20, passed=20, failed=0 |
| Java LDBC mixed validation smoke, `MAX_LINES=5` | Validation Result: PASS |

## 12. 当前未完成项

| 方向 | 状态 |
|---|---|
| SNB mixed update 自动触发 K4 lifecycle | 未实现 |
| 连续后台 LmergePolicy 调度 | 未实现，当前为显式调用 |
| L1/L2/... semantic segment/filter | 已实现 fanout cascade + exact metadata filter |
| schema lifecycle cost model | 未实现 |
| metadata downgrade/repair 的显式状态机 | 部分由 CSR writer 重新推导，尚未抽象为独立状态机 |
| paper 级稳定延迟和写放大指标 | 需要后续实验 |

因此，当前分支可以支撑“DB 内核已经具备 K4 lifecycle、LmergePolicy
显式调用、L1+ semantic segment/filter，且功能正确”的说法；但还不能声称
“完整 SNB mixed workload 已经自动走 K4 生命周期”或“后台调度器已经生产化”。
