# SemL0: 面向 LSM 动态属性图的查询语义 L0 物理设计

SemL0: Query-Semantic L0 Design for LSM-Based Dynamic Property Graphs

本文是当前阶段的 milestone paper draft，不是最终 SIGMOD submission。事实源来自 Linux 仓库 `/data/WorkSpace/lsmgraph-rs`，当前读取到的分支为 `codex/sf100-basegraph-bench`，HEAD 为 `ab24ce4e2a40e7d8d342a1498ae6ed1698bc9e36`。本文只整理现有 markdown、TSV、JSON 摘要和日志，不运行新的重实验。

## 0. 里程碑说明

这个版本的目标是冻结当前论文故事并整理证据边界。当前证据支持 SemL0 的研究方向和内部 layout 消融：查询语义可以安全地下推到 LSM L0 物理层，并通过 exact-proof pruning 降低 L0 read amplification。当前证据还不构成完整顶会评测闭环。

需要继续补齐的部分包括：外部系统数值基线、真实 KV/RocksDB-style measured baseline、混合读写稳态、SF100 重复运行与缓存控制、RSS/degree-directory 修复后的实测、property-aware workload，以及 full semantic/budg-b1024 在 precise offset read 后的 read_bytes 复测。

## 1. 摘要

LSM-based dynamic graph stores 适合高频写入，因为更新可以先进入内存 delta，再批量刷成不可变的 CSR-like L0 segment。然而 L0 segment 在 key/source 空间上重叠，查询邻居或 typed-neighbor 时需要探测大量近期 segment，从而产生 L0 read amplification。属性图查询天然携带 source label、edge type、direction、degree class、property presence 和 schema epoch 等语义约束，但传统 LSM L0 物理层通常只看到 key/range 或 source 范围。

SemL0 将这些查询约束抽象为存储层可见的 `GraphAccessSignature`，并在 L0 segment metadata 中记录 exact 或 conservative 的语义摘要。读取时，SemL0 只有在元数据可以证明查询签名与 segment 不相交、或证明所需属性确切不存在时才裁剪；遇到 mixed、unknown、legacy、tombstone-sensitive 或 schema-uncertain 状态时保守读取。因此，SemL0 的语义裁剪是 exact-proof pruning，而不是启发式跳过。

同时，完整语义物化并不是免费上界。按 `(src_label, edge_type, degree)` 细分 L0 可以扩大剪枝表面，但也会增加文件数、manifest 元数据、metadata-cache 压力和 reader over-read 风险。SemL0 因此采用 benefit-scored budgeted materialization，并结合 feedback-driven semantic compaction，让语义物化和维护优先级随查询热点移动。当前 SF30/SF100 结果显示 SemL0 显著降低 candidate L0 和 read amplification；latency、read_bytes、RSS 和外部 baseline 仍需要后续复测与修复后确认。

## 2. 引言

动态属性图同时要求持续写入和语义读路径。社交、推荐、交易和知识图谱会频繁插入、删除或更新边与属性，同时执行按 label、edge type、方向、属性和快照过滤的邻居查询。LSM 结构将写入聚合到内存，再批量刷新为不可变 segment，能够降低写放大；但 L0 segment 在近期更新区域高度重叠，读路径需要检查多个候选段，L0 read amplification 成为主要成本。

已有 graph LSM 或动态图系统通常从 key order、topology、vertex range、degree、CSR/adjacency conversion 或 compaction 策略出发设计物理布局。这些维度重要，但它们没有把 property-graph query signature 作为 L0 物理设计的一等输入。SemL0 的核心观察是：属性图查询语义不应只存在于查询层，它应该指导 L0 flush layout、segment metadata、read-time pruning 和 compaction priority。

当前里程碑版本的贡献定位如下。

