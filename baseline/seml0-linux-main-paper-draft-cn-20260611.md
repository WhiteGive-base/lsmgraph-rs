# SemL0: 面向 LSM 动态属性图的查询语义物理设计

> 论文草稿，Linux 结果为主，2026-06-11。
> 当前主事实源：`/data/WorkSpace/lsmgraph-rs`，分支 `codex/sf100-basegraph-bench`，HEAD `e3c178dd09935ce22982b5832f0a0717699ea4b7`。
> 本稿保留 `[[TODO: ...]]` 占位，表示还需要补跑或最终填数的位置。

## 摘要

基于 LSM 的动态图存储适合高频更新，因为新边、删除和属性变更可以先进入内存写缓冲并刷新为不可变段。但是，这种写友好的组织会在读路径上引入明显的 L0 read amplification。L0 段彼此重叠，传统图 LSM 通常按键范围、层级或邻接结构组织最近更新，导致一次语义上很窄的属性图查询仍可能探测大量无关段。

本文提出 SemL0，一种面向 LSM 动态属性图的查询语义物理设计。SemL0 将属性图查询中的 source label、edge type、direction、degree class、property presence 和 schema epoch 抽象为存储层可见的 `GraphAccessSignature`，并在 CSR-like L0 段元数据中记录精确或保守的语义摘要。读路径只在元数据能够证明不相交或属性缺失时裁剪段；混合、未知、legacy、tombstone-sensitive 或 schema 不确定的段全部保守保留。因此，SemL0 的查询语义裁剪是 exact-proof 的优化，而不是可能产生 false negative 的查询重写。

SemL0 进一步引入 benefit-scored budgeted materialization。完整语义布局按 `(label, edge_type, degree)` 细分 L0，能提供更强的剪枝表面，但也会产生更多 L0 文件、manifest 元数据和 cache 压力。SemL0 将 full semantic 视为一个设计端点，而不是默认最优点；它通过 file budget 和收益打分，只把高价值 edge type 或 degree class 保持为精确语义分区，其余分区退化为 schema-style 的保守布局。SemL0 还使用运行时反馈选择高 read-amplification 的语义范围进行 targeted compaction，使维护工作随查询热点变化而移动。

我们在真实 LDBC SNB SF30 和 SF100 上对九种 L0 layout/policy 进行了受控消融，包括 naive、LSMGraph-style、label-only、edge-type-only、degree-only、schema、full semantic、budgeted SemL0 和 full L0-to-L1 compact。结果表明，语义 L0 布局将候选 L0 段数相对 naive 降低约 8 倍。SF30 上，edge-type-only/schema/full semantic/budgeted SemL0 将 read bytes 降低 99.8% 到 99.9%，关键变体与 schema baseline 的 neighbor-compare mismatch 为 0。真实 SF100 上，小预算 SemL0 达到与 schema/edge-type-only 接近的 read volume 和 L0 fanout，并取得最低平均延迟；同时，full semantic 在 SF100 上出现过度分段和 metadata-cache 抖动，说明“更细语义物化”不是免费上界。该结果支持本文的核心结论：属性图查询语义应当指导 LSM L0 布局，但必须以可控成本和保守正确性为边界。

SF100 同档 `naive@s5000` 已完成：naive 读取 25.44 GiB、候选 L0 段 49,257,601、平均延迟 5382.8 us。相对 naive，schema 将 read bytes 降低 99.43%、candidate L0 降低 87.96%；`budg-b64` 将 read bytes 降低 99.44%、candidate L0 降低 87.77%，并将平均延迟降低 32.0%。
[[TODO: 如果完成 LiveGraph 外部数值 baseline，在摘要最后补一句外部对照结果。]]

## 1. 引言

动态属性图同时具有高更新率和复杂读路径。社交网络、推荐图、交易图和知识图谱都会持续写入边、删除边、更新属性，同时对邻居、关系类型、路径、属性和快照进行查询。LSM-based graph store 适合这类更新，因为写入可以先进入内存 delta，再批量刷新为不可变的 CSR-like 段，并通过 compaction 逐步整理。

