# SemL0：面向 LSM 动态属性图的查询语义生命周期管理

本文件是英文单文件 draft 的中文阅读版。它保留英文技术术语、代码符号、证据路径与关键数字，目的是帮助快速判断论文主线和 SIGMOD 风险。

## Abstract 中文版

LSM-based stores 对动态图写入友好，但读路径基本 query-semantics-blind：即使 graph predicate（label、edge type、direction、degree、property）已经证明某些 L0 或跨层 segment 不可能匹配，系统仍会探测它们。

SemL0 把 property-graph query signature 变成 lifecycle control signal：flush 时生成 per-segment **semantic pruning surface**；读路径只有在 metadata 证明 segment 不可能包含 visible match 时才剪枝，否则保守读取。

Naive compaction 会把 semantic partitions 合并成 mixed segments，从而侵蚀 pruning surface。SemL0 让 compaction semantics-aware：feedback 选择高代价 partitions，semantic merge 跨层保留或重建 surface，并把 read benefit 与 rewrite cost 同表核算。

在 tombstones、degree change、additive schema evolution 和 snapshot-visible deltas 下，SemL0 只在 exact evidence 下剪枝，否则 conservative read，从而保持 **no-false-negative invariant**。

在 LDBC SNB up to SF100 上，SemL0 降低 candidate segments 和 read bytes，correctness mismatches 为 0；budgeted/schema variants 的内存与 semantics-blind baseline 基本持平，full semantic 是 unbudgeted stress point。Latency 收益依赖 workload，因此论文报告 pruning-surface retention 及其 write-amplification cost，而不声明 uniform speedup。

## Contributions 中文版

- **C1：Query-semantic pruning surface。** 将 property-graph query signature 暴露给 LSM read path 的 segment-level metadata；用 exact/conservative/unknown completeness model，只在证明 disjointness 或 absence 时剪枝。
- **C2：Lifecycle retention of the pruning surface。** 把 pruning surface 当成必须跨 flush、read、feedback、compaction 存活的状态；semantic-aware compaction 保留或重建 surface，并显式报告 rewrite/write-amplification cost。
- **C3：Snapshot- and schema-correct semantic pruning。** 在 tombstones、degree change、additive schema evolution、snapshot deltas 下保持 DB-safe pruning，通过三条 invariant 和 no-false-negative theorem 约束。

## 1. Introduction 中文版

动态图需要持续吸收 edge/property updates，同时还要快速回答 selective reads。LSM 适合写路径，但读路径会因为 L0 overlap 探测很多 segment。传统 framing 把问题看成 physical adjacency locality；本文认为它同时是 semantics 问题，因为 property-graph query 在读 body 前就暴露 source/destination label、edge type、direction、degree、property、schema epoch。

SemL0 的 thesis 是：query signature 应当成为 LSM graph store 的 lifecycle control signal。Flush 生成 semantic pruning surface，read 用 exact proof 剪枝，compaction 保留/重建 surface，schema/snapshot 规则保证保守正确。论文不声称完整图数据库、完整 schema migration engine、production write-stall study 或 end-to-end production report。

## 2. Background and Problem 中文版

LSM graph layout 将写入 flush 成 immutable CSR-like L0 segments，并通过 background compaction 下推。L0 overlap 让读路径探测多个 segment；compaction 若不看 semantics，会生成更粗的 mixed segments。

Graph query signature 包含 source label、edge type、direction、degree class、destination label、property presence/value、snapshot、schema epoch。Pruning 的安全契约是：

> 只有 metadata 证明 disjointness 或 absence 时才剪枝；否则保守读取。

## 3. System Overview 中文版

SemL0 有四个 loop：

1. **Write / flush**：生成 CSR-like L0 segments，并推导 semantic metadata。
2. **Read / prune**：把 query predicate 编译成 `GraphAccessSignature`，调用 pruning decision。
3. **Feedback / compaction**：用 runtime metrics 选择 hot/costly semantic partitions，运行 semantic-aware merge。
4. **Schema / snapshot safety**：用 versioned schema catalog 和 per-segment schema epoch 保证旧 segment 可解释；不确定时 conservative read。

同一个 `SegmentSemanticState`（topology / degree / property / tombstone / schema）贯穿 flush、read、compaction、schema resolution。

## 4. C1：查询语义剪枝面

C1 说明如何从 `GraphAccessSignature` 到 `CsrSegmentMeta`，以及如何用 `signature_pruning_decision` 执行 exact-proof pruning。

关键点：

- `SemanticSummaryCompleteness` 是 `Exact` / `Conservative` / `Unknown` 三值。
- `Unknown` 或 legacy/mixed/tombstone-sensitive metadata 强制 conservative read。
- Prune reasons 包括 `time`、`src_label`、`edge_type`、`direction`、`degree`、`dst_label`、`property_absence`。
- Keep reasons 包括 `mixed_unknown_fallback`、`budgeted_not_materialized`、`schema_tombstone_fallback`、`kept_candidate`。
- Benefit-scored materialization 避免 materialize everything，防止 unbounded fanout。

证据：

- W6 SF100：`naive` candidate L0 mean 49,257,601；pruned variants 约低一个数量级；`semantic` read bytes 最低；全部 `mismatches=0`。
- W8 SF30：property predicate 改善 candidate/body/read/elapsed；2-hop 改善 body/read/elapsed，但不写 universal candidate reduction。
- W14：correctness parity 成立，但 composite semantics 稳定优于 edge-type-only 未证明，因此不能写强 claim。

## 5. C2：Pruning surface 的 lifecycle retention