1. Exact-proof query-semantic L0 physical design：将 source label、edge type、direction、degree class、property presence 和 schema epoch 编码进 L0 segment metadata，并只在可证明无交集或确切缺失时裁剪。
2. Budgeted semantic materialization：在 schema-style 和 full semantic 之间用收益/成本分数选择语义物化粒度，避免 full semantic fanout cliff。
3. Feedback-driven semantic compaction：记录 query count、candidate segments、filter-passed segments、offset/cache miss 和 rewrite cost，选择热点语义范围做 targeted compaction。
4. Snapshot/schema/tombstone-safe pruning contract：把 schema epoch、property encoding、tombstone 和 snapshot visibility 纳入裁剪证明。
5. 当前 Linux 评测证据：在 LDBC SF30/SF100 上整理内部 layout 消融、budget sweep、反馈 microbenchmark、维护成本和 caveat。

本文不声称 SemL0 是完整图数据库，也不声称完整 schema migration 或生产级写停顿安全。本文关注一个更窄的问题：查询语义如何安全地塑造 LSM 动态属性图的 L0 物理设计。

## 3. 背景与问题

LSM 动态图存储通常由内存 MemGraph/delta buffer、不可变 CSR-like L0 segment 和后台 compaction 组成。写入路径将更新追加到内存；flush 时将当前 delta 编码为紧凑 segment；compaction 再把多个 segment 合并为更稳定的层级。这个设计对写入友好，但 L0 segment 之间存在大量重叠。一次 typed-neighbor 查询可能需要先枚举多个 L0 文件，再经过 source range、SourceBloom 和 CSR body read 才能返回可见邻居。

属性图查询通常带有比 source id 更丰富的语义：source label、edge type、direction、degree hint、required property、schema epoch 以及 snapshot/tombstone 约束。单纯的 key-range 或 source-range metadata 无法表达这些约束。例如，查询 `Person -[knows]-> ?` 与 `Person -[likes]-> ?` 可能落在相同 source range 中，但 edge type 完全不同；只靠 source range 无法安全跳过无关 segment。

SemL0 的安全契约是：

> Only prune when metadata proves disjointness or exact absence; otherwise read conservatively.

也就是说，metadata 的失败模式只能是多读，不能是漏读。unknown、mixed、legacy、tombstone-sensitive 或 schema-uncertain 状态一律保守保留。

## 4. 系统总览

SemL0 由四个循环组成。

写入和 flush 循环：更新进入 MemGraph，flush 时根据 `--l0-layout` 选择 naive、schema、semantic、semantic-budgeted 等策略，将边分组写为 CSR-like L0 segment。每个 segment 携带 source label、edge type partition、degree class、summary completeness、schema epoch、property summary 和 tombstone summary。

读取循环：查询被编译为 `GraphAccessSignature`。读路径先通过 semantic L0 index 找候选 segment，再执行 `may_contain_signature()`、source range、SourceBloom、CSR reader 和 visible merge。只有 exact metadata 可以证明不匹配时，segment 才被跳过。

维护循环：系统记录 read-amplification feedback，包括 query count、candidate segments、filter-passed segments、offset/cache miss、body reads 和 rewrite estimate。feedback compaction 选择热的 `(src_label, edge_type, source range)` 进行 targeted rewrite。

安全循环：schema catalog、property encoding epoch、snapshot visibility 和 tombstone semantics 共同约束裁剪逻辑。旧 segment 保留自己的 schema epoch；schema 变化影响未来写入和查询解释，但不会把旧 metadata 解释成更强的裁剪证明。

## 5. Query-Semantic L0 Physical Design

`GraphAccessSignature` 是查询语义在存储层的表示。当前设计覆盖 source label、edge type、direction、degree class、required property/property predicate、schema epoch 和 snapshot/tombstone 相关状态。SemL0 将这些签名与 L0 segment metadata 对齐，使读路径可以在 segment 粒度上执行安全裁剪。

Segment metadata 的状态机如下。

| Metadata state | 读路径动作 |
|---|---|
| exact match / maybe overlap | 保留并继续过滤 |
| exact disjoint | 裁剪 |
| exact absent | 裁剪 |
| mixed | 保守保留 |
| unknown | 保守保留 |
| legacy | 保守保留 |
| tombstone-sensitive | 保守保留 |
| schema-uncertain | 保守保留 |