问题在于读路径。L0 是 LSM 中最新、重叠最多的层级。一次邻居查询如果只依赖键范围或源点范围，往往必须检查许多最近刷出的 L0 文件。这种开销在属性图中尤其浪费，因为查询本身通常已经包含很强的语义信息。例如，查询可能只关心 `Person` 顶点的 `Knows` 出边，也可能只关心某一类边上的属性谓词。传统存储层知道“要查某个 source 的邻接”，但不知道“哪些 label、edge type、degree class 或 property summary 能证明一个段无关”。

本文的核心观察是：property graph query semantics 不应只存在于查询层。它应该成为 LSM 物理设计的一等输入，指导 L0 flush layout、segment metadata、read-time pruning 和 compaction priority。

SemL0 正是围绕这个观察设计的。它将查询访问模式表示为 storage-facing 的 `GraphAccessSignature`。写入刷盘时，SemL0 为每个 CSR-like 段写入 source label、edge type、degree class、property presence、schema epoch 和 tombstone summary。读取时，SemL0 使用这些摘要和 semantic L0 index 过滤候选段。裁剪规则非常保守：只有 exact metadata 证明段不可能包含可见匹配时才跳过；否则宁可多读。这样，查询语义成为安全的物理剪枝证明，而不是不透明的启发式优化。

完整语义物化并不总是最优。按 `(label, edge_type, degree)` 完整细分 L0 可以增加剪枝机会，但也会增加文件数和元数据开销。在真实 SF100 中，我们观察到 full semantic 布局产生 6615 个 L0 文件，而 schema 只有 3444 个。由于 metadata cache 容量和 offset-array 读粒度限制，full semantic 的 `read_bytes` 被放大到约 9994 MiB。这不是语义剪枝错误，而是过度分段触发的系统代价。SemL0 因此采用 benefit-scored budgeted materialization，在 schema-style 和 full semantic 之间选择可控工作点。

本文贡献如下：

1. 查询语义 L0 物理设计。SemL0 将 source label、edge type、direction、degree class、property presence 和 schema epoch 编码进 L0 segment metadata，并用 exact-proof 规则减少 L0 read amplification。
2. 带预算的语义物化策略。SemL0 通过 file budget 和 benefit score 选择性保留高价值语义分区，避免 full semantic 在大规模下的 fanout 和 metadata-cache 退化。
3. 反馈式语义 compaction。SemL0 记录每个语义范围的查询次数、候选段数、cache miss 和 rewrite cost，并选择当前热点范围进行 targeted compaction。
4. 动态更新下的保守正确性。SemL0 将 schema epoch、property encoding、tombstone 和 snapshot visibility 纳入剪枝证明，确保 unknown/mixed metadata 导致额外读取而不是漏读。
5. Linux 实证评估。我们在真实 LDBC SNB SF30/SF100 和受控 feedback microbenchmark 上评估内部 layout 消融、budget sweep、读放大、维护成本和正确性边界。

本文不声称 SemL0 是完整图数据库，也不声称完整 schema migration 或生产级写停顿安全。本文关注的是一个更窄但关键的问题：查询语义如何安全地塑造 LSM 动态属性图的 L0 物理设计。

## 2. 背景与问题

### 2.1 LSM 动态图存储

LSM 图存储通常将可变写缓冲和不可变邻接段结合。新更新进入 MemGraph 或 delta buffer；当缓冲达到阈值后，系统将其刷新为 L0 段。段内部可以采用 CSR-like 格式，以支持紧凑邻接扫描。后台 compaction 再将多个段合并到更稳定的层级中。

这种设计对写入友好，但 L0 读放大明显。L0 文件通常在 key space 上重叠。查询某个 source 的邻居时，系统必须检查多个 L0 段才能确定是否存在相关记录。随着更新积累，L0 段数和 candidate probes 会增长。

### 2.2 属性图查询的语义盲视

属性图查询并不是普通 key-value lookup。一次查询可能包含：

- source label，例如 `Person`；
- edge type，例如 `Knows`、`HasCreator`、`LikesPost`；
- direction；
- degree class；
- required property 或 absent/default property；
- snapshot id；
- schema epoch 或 property encoding epoch。

如果 L0 段元数据只记录 source range，系统就不能安全跳过其他 edge type 或 label 的段。相反，如果段元数据精确说明自己只包含 `HasTag`，那么 `Knows` 查询就可以跳过该段。

关键难点是正确性。语义元数据可能是 mixed、unknown 或 legacy；段可能包含 tombstone；schema 可能已经演进。错误地把“不知道”当成“不存在”会产生 false negative。因此 SemL0 使用以下安全契约：