C2 是承重贡献。Naive merge 会把多个 exact partitions 合并成 mixed segment，`src_label` / `edge_type_partition` 变成 sentinel，surface 被毁掉。Semantic merge 按 `(src_label, edge_type)` 分组输出，因此保留或重建 exact surface。

### Controlled synthetic 结果

| scale | policy | retention | read after vs before | output segs | write_amp | mismatches |
|---|---:|---:|---|---:|---:|---:|
| SF1 | naive | 0.0 | 4x blow-up | 1 | 1.22 | 0 |
| SF1 | semantic | 1.0 | flat | 4 | 1.87 | 0 |
| SF10c | naive | 0.0 | 6x blow-up | 1 | 1.14 | 0 |
| SF10c | semantic | 1.0 | flat | 6 | 1.85 | 0 |

### 真实 LDBC SF30 结果

- 1.09B directed edges。
- 40 `(src_label, edge_type)` partitions。
- 528 exact L1 segments。
- Naive：retention 0.0，mixed_after 1.0，output_segs 503，write_amp 1.07。
- Semantic：retention 1.0，mixed_after 0.0，output_segs 528，write_amp 1.24。

新增 real SF30 metadata read-amp proxy：在 40 个 typed-neighbor partitions 上 replay post-merge segment metadata，naive avg candidate segments/query = 93.6，candidate bytes total = 282.16 GB，weighted read-amp proxy = 6.52x；semantic avg candidate segments/query = 13.2，candidate bytes total = 43.25 GB，weighted proxy = 1.00x。

解释：真实 SF30 现在证明 retention、write cost，并用 metadata replay 连接 read-amp 后果；但它仍不是完整 body-read workload。论文必须保持三层区分。

## 6. C3：Schema / snapshot correctness

C3 证明 schema evolution、snapshot、tombstone 不会让 semantic pruning 漏读。

三条 invariant：

1. **Segment Schema**：每个 segment 记录自己的 `schema_epoch` / `property_encoding_epoch`。
2. **Epoch-Aware Resolution**：读路径按 segment 自己的 epoch 解释 metadata。
3. **Conservative Pruning**：只有 exact/conservative proof 才能剪枝，不确定就读。

No-false-negative theorem：如果 segment 包含 query 下可见的 match，则 `signature_pruning_decision` 不会剪掉它。Schema/snapshot uncertainty 只能减少 pruning precision，不能制造 false negative。

证据：W13 10 个 schema-evolution tests，通过 old-segment readability、mixed delta across compaction/reopen、alias/drop、encoding epoch、new label exact-vs-mixed pruning 等场景。

## 7. Evaluation 中文版

六个 RQ：

- **RQ1**：W6 SF100 证明 semantic surface 降低 read amplification。
- **RQ2**：budgeted variants 避免 full-semantic fanout cliff。
- **RQ3**：W7/W9 说明 feedback 与动态 churn 下的 tail-latency story。
- **RQ4**：W13 证明 schema evolution/deltas correctness。
- **RQ5**：maintenance/RSS cost 可见，budgeted/schema 与 naive 持平。
- **RQ6**：C2 证明 semantic-aware compaction 保留 pruning surface，且 write cost bounded。

最强的三张表：

1. W6 SF100 main matrix：candidate/read bytes/RSS/correctness。
2. W9 SF30 mixed read/write p99 over time：dynamic tail-latency evidence。
3. C2 retention table：controlled read-amp + real SF30 retention/write cost + real SF30 metadata proxy。

## 8. Related Work 中文版

SemL0 与 LSM/KV compaction tuning、query-driven physical design、graph storage、schema evolution 相关。核心差异是：

> 控制信号是 property-graph query signature，被维护对象是 segment-level semantic pruning surface，并且该 surface 跨 LSM lifecycle 保守正确。

External baseline：

- LiveGraph SF10：真实外部系统，355,185,382 edges，load 1,502.59s，peak RSS 约 45.7GiB，positive core edge types scan weighted avg 2.742us。
- LiveGraph SF100 infeasible（17-21 days extrapolation），所以 external comparison 诚实限制在 SF10。
- RocksDB-style KV-LSM 是内部 style baseline，不是 official RocksDB。
- KV-style encoding 是 simulated，不是 measured external system。

## 9. Limitations 中文版

必须保留的边界：

- Latency 是 supporting signal，不是 headline。
- 不声明 composite semantics 稳定优于 edge-type-only。
- C2 的完整 body-read workload 在 synthetic controlled rows 测；真实 SF30 新增 metadata candidate replay，但仍不是完整 SF30 body-read workload。
- External baseline 是 LiveGraph SF10-only。
- Feedback 是 controlled evidence，不是 production trace。
- Schema scope 是 additive + fixed-width，不覆盖 arbitrary migration。
- 无 production write-stall characterization。
- 投稿前还需 TeX/PDF、figures、bib citations、page budget。

## 10. Conclusion 中文版

SemL0 说明 property-graph query signature 可以成为 LSM-based dynamic graph store 的 lifecycle control signal：它影响 flush-time pruning surface、read pruning、feedback 和 cross-level compaction retention，同时在 schema evolution、tombstones、snapshot-visible deltas 下保持 conservative correctness。它不是新图数据库，而是一种让 query semantics 管理 LSM property graph 物理生命周期的安全边界清晰的方法。

## Appendix 中文版

核心证据路径：

- C1：`baseline/sf100-matrix-20260613-cn.md`，`baseline/w8-property-2hop-summary-20260615-cn.md`
- C2：`baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`，`baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md`，`remote-logs/c2-sf30-real-20260619/{naive,semantic}.json`
- C3：`baseline/w13-schema-evolution-summary-20260614.md`
- External baseline：`remote-logs/livegraph-sf10-20260612/`，`tables/external-baseline-md.md`