这个规则强调的是证明，不是概率。SemL0 不使用“看起来不相关”的启发式跳过；它只在 metadata 足以证明无交集或确切缺失时裁剪。

当前 `--l0-layout` 设计空间包括：

| Layout | 语义信号 | 作用 |
|---|---|---|
| naive | 无语义布局 | 无语义基线 |
| lsmgraph-style | key/range only | 传统 LSM-CSR 风格 |
| label-only | source label | 检查 label 信号 |
| edge-type-only | edge type | 检查 edge type 信号 |
| degree-only | degree class | 检查 degree 信号 |
| schema | `src_label + edge_type` | graph-aware 强内部基线 |
| semantic | `src_label + edge_type + degree` | full semantic 设计点 |
| semantic-budgeted | benefit-scored SemL0 | 当前主策略 |
| full-compact | L0-to-L1 compact | 极端读优化基线 |

这些消融使用同一引擎、同一数据和同一 sample plan，目的不是比较不同系统工程实现，而是隔离 query-semantic metadata 对 L0 candidate fanout 的影响。

## 6. Budgeted Semantic Materialization

Full semantic layout 将 `(src_label, edge_type)` 进一步按 degree class 细分。它可能提供更细的剪枝表面，但也会带来更多 L0 files、manifest entries、metadata-cache pressure 和 reader offset-array over-read 风险。当前 SF100 中，full semantic 的 L0 files 为 6,615，而 schema 为 3,444；full semantic 的 read_bytes 受 reader over-read caveat 影响，不能作为强结论。

Budgeted SemL0 将 full semantic 视为一个设计端点，而不是默认最优点。策略包含两个层次。

1. Edge-type level selection：根据 benefit/cost score 选择值得保留精确语义分区的 edge type。
2. Degree-class exactness：只有当收益足以覆盖额外文件数和 metadata 成本时，才保留 exact degree class。

一个关键规则是：未被预算选中的 edge type 不应塌缩为 `MIXED_EDGE_TYPE`，而应保留真实 edge type，只把 degree 合并为 Mixed。这样 budget 0 接近 schema-style，预算增加时逐步接近 full semantic，同时不会丢掉 schema 已经具备的 edge-type pruning。

当前主要结论是：语义是有用的，但完整语义物化不是免费上界。SemL0 的价值在于选择可控工作点。

## 7. Feedback-Driven Semantic Compaction

传统 LSM compaction 主要优化 level size、key overlap 和 write amplification。SemL0 将维护目标扩展到 semantic range：如果某些 `(src_label, edge_type, source range)` 在近期查询中产生高 candidate fanout、高 filter-passed segments 或高 offset/cache miss，就优先重写这些范围。

当前 feedback 指标包括 query count、candidate segments、filter-passed segments、offset cache miss、body reads、estimated rewrite bytes、compaction input/output bytes 和 compaction latency。选择逻辑仍应保持保守：feedback 改变物理布局和维护优先级，不改变查询正确性契约。

受控 workload-shift microbenchmark 显示，phase A/B 的热点范围变化后，feedback compaction 可以把热点查询的 candidate L0 从 30 降到 0；no-feedback 基线仍保留 6 个候选段。补充的 30 分钟 sustained feedback trace 有 7 个 checkpoint，`selected_ranges_changed=true`，反馈组 compaction 后的最大平均 candidate L0 为 0.0，no-feedback 组为 3.0；7 次 compaction 的 input/output bytes 为 2056/1448，记录的 IO write bytes 为 1448。

这说明 feedback path 在受控 trace 中能跟随热点，但它还不是生产长期自适应稳定性的证明。

## 8. 正确性：Snapshot、Tombstone、Schema Epoch

语义裁剪在动态图更新下容易出错。schema 可能演进，property encoding 可能变更，旧 segment 可能包含 tombstone，查询也可能在 snapshot 边界上读取历史可见状态。SemL0 的设计原则是：所有不确定性都导致保守保留。