```text
Only prune when metadata proves disjointness or exact absence.
Otherwise, read conservatively.
```

这条契约贯穿全文。SemL0 的优化失败模式是多读，而不是漏读。

## 3. 系统总览

SemL0 位于 LSM 动态属性图存储的 delta/L0 路径。系统由四个回路组成。

写路径首先将更新吸收到 MemGraph。当 MemGraph 满或显式 flush 时，SemL0 根据当前 `--l0-layout` 策略将边分组，并写成 CSR-like L0 segment。每个 segment 携带语义元数据，包括 `src_label`、`edge_type_partition`、`degree_class`、`summary_completeness`、`schema_epoch`、property summary 和 tombstone flag。

读路径将邻居查询或属性查询编译成 `GraphAccessSignature`。如果查询包含 edge type 和可识别 source label，系统使用 semantic L0 index 找候选段；如果查询还携带 degree hint，系统结合 degree directory 做 degree-aware candidate routing。随后每个段还要经过 `may_contain_signature()`、source range、SourceBloom 和 CSR reader 过滤。

维护路径记录 read-amplification feedback。系统对每个 L0 semantic partition 记录 query count、candidate segments、filter-passed segments、offset cache miss 和 rewrite estimate。feedback compaction 根据收益和重写代价选择热点范围，将 L0 段重组到更有利的物理布局。

安全层维护 schema catalog、property encoding epoch、snapshot visibility 和 tombstone semantics。旧段保持自己的 `schema_epoch`，新 schema 影响未来写入和查询解释，但不会使旧段失效。

## 4. 查询语义 L0 物理设计

### 4.1 GraphAccessSignature

`GraphAccessSignature` 是存储层的访问签名，不是完整逻辑查询计划。它包含 source、source label、edge type、direction、degree class、目标 label、时间范围和 property predicate 等物理相关维度。SemL0 用它来回答一个存储问题：

```text
这个 L0 segment 是否可能包含当前 snapshot 下对该 signature 可见的匹配边？
```

### 4.2 Segment Metadata 与 exact-proof 裁剪

每个 CSR segment 的 metadata 可以处于三类状态：

| Metadata 状态 | 读路径动作 |
|---|---|
| exact match | 保留候选 |
| exact disjoint 或 exact absent | 裁剪 |
| mixed / unknown / legacy / tombstone-sensitive / schema-uncertain | 保守保留 |

这使 SemL0 的裁剪具备可解释性。它不是根据统计概率跳过段，而是根据 metadata proof 跳过段。

### 4.3 Layout 设计空间

SemL0 在同一内核中支持多个 `--l0-layout`，用于公平消融：

| Layout | 含义 | 作用 |
|---|---|---|
| naive | 无语义布局 | 无语义基线 |
| lsmgraph-style | key/range only | 传统 LSM-CSR 风格 |
| label-only | 只按 source label | 检查 label 信号 |
| edge-type-only | 只按 edge type | 检查 edge type 信号 |
| degree-only | 只按 degree class | 检查 degree 信号 |
| schema | `src_label + edge_type` | graph-aware 强内部基线 |
| semantic | `src_label + edge_type + degree` | full semantic 设计点 |
| semantic-budgeted | benefit-scored SemL0 | 本文主策略 |
| full-compact | L0-to-L1 compact | 写优化极端基线 |

该设计空间回答审稿人最可能提出的问题：收益到底来自 query semantics，还是来自已有 graph-aware label/type partition、Bloom filter、metadata cache 或 BaseGraph CSR。通过同一内核、同一数据、同一 sample plan 的 layout 消融，SemL0 将这些因素拆开。

## 5. 带预算的语义物化

完整语义布局将 `(src_label, edge_type)` 进一步按 degree class 细分。小规模时，这可以带来更细粒度剪枝；但大规模下，过度分段会增加 L0 文件数、manifest 元数据和 metadata cache 压力。

SemL0 的 budgeted policy 分两层控制：

1. edge type 级别：按 group bytes、estimated exact files 和 query weight 计算 benefit score，选择值得精确维护的 edge type。
2. degree class 级别：只在 class bytes、source 分布和 benefit score 达到阈值时保留 exact degree，否则合并为 Mixed degree。