Lemma 1: Conservative metadata invariant。对任意 segment，若 metadata 声称某个 signature 维度 exact absent 或 exact disjoint，则该声明必须覆盖所有在当前支持模型下可能可见的 matching records。若该条件不能满足，metadata 状态必须是 mixed、unknown、legacy、tombstone-sensitive 或 schema-uncertain。

Lemma 2: Safe pruning invariant。读路径只在 Lemma 1 的 exact absent 或 exact disjoint 情况下裁剪 segment；其他状态均保留。因此，被裁剪的 segment 不包含当前 query signature 下可见的匹配记录。

Theorem: No false negatives under supported schema/tombstone/snapshot model。在 additive schema、固定宽度 property equality、受支持的 tombstone/snapshot 边界内，SemL0 的 segment-level pruning 不会漏掉可见匹配记录。证明来自 Lemma 1 和 Lemma 2：所有可见匹配要么位于未被裁剪的 segment 中，要么 metadata 必须能证明其不存在；后者与存在可见匹配矛盾。

当前非目标包括完整 rename/drop/type-change physical migration、range/string/compound predicates、SQL null semantics，以及完整图数据库事务语义。

## 9. 评测

当前评测口径必须保守。candidate L0、body reads、L0 files 和 read amplification 是当前最稳定的证据；latency 仍需 warm-up、repeats、cache-state control 后作为强结论；full semantic 和 budg-b1024 的 read_bytes 受 reader over-read caveat 影响。

### RQ1：查询语义布局是否降低 SF30 L0 read amplification？

SF30 上，语义布局显著降低候选 L0 和读取字节。edge type 是当前最强、跨规模最稳定的单一信号。

| Variant | L0 signal | candidate L0 | read bytes | vs naive | correctness |
|---|---|---:|---:|---|---|
| naive | none | 602,946 | 8.04 GB | 0% | N/A |
| lsmgraph-style | key/range only | 602,946 | 8.04 GB | 0% | N/A |
| label-only | source label | 429,753 | 8.00 GB | 0.4% read reduction | N/A |
| degree-only | degree class | 708,853 | 8.03 GB | 0.1% read reduction | N/A |
| edge-type-only | edge type | 72,413 | 4.70 MB | 99.9% read reduction | 0 mismatch |
| schema | label + edge type | 75,649 | 13.1 MB | 99.8% read reduction | 0 mismatch baseline |
| full semantic | label + edge type + degree | 94,183 | 4.70 MB | 99.9% read reduction | 0 mismatch |
| budgeted SemL0 | benefit-scored | 98,027 | 13.1 MB | 99.8% read reduction | 0 mismatch |
| full-compact | L0 eliminated | 1,196 | 38.6 MB | 99.5% read reduction | SF30 complete; SF100 resource boundary |

### RQ2：SemL0 是否扩展到 SF100？

SF100 `naive@s5000` 基线已经完成，同档主表如下。

| Variant | read MiB | candidate L0 | L0 files | avg us | source |
|---|---:|---:|---:|---:|---|
| naive@s5000 | 26052.88 | 49,257,601 | 1,703 | 5382.8 | measured |
| schema | 149.71 | 5,929,197 | 3,444 | 4906.2 | measured |
| edge-type-only | 146.70 | 5,899,015 | 3,446 | 4288.8 | measured |
| full semantic | 9994.25 | 7,782,877 | 6,615 | 4731.1 | measured, read_bytes caveat |
| budg-b64 | 146.70 | 6,022,519 | 3,483 | 3658.3 | measured |
| budg-b256 | 146.70 | 6,486,337 | 3,675 | 3681.1 | measured |
| budg-b1024 | 700.07 | 7,676,875 | 4,451 | 4060.6 | measured, read_bytes caveat |

相对 naive，schema 和 small-budget SemL0 都把 candidate L0 从 49,257,601 降到约 5.9M 到 6.5M。当前结果支持“query-semantic layout 大幅降低 L0 fanout”和“full semantic 过度分段不是免费上界”。`budg-b64` 的平均延迟最低，但在重复运行和缓存控制前只能作为 preliminary observation。

### RQ3：预算是否控制 full semantic 成本？

| Budget | read MiB | L0 files | avg us | 解读 |
|---|---:|---:|---:|---|
| 0, schema-style | 149.71 | 3,444 | 4906.2 | 强内部基线 |
| 64 | 146.70 | 3,483 | 3658.3 | 小预算安全区，latency 仍需复测 |
| 256 | 146.70 | 3,675 | 3681.1 | 小预算安全区 |
| 1024 | 700.07 | 4,451 | 4060.6 | 开始滑向过分段，read_bytes 有 caveat |
| infinity, full semantic | 9994.25 | 6,615 | 4731.1 | 过度分段/metadata-cache 抖动，read_bytes 有 caveat |

这个 sweep 支持 budgeted materialization 的必要性：小预算接近 schema/edge-type 的稳定剪枝面，同时避免 full semantic 的 fanout cliff。

### RQ4：Feedback microbenchmark

| Phase | before feedback | after feedback | no-feedback |
|---|---:|---:|---:|
| phase A | 30 | 0 | 6 |
| phase B | 30 | 0 | 6 |

`selected_ranges_changed=true`。30 分钟 sustained trace 进一步显示，7 个 checkpoint 覆盖 A/B/C 三个热点阶段，feedback 组 compaction 后最大平均 candidate L0 为 0.0，no-feedback 组为 3.0，7 次 compaction 的 input/output bytes 为 2056/1448。该结果应表述为受控 microbenchmark/trace，不应表述为生产长期稳定性证明。

### RQ5：维护成本

当前 SF30 维护代价摘录如下。

| Variant | import s | throughput e/s | store bytes | L0 files | manifest bytes | max RSS KB |
|---|---:|---:|---:|---:|---:|---:|
| naive | 1194.17 | 910,966 | 42,818,497,255 | 519 | 355,861 | 2,436,484 |
| schema | 1441.73 | 754,544 | 42,994,664,727 | 1,076 | 736,157 | 54,858,952 |
| edge-type-only | 1404.04 | 774,799 | 42,994,700,749 | 1,078 | 738,467 | 54,168,148 |
| full semantic | 1373.47 | 792,044 | 42,995,492,789 | 2,062 | 1,404,555 | 56,118,188 |
| budgeted SemL0 | 1390.73 | 782,214 | 42,995,280,813 | 1,732 | 1,181,944 | 54,158,872 |
| full-compact | 1961.04 | 554,730 | 79,693,512,242 | 519 | 374,056 | 167,250,336 |

SF100 maintenance table 已存在：

| Variant | store bytes | manifest bytes | L0 files | import wall | max RSS KB |
|---|---:|---:|---:|---:|---:|
| schema | 141,242,590,040 | 2,364,030 | 3,444 | 1:20:15 | 217,254,732 |
| edge-type-only | 141,242,628,041 | 2,368,319 | 3,446 | 1:18:41 | 217,319,028 |
| full semantic | 141,245,314,584 | 4,518,158 | 6,615 | 1:19:08 | 216,672,136 |
| budg-b64 | 141,242,986,434 | 2,393,463 | 3,483 | 1:18:05 | 216,854,320 |
| budg-b256 | 141,243,140,540 | 2,524,145 | 3,675 | 1:17:38 | 216,335,536 |
| budg-b1024 | 141,243,805,064 | 3,052,991 | 4,451 | 1:21:27 | 215,418,948 |

注意：RSS 当前受 degree_directory 问题影响，不能强说为稳定内存成本结论。应在修复并复测后再将 RSS 作为主表强指标。

### RQ6：外部 baseline 当前状态

外部数值 baseline 尚未闭环。LiveGraph/Teseo/GraphOne/LLAMA 等系统只有在同一边集、同一 sampled typed-neighbor workload、correctness check、load/RSS/footprint/latency gate 通过后，才能进入主数值表。当前不应把 Python/model-derived `kv-style` 行称为 measured；真实 KV/RocksDB-style baseline 仍是下一阶段必须补齐的对照。

## 10. 讨论