关键修正是：未被预算选中的 edge type 不再塌缩为 `MIXED_EDGE_TYPE`，而是保留真实 edge type，并只把 degree 合并为 Mixed。这样 budget 0 对应 schema-style layout，预算增加时逐步向 full semantic 过渡，不会丢掉 schema 已有的 edge-type pruning。

因此，SemL0 的 budgeted materialization 不是“少做一些 full semantic”，而是在 schema 和 full semantic 之间选择成本可控的物理工作点。

## 6. 反馈式语义 Compaction

静态 layout 可能与当前热点不匹配。SemL0 的 feedback compaction 将维护目标从传统 level size/key overlap 扩展到 semantic range。

系统记录每个 L0 partition 的：

- query count；
- candidate L0 segments；
- filter-passed segments；
- offset cache miss rate；
- estimated rewrite bytes；
- compaction input/output bytes；
- compaction latency。

然后按收益和重写成本选择 `(src_label, edge_type, source range)` 进行 targeted compaction。受控 workload-shift microbenchmark 显示，phase A 和 phase B 的热点不同，反馈选择的 range 也随之改变。每个 phase 中，反馈前热点查询需要探测 30 个 L0 candidates；反馈 compaction 后候选 L0 降为 0；no-feedback 基线仍保留 6 个候选段。

该结果证明 SemL0 的反馈路径能够在受控负载迁移下移动维护优先级。它不等价于生产级长期稳定性证明。

补充的 sustained feedback run 将实验延长到 30 分钟，每 5 分钟记录一次 checkpoint，并让 A/B/C 三个热点阶段依次出现。正式输出位于 `remote-logs/p6-sustained-feedback-20260612/`。该 run 共生成 7 个 checkpoint（0/5/10/15/20/25/30 分钟），`selected_ranges_changed=true`，说明 feedback path 在三个阶段中都切换到了当前热点 range。反馈组在 compaction 后的最大平均 candidate L0 为 0.0；no-feedback 组最大平均 candidate L0 为 3.0。该 run 共触发 7 次 compaction，compaction_input_bytes=2056，compaction_output_bytes=1448，io_write_bytes=1448。

## 7. Schema 与 Snapshot 正确性

查询语义裁剪必须在动态图更新下保持正确。SemL0 的原则是：schema 和 snapshot 信息是剪枝证明的一部分。

### 7.1 Schema epoch

schema catalog 记录 label、property 和 encoding 的逻辑到物理映射。每次 schema 更新会推进 catalog epoch。未来写入使用新 epoch；旧 segment 保留自己的 `schema_epoch`。查询在当前 schema 下编译，但旧段仍按其写入时的 epoch 解释。

因此，添加 edge label 或 property 不会使旧存储失效。旧段如果 exact metadata 能证明与新查询不相交，可以裁剪；如果 metadata mixed/unknown/legacy，则读取。

### 7.2 Tombstone 与 snapshot

删除会产生 tombstone。Tombstone 可能隐藏更旧 snapshot 中的 insert，因此含 tombstone 或 tombstone-sensitive 的段不能因为缺少 required property 或 edge type 就随意跳过。SemL0 在 `may_contain_signature()` 和 property predicate 中保守保留相关段。

### 7.3 当前正确性证据

当前测试覆盖：

- delete/tombstone 隐藏最新边；
- 删除后旧 snapshot 仍可见；
- compaction 后保留旧 snapshot；
- degree change 下的保守 degree pruning；
- schema epoch + snapshot mixed delta；
- property schema + snapshot mixed delta；
- edge label alias；
- property encoding epoch；
- dropped property 对 value query 隐藏但 topology 仍可读；
- legacy missing property summary 保守读取。

这些测试支持当前声明：在 additive schema、固定宽度 property equality、tombstone 和 snapshot 边界内，SemL0 不产生 false negative。

本文不声明完整 rename/drop/type-change physical migration，也不声明 range/string/compound property predicates。

## 8. 实验设置

所有主要性能实验以 Linux 端为准。

```text
repository: /data/WorkSpace/lsmgraph-rs
branch: codex/sf100-basegraph-bench
head: e3c178dd09935ce22982b5832f0a0717699ea4b7
baseline dir: /data/WorkSpace/lsmgraph-rs/baseline
main SF100 log: /data/WorkSpace/lsmgraph-rs/remote-logs/qslsm-sf100-strong-baseline-20260610
```

SF100 `naive@s5000` 事实源：

- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-bench.json`
- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-stats.json`
- `baseline/sf100-results-s5000.tsv`
- `baseline/sf100-strong-baseline-traces/summary-sf100.tsv`

数据集：

- LDBC SNB SF30：用于九种 layout 的完整内部消融和 full-compact 对照。
- LDBC SNB SF100：真实规模，`directed_edges = 3,570,968,680`。

工作负载：

- storage-bench neighbor workload；
- 9 个主要 edge type：`1,2,3,7,8,9,10,11,12`；
- SF100 主表使用 `SAMPLES=5000`，即每变体 9 edge type × 5000 source；
- 所有变体复用 schema store 生成的同一 sample plan；
- degree-aware 变体使用 `--semantic-degree-hint`。

主要指标：

- candidate L0 segments：最干净的 L0 read-amplification 指标；
- read bytes：对 schema/edge-type/budget-small 有效；对 SF100 full semantic/budg-b1024 需要 caveat；
- avg/p99 latency；
- L0 files；
- store bytes；
- import time；
- manifest bytes；
- correctness mismatch。

## 9. 评估

### RQ1：查询语义 layout 是否减少 L0 read amplification？

SF30 上，naive L0 scan 需要 602,946 个 candidate L0 segments，read bytes 为 8.04 GB。语义布局显著减少候选段和读取字节：

| Variant | L0 signal | candidate L0 | read bytes | vs naive | correctness |
|---|---|---:|---:|---:|---|
| naive | none | 602,946 | 8.04 GB | 0% | N/A |
| lsmgraph-style | key/range only | 602,946 | 8.04 GB | 0% | N/A |
| label-only | source label | 429,753 | 8.00 GB | 0.4% read reduction | N/A |
| degree-only | degree class | 708,853 | 8.03 GB | 0.1% read reduction | N/A |
| edge-type-only | edge type | 72,413 | 4.70 MB | 99.9% read reduction | 0 mismatch |
| schema | label + edge type | 75,649 | 13.1 MB | 99.8% read reduction | 0 mismatch baseline |
| full semantic | label + edge type + degree | 94,183 | 4.70 MB | 99.9% read reduction | 0 mismatch |
| budgeted SemL0 | benefit-scored | 98,027 | 13.1 MB | 99.8% read reduction | 0 mismatch |
| full-compact | L0 eliminated | 1,196 | 38.6 MB | 99.5% read reduction | SF30 complete; SF100 resource boundary |

结论是：edge type 是最强、跨规模最稳定的信号；单独 label 或 degree 不足以解释收益。SemL0 的 query-semantic metadata 将候选 L0 降低约 8 倍，并在关键变体上保持 exact-proof correctness。

### RQ2：SemL0 在 SF100 是否仍有效？

真实 SF100 主结果如下：

| Variant | read MiB | candidate L0 | L0 files | avg us | source |
|---|---:|---:|---:|---:|---|
| naive@s5000 | 26052.88 | 49,257,601 | 1,703 | 5382.8 | measured |
| schema | 149.71 | 5,929,197 | 3,444 | 4906.2 | measured |
| edge-type-only | 146.70 | 5,899,015 | 3,446 | 4288.8 | measured |
| full semantic | 9994.25 | 7,782,877 | 6,615 | 4731.1 | measured, read_bytes caveat |
| budg-b64 | 146.70 | 6,022,519 | 3,483 | 3658.3 | measured |
| budg-b256 | 146.70 | 6,486,337 | 3,675 | 3681.1 | measured |
| budg-b1024 | 700.07 | 7,676,875 | 4,451 | 4060.6 | measured, read_bytes caveat |

`naive@s5000` 的原始读取量为 27,318,428,688 bytes，即 26,052.88 MiB / 25.44 GiB；同一次运行记录 body_reads=3,122,166、header_reads=1,703、ops=45,000。该行使用较大的 flush/memgraph 设置作为 SF100 同档无语义 L0 基线，避免把 naive 的运行规模与 schema/budgeted 变体混淆。

SF100 的关键发现不是“full semantic 最强”，而是“budgeted SemL0 是最好的实用工作点”。`budg-b64` 与 schema/edge-type-only 具有接近的 read volume 和 L0 file count，同时平均延迟最低。full semantic 因过度分段产生更多 L0 文件和 metadata-cache 抖动，说明完整语义物化在大规模下不是免费上界。