为什么不只是 edge-type partitioning？当前结果确实显示 edge type 是最强信号，但 SemL0 的主张更广：edge type、label、degree、property、schema epoch 和 tombstone/snapshot 状态共同形成可证明安全的存储层 signature。Edge type 是当前最强实验证据，不应被包装成全部贡献。

为什么不 full semantic？SF100 说明 full semantic 产生更多 L0 files 和 metadata/cache 压力，read_bytes 还受到 reader over-read caveat 影响。更细粒度不一定更好，budgeted materialization 才是可扩展设计点。

为什么不 key-prefix KV/RocksDB-style？KV-style 可以把 edge type 编进 key prefix，并可能降低候选范围，但它会改变 CSR 邻接局部性和读路径单位。当前 kv-style 数字是 simulated/model-derived，不能作为 measured baseline。真实 KV-style baseline 必须补跑。

当前结果证明了什么？它支持 query semantics 作为 LSM L0 physical design input 的方向，支持 exact-proof pruning 和 budgeted materialization 的必要性，支持 SF30/SF100 内部消融中的 L0 fanout 降低。

当前结果没有证明什么？它还没有证明生产级动态图数据库完整性、顶会级外部 baseline 闭环、混合读写稳态、property workload 覆盖、稳定 tail latency，或 RSS 修复后的最终成本。

当前 server end-to-end LDBC 只能作为 integration sanity check。主证据仍是 storage-level typed-neighbor kernel。除非实际 server store switching 数据存在，否则不能声明 end-to-end schema vs budg-b64。

## 11. 相关工作定位

BACH、LSMGraph 和 graph-aware LSM 工作关注 topology-level layout、AL/CSR 转换、graph-aware compaction 或 LSM 层级策略。SemL0 与这些工作相邻，但核心边界是 property-graph query signature 成为 L0 segment metadata 和 pruning proof 的物理设计输入。

LSM/KV 系统研究 compaction、Bloom filter、prefix key、key-value separation 和 write amplification，但通常不把属性图查询语义作为 L0 segment-level exact-proof pruning 的一等输入。SemL0 与 KV-style prefix partition 的区别需要通过真实 KV baseline 进一步定量说明。

自适应物理设计和 adaptive indexing 说明访问模式可以反过来调整物理布局。SemL0 将这一思想用于 graph LSM L0 segment：feedback 选择高 read-amplification 的语义范围进行 targeted rewrite，而不是仅调整逻辑查询计划。

Schema evolution 工作强调旧数据在 schema 变化后的解释边界。SemL0 将 schema epoch 纳入裁剪证明：旧 segment 保留写入时的 epoch，unknown/legacy metadata 保守读取。

## 12. 局限

1. 外部 numeric baseline 尚未闭环。
2. Feedback 证据目前是受控 microbenchmark/trace，不是生产长期稳定性证明。
3. 维护和写成本仍不完整，缺少 write-stall、flush p99、concurrent read/write tail latency。
4. SF100 full semantic/budg-b1024 read_bytes 受 reader over-read caveat 影响。
5. Property-predicate workload 仍不足，当前最强证据主要来自 typed-neighbor/edge-type。
6. 混合读写 steady-state 仍缺失。
7. 真实 KV/RocksDB-style measured baseline 仍缺失。
8. RSS/degree_directory 问题需要修复后复测，当前 RSS 不能强解释。
9. SemL0 不声明完整图数据库、完整 schema migration、rename/drop/type-change migration、range/string/compound predicates 或 SQL null semantics。

## 13. 结论

SemL0 显示，属性图查询语义可以成为 LSM L0 physical design input。通过将 query signature 写入 L0 segment metadata、使用 exact-proof pruning、采用 budgeted materialization，并用 feedback compaction 维护热点语义范围，SemL0 在当前 SF30/SF100 内部消融中显著减少 L0 read amplification。

最重要的发现不是“语义布局越细越好”，而是 query-semantic pruning 必须 exact-proof 且 budgeted。当前证据支持这个方向，也明确指出下一阶段需要补齐的实验和系统修复。