相对 naive，schema 的 read bytes 降低 99.43%、candidate L0 降低 87.96%、平均延迟降低 8.9%；`budg-b64` 的 read bytes 降低 99.44%、candidate L0 降低 87.77%、平均延迟降低 32.0%。这确认了 SF100 上“99%+ read pruning”和约 8 倍 candidate fanout 降低的主张。
SF100 correctness 采用 CSR sampled compare，而不是旧的 `SNB_SKIP_SEM_INDEX=1 neighbor-compare` 路径。schema vs edge-type-only、schema vs semantic、schema vs `budg-b64`、schema vs `budg-b256` 均为 checked=900、mismatches=0，四组比较的 left/right_edges_total 均为 62,431。

### RQ3：budget 是否控制 full semantic 的代价？

SF100 budget sweep 显示：

| Budget | read MiB | L0 files | avg us | 解读 |
|---:|---:|---:|---:|---|
| 0, schema-style | 149.71 | 3,444 | 4906.2 | 强内部基线 |
| 64 | 146.70 | 3,483 | 3658.3 | 安全区，最好延迟 |
| 256 | 146.70 | 3,675 | 3681.1 | 安全区 |
| 1024 | 700.07 | 4,451 | 4060.6 | 开始滑向过分段 |
| infinity, full semantic | 9994.25 | 6,615 | 4731.1 | 过度分段/metadata cache 抖动 |

该结果支持 SemL0 的 budgeted materialization：小预算可以保留 schema/edge-type 的稳定剪枝面，同时避免 full semantic 的 fanout cliff。

### RQ4：反馈式 compaction 是否适应热点变化？

| Phase | before feedback | after feedback | no-feedback |
|---|---:|---:|---:|
| phase A | 30 | 0 | 6 |
| phase B | 30 | 0 | 6 |

`selected_ranges_changed = true`。说明 SemL0 可以在受控负载迁移中将 compaction priority 移到新的热点语义范围。

30 分钟 sustained run 进一步确认这一点：7 个 checkpoint 中，A/B/C 三个热点阶段均被 feedback path 选中。反馈组 compaction 后的最大平均 candidate L0 为 0.0，no-feedback 组为 3.0；7 次 compaction 的 input/output bytes 分别为 2056/1448，记录的 IO write bytes 为 1448。该 run 仍是受控 trace，而非生产级长期自适应证明。

### RQ5：维护和写入代价是多少？

当前 SF30 维护代价摘录：

| Variant | import s | throughput e/s | store bytes | L0 files | manifest bytes | max RSS KB |
|---|---:|---:|---:|---:|---:|---:|
| naive | 1194.17 | 910,966 | 42,818,497,255 | 519 | 355,861 | 2,436,484 |
| schema | 1441.73 | 754,544 | 42,994,664,727 | 1,076 | 736,157 | 54,858,952 |
| edge-type-only | 1404.04 | 774,799 | 42,994,700,749 | 1,078 | 738,467 | 54,168,148 |
| full semantic | 1373.47 | 792,044 | 42,995,492,789 | 2,062 | 1,404,555 | 56,118,188 |
| budgeted SemL0 | 1390.73 | 782,214 | 42,995,280,813 | 1,732 | 1,181,944 | 54,158,872 |
| full-compact | 1961.04 | 554,730 | 79,693,512,242 | 519 | 374,056 | 167,250,336 |

该表说明 SemL0 的收益不是免费的：semantic layout 增加了 L0 files 和 manifest bytes。budgeted SemL0 的价值在于，它比 full semantic 更少 fanout，同时保留接近 schema/full semantic 的读收益。full-compact 是极端读优化点，但 store 接近 2 倍、import 最慢、RSS 最高。

SF100 维护代价补表已完成，输出为 `remote-logs/qslsm-sf100-maintenance-table-20260612/maintenance-table.tsv`：

| Variant | store bytes | manifest bytes | L0 files | import wall | max RSS KB |
|---|---:|---:|---:|---:|---:|
| schema | 141,242,590,040 | 2,364,030 | 3,444 | 1:20:15 | 217,254,732 |
| edge-type-only | 141,242,628,041 | 2,368,319 | 3,446 | 1:18:41 | 217,319,028 |
| full semantic | 141,245,314,584 | 4,518,158 | 6,615 | 1:19:08 | 216,672,136 |
| budg-b64 | 141,242,986,434 | 2,393,463 | 3,483 | 1:18:05 | 216,854,320 |
| budg-b256 | 141,243,140,540 | 2,524,145 | 3,675 | 1:17:38 | 216,335,536 |
| budg-b1024 | 141,243,805,064 | 3,052,991 | 4,451 | 1:21:27 | 215,418,948 |

SF100 上 store bytes 基本稳定在 141.24 GB 左右；主要成本差异体现在 manifest bytes 和 L0 file count。full semantic 的 manifest bytes 达到 4.52 MB、L0 files=6,615；`budg-b64` 则接近 schema/edge-type-only，manifest bytes=2.39 MB、L0 files=3,483。这支持 budgeted SemL0 的成本控制主张。

[[TODO: 补 flush time、compaction rewrite bytes、update throughput。]]
[[TODO: 补 concurrent read/write workload 下的 tail latency 和 stall proxy。]]

### RQ6：外部系统对比

目前外部系统状态：

- LiveGraph：源码和 build 可用；SF10 pipeline 已完成并通过同一边集 count check 与 sampled get_neighbors benchmark；SF100 stream pipeline `livegraph-sf100-stream-20260612` 已完成 scan/convert，当前处于 driver load 阶段。
- Teseo：源码/build 可用，但无向、无 label，需要每 edge type 独立实例或注脚。
- GraphOne：源码/build 可用，但 driver/load path 还未过同一 workload gate。
- LLAMA：可作为 snapshot CSR 定性 baseline。

外部数值进主文前必须满足：

1. 同一 SF10/SF100 边集；
2. 同一 sampled neighbor workload；
3. sampled correctness；
4. load time、RSS、memory/store footprint、avg/p50/p99 latency；
5. 明确不跨系统比较 `read_bytes`，因为 read bytes 是 LSM 内部指标。

LiveGraph SF10 已锁定的外部 baseline 证据：

- `remote-logs/livegraph-sf10-20260612/livegraph-sf10.json`
- `remote-logs/livegraph-sf10-20260612/livegraph-footprint.tsv`
- scan_edges=dense_edges=LiveGraph edge_count=355,185,382。
- vertex_count=29,987,835。
- load_s=1502.59，driver wall=27:17.02，peak_rss_kb=47,908,592。
- footprint：block=37,580,963,840 bytes，wal=1,073,741,824 bytes。

SF100 `livegraph-sf100-stream-20260612` 已完成 scan 和 dense 转换：directed_edges=3,570,968,680，vertex_count=282,637,871，dense edge list=72,750,684,584 bytes。但 LiveGraph driver 在 load 阶段出现多天级尾部：2026-06-12 20:44-20:47 采样显示 RSS 约 320 GB，fd 偏移约 59.08 / 72.75 GB，按近期 fd 速度线性外推约 17-21 天。该 run 于 2026-06-12 20:54 经确认终止，`livegraph.stderr` 记录 wall=5:09:56、max RSS=320,174,300 KB，未生成 `livegraph-sf100-stream.json`、`livegraph-footprint.tsv` 或 `DONE`。因此 SF100 LiveGraph 不作为完成数值进入主表，而记录为 external baseline 的 scalability/time boundary。

当前外部系统入表口径：LiveGraph 是主数值外部 baseline，因为它支持有向 edge label 并已跑通同一 typed-neighbor workload。Teseo/GraphOne/LLAMA 先作为相关工作或附录定性对照；只有补齐同一边集、同一 sampled typed-neighbor workload、correctness check、load/RSS/footprint/latency 后，才进入主数值表。

## 10. 讨论

### 10.1 性能是否已经足够？

如果只看内部 SemL0 论文主张，当前性能已经足够支撑一篇有研究点的系统论文草稿。原因是：

- SF30 和 SF100 都显示 query-semantic layout 能大幅降低 candidate L0 fanout；
- edge_type/schema 信号跨规模稳定；
- SF100 暴露了 full semantic 过分段退化，使 budgeted materialization 的必要性更强；
- `budg-b64` 在 SF100 上达到接近 schema/edge-type 的 read volume、接近 schema 的 L0 files，并取得最低 avg latency；
- correctness 和 feedback 机制有代码与测试支撑。

但是，如果按顶会标准，当前性能证据还不够完整。短板不是核心机制无效，而是证据面不够全：

- naive@s5000 已完成，SF100 无语义基线已锁定为 read_bytes=27,318,428,688（26,052.88 MiB / 25.44 GiB）、candidate L0=49,257,601、L0 files=1,703、avg=5382.8 us；
- 外部系统 baseline 已有 LiveGraph SF10 数值，SF100 LiveGraph 仍在运行，尚未形成可进入主表的最终数值；
- 维护代价、写放大、tail latency、concurrent workload 还不充分；
- read_bytes 对 full semantic/budg-b1024 有 reader over-read caveat；
- 本地论文口径还未同步远端 Linux 最新结果。

因此判断是：

```text
当前性能足够支撑“SemL0 方向成立、内部消融强”的论文版本；
还不足以直接说“顶会实验已闭环”。
```

### 10.2 SF100 read_bytes caveat

SF100 上 full semantic 和 budg-b1024 的 read_bytes 偏高，根因是 metadata cache 容量和 reader offset-array 读粒度：

- schema L0 files = 3444，小于 cache 4096；
- full semantic L0 files = 6615，大于 cache 4096；
- reader 当前可能为一个 source 读取整段 offset array；
- OS page cache 缓解 latency，但 `read_bytes` 被放大。

主文应避免用这些变体的 read_bytes 做强结论。主指标应是 candidate L0、latency、L0 files 和维护成本。可选修复是实现 precise offset read，然后只重跑 semantic 和 budg-b1024。

## 11. 相关工作

LSM/KV 系统研究 compaction、Bloom filter、key-value separation 和 write amplification，但通常不把 property-graph query signature 作为 L0 segment metadata 的一等输入。

动态 graph store 如 LiveGraph、Teseo、GraphOne 和 LLAMA 关注动态图更新、事务、内存布局或 snapshot CSR。SemL0 与它们互补，关注的是 LSM delta/L0 层如何利用 label、edge type、degree、property 和 schema epoch 做安全的读时剪枝与维护。

自适应物理设计和 adaptive indexing 证明了访问路径可以随 workload 改变而逐步调整。SemL0 将该思想用于 graph LSM L0 segment：反馈不是调整逻辑查询计划，而是选择高 read-amplification 的语义范围进行物理重写。

schema evolution 研究强调旧数据在 schema 变化后的可解释性。SemL0 将该原则应用到 segment-level pruning：旧段保留 schema epoch，未知元数据保守读取，lazy compaction 修复性能而非保证正确性。

## 12. 局限

1. 当前 external numeric baseline 尚未闭环。LiveGraph SF10 已通过本地 reproducibility gate；LiveGraph SF100 仍在运行。Teseo/GraphOne/LLAMA 只能在通过同等 gate 后进入主数值表。
2. 当前 feedback 证据已包含 30 分钟受控 workload-shift trace，但仍不是生产长期自适应证明。
3. 当前维护代价表仍偏 proxy，缺少完整 write-stall、flush p99、concurrent read/write tail latency。
4. SF100 full semantic/budg-b1024 的 read_bytes 受 reader over-read 影响，需要注脚或修复后重跑。
5. SemL0 不声明完整 schema migration。rename/drop/type-change physical migration、range/string/compound predicates、SQL null semantics 是 future work。
6. full-compact@SF100 当前受 OOM/streaming merge 完成度影响，应作为极端 baseline 的边界讨论。

## 13. 结论

SemL0 表明，属性图查询语义可以成为 LSM 动态图存储的物理设计输入。通过将 query signature 写入 L0 segment metadata、使用 exact-proof candidate pruning、采用 budgeted materialization，并用 feedback compaction 维护热点语义范围，SemL0 在保持保守正确性的同时减少了 L0 read amplification。

SF30/SF100 结果进一步说明，最重要的不是盲目追求完整语义细分，而是在 schema-style 稳定剪枝面和 full semantic 高 fanout 之间选择可控工作点。`label + edge_type` 是跨规模稳定主因；budgeted SemL0 在 SF100 上避免 full semantic 的过分段退化，并取得最好的平均延迟。这是本文最应该强调的研究结论。

[[TODO: 最终版结论补入 LiveGraph baseline 和维护代价补表后的定量一句话。]]
