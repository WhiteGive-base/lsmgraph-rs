# SemL0：把查询签名作为动态图存储控制平面

中文阅读版 v0.1，2026-06-24。
对应英文主稿：`../en/seml0-cidr-draft.md`。本文件用于自己和导师快速阅读，不是投稿正文。

## 摘要

动态 property graph 的查询天然带有丰富语义：点标签、边类型、方向、度数特征、属性谓词、snapshot 和 schema epoch。但在多数 LSM-style 图存储里，这些语义停在查询层；存储层仍然围绕 key、level、adjacency block 和 size-based compaction 来组织读路径和物理重写。结果是：查询明明知道自己只需要某个 typed neighborhood，存储层却仍然可能扫描很多语义无关的 segment。

SemL0 的核心主张是：**query signatures 不应该只停留在 query layer，而应该成为 mutable graph storage 的 storage control plane。** SemL0 是一个 LSM-based property-graph store 原型，它把查询签名编译为 segment-level semantic state，用 exact evidence 做安全剪枝，在 compaction 中保留或重建 semantic pruning surface，并在 schema、snapshot、tombstone 不确定时保守回退。目标不是做完整图数据库，而是研究一个系统架构原则：动态图存储不应只受 key order 和 level structure 控制，也应受它要服务的 graph query semantics 控制。

远端原型部署上的实验显示：SemL0 在 LDBC SNB up to SF100 上降低 candidate segments 和 read bytes；在真实 SF30 compaction input 上保留 semantic pruning surface；在 schema/snapshot evolution 下保持 no-false-negative 行为。同时，外部系统比较必须诚实：很多动态图系统优化 transaction、structural analytics、multi-version CSR 或 distributed graph service，但它们不天然暴露同一个 property-graph typed-neighbor control surface。

## 1. Introduction：论文要讲的故事

Property graph 查询是语义化的。一个典型查询不是简单问“下一个 key range 在哪里”，而是问：从 `Person` 出发的 `KNOWS` 边、指向 `Forum` 的 membership 边、满足某个属性谓词的 recent messages，或者在 snapshot 下可见的路径。这些访问都带有 query signature：source label、edge type、direction、degree distribution、property access pattern、temporal visibility 和 schema epoch。

但存储系统通常不把这些 signatures 当作物理设计输入。LSM-style graph store 对动态图很有吸引力：更新先进入 mutable/append-friendly component，再通过 flush 和 compaction 进入低层。但存储层通常还是围绕 key order、level structure、adjacency representation 和 size-based rewrite policy 工作。query semantics 在上层解释，storage 本身基本 query-blind。

这个错位会造成一种特别的 read amplification。选择性 graph query 可能知道只有很小一部分 typed neighborhoods 会贡献结果，但存储层如果不能证明某个 segment 语义无关，就必须读它。LSM 布局里这个问题更明显：L0 中原本语义精确的 segments 被 compaction 合并到 lower levels 后，可能变成 mixed physical outputs。一旦 segment 失去精确语义身份，未来查询就必须保守读取它，即使其中大部分内容无关。

因此，这篇 CIDR 版的 provocative thesis 是：

> Query signatures should not stop at the query layer; they should become a storage-level control plane.

SemL0 探索的就是这个架构。它把 property-graph query semantics 编译成 storage-visible metadata 和 rewrite decisions。Segment metadata 记录一个 segment 可能包含哪些 graph neighborhoods。Read path 只有在 absence/disjointness 是 exact 的时候才剪枝。Compaction path 不再把所有物理重写都看作语义中立，而是尽量保留或重建高价值 semantic partitions。Schema、tombstone、snapshot 不确定时，correctness path 保守回退。

这篇文章的贡献可以写成四点：

1. 提出 dynamic property graph 的 storage-level query-semantic control plane，让 graph query signatures 影响 segment metadata、read pruning 和 physical rewrite。
2. 定义 exactness-aware pruning contract：exact 才能剪枝，conservative/unknown 必须读取，避免 false negative。
3. 把 semantic surface retention 提升为 physical rewrite problem：naive compaction 会毁掉 query-relevant evidence，semantic-aware compaction 可以保留或重建 pruning surface。
4. 给出 prototype evidence 和 comparison boundary：LDBC SNB 上降低 candidate/read bytes，暴露 full semantic materialization 的 memory cliff，在真实 compaction input 上保留 semantic surface，并解释为什么很多外部动态图系统只能做 design baseline 而不是直接 apples-to-apples。

## 2. 为什么 Query Signatures 应该进入 Storage

Property-graph query signature 不只是 key range。对 typed-neighbor scan 来说，它可能包含 source label、edge type、direction、degree class、property predicate、snapshot timestamp 和 schema epoch。这些信息本质上是在告诉存储层：哪些 segment 可能有贡献，哪些 segment 可以被证明无关。

现有 LSM-style graph storage 往往只知道 keys、segments、adjacency blocks 或 CSR slices，却不知道一个 segment 是否因为 source label 不对、edge type 不对或 schema epoch 不确定而需要被跳过或保守读取。这是一个控制机会：一个 exact for `(source label, edge type)` 的 segment 不只是物理文件，它也是未来查询剪枝的 proof object。

SemL0 在三个位置使用 query signatures：

- Flush time：记录 segment-level semantic state。Segment 可以是 exact、conservative 或 unknown。
- Read time：只有 exact semantic state 和 query signature disjoint 时才 skip；否则 read。
- Compaction time：决定 physical rewrite 是否应该保留或重建 semantic partitions。减少文件数不一定等于好，因为可能会抹掉未来查询需要的 evidence。

## 3. SemL0 设计概要

SemL0 是一个 LSM-based property-graph store 原型。它使用 LSM-style ingestion 和 CSR-like segment bodies，但每个 segment 额外带有由 property-graph query signatures 产生的 semantic metadata。核心设计原则是：semantic metadata 可以改善 pruning，但 correctness 不能依赖 metadata 一定存在或一定精确。

**Query signatures。** 当前原型主要覆盖 LDBC SNB-style 数据上的 typed-neighbor 和 property-aware reads。Signature 可以表达 source label、edge type、direction、degree class、property predicate class、snapshot 和 schema epoch。不是所有 query 都必须完全 exact；未知部分可以保守处理。

**Segment semantic state。** Segment metadata 描述 segment 可能包含什么。例如，一个 segment 可以 exact for `(Person, KNOWS)`，也可以只 exact for edge type，或因为混合多个 label 而 conservative，或因为 schema uncertainty 而 unknown。这里最重要的是 exactness：只有 exact metadata 才能证明 absence。

**Read pruning。** Read path 把 query signature 和 segment metadata 做匹配。只有当 metadata 能证明该 segment 与查询不重叠时才 skip。这个规则保证 no false negative：SemL0 可能 over-read，但不会因为猜测而漏读。

**Lifecycle control。** SemL0 的区别在于 query signatures 不只是 L0 filter。它贯穿完整物理生命周期：flush 创建 pruning surface，read 消费 pruning surface，feedback 找到有价值的 partitions，compaction 保留或重建它，schema/snapshot logic 决定什么时候必须 downgrade exactness。

## 4. Compaction is Not Semantically Neutral

LSM compaction 通常被看作物理维护操作：合并 runs、移除 obsolete entries、减少 overlap、满足 level-size constraint。评价标准通常是 write amplification、space amplification、对未来 key-range lookup 的影响。对 dynamic property graph 来说，这个视角不完整。Compaction 不只是 rewrite bytes；它也会 rewrite future graph queries 依赖的 semantic surface。

假设 L0 中有一组 query-semantic flush 产生的 graph segments。有些 segment 对 `(source label, edge type)` 是 exact 的：一个只包含 `Person` 的 outgoing `KNOWS`，另一个只包含 `Forum` 相关的 `HAS_MEMBER`，另一个只包含 message-related edges。Typed-neighbor query 可以安全跳过与自己 signature disjoint 的 exact segment，因为这是基于 absence 的证明，不是猜测。

Naive compaction 会毁掉这个性质。如果 compactor 把多个 exact semantic partitions 合并成一个 mixed output segment，字节还是正确的，图内容也没有丢。但 output segment 可能不再能为任何一个 query signature 提供 exact evidence。它的 semantic state 变成 conservative 或 unknown。未来查询必须读它，因为系统不能再证明它无关。也就是说，compaction 可以在保持逻辑图内容正确的同时，毁掉高效读取所需的物理证据。

这个观察改变了 compaction 的角色。Compaction 不应只受 level size 和 key overlap 控制，还应考虑一次 rewrite 是保留、削弱还是重建 semantic pruning surface。SemL0 因此把 compaction 当成 semantic rewrite。当某个 partition 对 workload 有价值且保留成本可接受时，SemL0 输出 semantic-aware partitions，例如按 `(source label, edge type)` 分组。当 exact preservation 太贵或不安全时，SemL0 标记为 conservative，让 read path 回退扫描。

这里 correctness 和 optimization 是分离的。Correctness 不依赖 semantic partitioning 成功。如果 metadata exact，read path 可以 prune；如果 metadata conservative/unknown，read path 读取。这意味着 semantic-aware compaction 是 safe baseline 上的优化，不是危险捷径。最坏情况是剪枝变少，不是 false negative。

C2 实验隔离了这个效果。Controlled inputs 上，naive physical merge 和 semantic-aware merge 处理相同逻辑图内容和查询 workload。真实 SF30 compaction inputs 上，naive merge 会把 exact partitions 折叠成 mixed outputs，而 semantic-aware merge 保留 pruning surface。结论很直接：在 LSM-based property-graph stores 中，physical rewrite policy 不仅决定 write cost，也决定未来是否还有 semantic evidence 可以安全剪枝。

这也是 SemL0 不只是 query-time filter 的原因。如果 semantic metadata 只在 flush 产生，而 compaction 完全忽略它，这个优化会随着 LSM tree 演化而衰减。因此 storage-level query-semantic control plane 必须覆盖完整 lifecycle：flush、read、compaction、schema evolution、tombstones 和 snapshots。

## 5. 不确定性下的正确性

Semantic pruning 最大风险不是少剪枝，而是错剪枝。一个 storage engine 如果因为“可能无关”而跳过 segment，就可能静默漏边。因此 SemL0 把 exactness 作为 storage contract 的一部分。

规则很简单：

- exact metadata 可以证明 disjointness；
- conservative metadata 不能；
- unknown metadata 不能；
- 只有第一种情况可以 skip segment。

Schema evolution、snapshots、tombstones 和 encoding epochs 都是 correctness boundaries。旧 segment 可能在不同 schema epoch 下写入；如果可以 exact mapping，就继续剪枝；如果 alias/drop/encoding change 造成不确定，就保守读取。Snapshot 和 tombstone 也是类似：一个 segment 可能对 label/edge type exact，但对 visibility conservative，此时只能使用仍然 sound 的部分，必要时读取 segment。

重点是：conservative fallback 不是失败，而是让 query signatures 能安全控制 storage 的机制。

## 6. 远端原型部署与实验

SemL0 现在只能写成 remote prototype deployment，不是 production graph database。代码和实验在远端 `/data/WorkSpace/lsmgraph-rs`。实验要回答的是系统问题：control plane 是否降低 read amplification？semantic materialization 什么时候太贵？compaction 是否保留 semantic evidence？外部系统什么时候无法直接比较？

### W6：SF100 read amplification 和 budget tradeoff

LDBC SNB SF100 上，`naive` 和 `kv-lsm` 都检查 49,257,601 个 L0 candidates，read bytes 是 3,461.6 MiB。Semantic variants 把 candidates 降到约 5.9M 到 7.9M，read bytes 降到 642.7 到 820.0 MiB。所有 checked rows 都是 zero correctness mismatches。

同一实验也说明 control plane 必须 budgeted。Full unbudgeted semantic materialization 的 peak import RSS 达到 118.03 GiB，而 budgeted/schema variants 仍接近 naive 的内存范围。这是重要的负面结果：query semantics 很有用，但不能无预算地物化所有语义划分。

### W9：SF30 dynamic mixed read/write 支撑证据

SF30 30 分钟 mixed read/write run，6 个 checkpoints，约 163 q/s，0 writer errors。Latency 不能写成 universal speedup，但动态场景中 signal 很强：1800s 时 schema p99 是 8,740.2 us，semantic p99 是 1,274.0 us。这支持“跨时间保留 query-semantic state 可以保护动态 LSM 布局下的 read path”这个说法。

### C2：compaction surface retention

C2 是这篇 CIDR 版最核心的证据。Controlled experiments 显示，naive merge 会摧毁 exact semantic surfaces，造成 4x 到 6x typed-neighbor read blow-up；semantic-aware merge 保持 read cost flat，并且 zero mismatches。

真实 SF30 compaction input 更关键：1.09B directed edges，40 个 `(source label, edge type)` partitions，528 个 exact L1 segments。Naive compaction 的 semantic retention 是 0.0；semantic-aware compaction 是 1.0。成本可控：semantic-aware write amplification 1.24，naive 1.07。对真实 SF30 post-merge metadata 的 replay 显示，naive 的 weighted candidate-byte proxy 是 6.52x，而 semantic-aware 是 1.00x。

边界必须写清楚：这不是 full end-to-end SF30 body-read workload。Controlled rows 测 full read workload + correctness；real SF30 rows 测 retention、write cost 和 metadata-level candidate replay。

### W13：schema/snapshot correctness

W13 覆盖 conservative fallback：old segment readability、mixed delta across compaction/reopen、alias/drop、encoding epoch、new label exact-vs-mixed pruning。这些测试支撑 correctness claim：SemL0 在 metadata 仍然 exact 时剪枝，在 schema/snapshot 不确定时保守读取。

### 外部 baseline：通过 digest gate 的 measured systems

外部 baseline 现在不是只有 LiveGraph。当前进入数值表的条件是：加载同一 LDBC dense edge set，执行同一 fixed-seed sampled typed-neighbor workload，并且对每个 sampled query 做 count/hash digest correctness。按这个 gate，LiveGraph、Aster RocksGraph、Neo4j Community、TuGraph、NebulaGraph 都已经通过 SF1/SF10；LSMGraph-style 仍是内部 layout row，不写成官方 external artifact。

LiveGraph 是 scope-limited dynamic graph storage baseline。SF1：34,692,699 edges，load 21.155 s，peak RSS 4,850,512 KB；SF10：355,185,382 edges，load 1,457.36 s，peak RSS 47,908,544 KB，footprint 38,654,705,664 bytes。Digest verifier 固定 seed 采样 `(edge_type, src)`，SF1 checked 16,140 / mismatches 0，SF10 checked 32,140 / mismatches 0。主文仍不能写 “we beat LiveGraph”，只能写 typed-neighbor workload 范围内的外部系统证据。

Neo4j Community 是 general property-graph database baseline。我们把所有 SemL0 dense edge types 导入为独立 outgoing relationship types（例如 `E_Px` / `E_Nx`），避免把负向 edge type 错写成普通 incoming traversal。SF1 checked 1,700 / mismatches 0，all-types avg/p50/p90/p99 是 180,922.681/2,899.281/43,831.280/3,458,485.918 us。SF10 checked 1,700 / mismatches 0，all-types avg/p50/p90/p99 是 1,974,806.086/3,442.775/70,662.558/34,522,340.128 us。SF10 的高尾延迟主要来自 negative/high-fanout edge types，sampled workload 中 negative neighbors 返回 84,017,646 个。

TuGraph 是 embedded graph database baseline。我们使用 TuGraph runtime image 的 embedded C++ typed-neighbor driver。SF1 loaded 3,181,724 vertices / 34,692,699 edges，checked 1,700 / mismatches 0，avg/p50/p90/p99 是 420.925/6.358/151.398/17,602.600 us。SF10 loaded 29,987,835 vertices / 355,185,382 edges，checked 1,700 / mismatches 0，avg/p50/p90/p99 是 3,775.450/12.630/633.490/158,870 us。这个结果可以进入数值表，但必须说明它是 embedded API typed-neighbor baseline，不是完整 TuGraph LDBC Interactive benchmark。

Aster RocksGraph 是 LSM-adjacent typed-neighbor bridge baseline。我们使用 `NTU-Siqiang-Group/Aster` 轻量实现，checkout commit 是 `6abb258e577c479325092a8ac0e7691fdfd154c2`。driver 把 `(edge_type, src)` 映射成 compact logical vertex id，避免稀疏大 id 触发 MorrisCounter 内存放大。SF1 checked 1,700 / mismatches 0，avg/p50/p90/p99 是 150.985/10.680/381.904/3,411.36 us。SF10 checked 1,700 / mismatches 0，avg/p50/p90/p99 是 1,408.270/580.529/1,061.240/15,809.60 us。这个结果可以进数值表，但不能写成完整 AsterDB/Gremlin benchmark。

NebulaGraph 是 distributed open-source graph DB baseline。我们使用 NebulaGraph v3.8.0 官方 server images；因为远端 Docker Hub 直连受限，镜像通过 `docker.1ms.run` 拉取并 retag。导入模型是每个 dense edge type 建一个 nGQL edge type。SF1 checked 1,700 / mismatches 0，avg/p50/p90/p99 是 61,553.966/581.010/10,736.324/1,189,334.146 us。SF10 checked 1,700 / mismatches 0，avg/p50/p90/p99 是 744,146.618/741.524/20,218.339/14,228,169.276 us。这个结果可以写成同 workload 图数据库 baseline，但不能写成生产部署或完整 LDBC Interactive benchmark。

## 7. 与现有系统的比较

SemL0 和动态图存储布局、transactional graph store、multi-version CSR、dynamic graph analytics container 都有关，但它的接口和 claim 不完全一样。

LiveGraph 是最接近 typed-neighbor scan 的 measured external system，因为它能跑 adjacency/typed-neighbor scan。Aster RocksGraph 给了 LSM-adjacent 的 typed-neighbor bridge 结果。Neo4j、TuGraph 和 NebulaGraph 则把比较扩展到主流 property-graph database 实现。但这些系统都不是 SemL0 的同类 storage-control-plane 系统，所以必须保留 workload 和 digest gate 边界。

LSMGraph、BACH 更接近 LSM/layout 主题，适合 qualitative comparison。它们可以用来对比 multi-level CSR、LSM graph layout、adjacency/CSR transformation。SemL0 的区别不是又提出一个 layout，而是让 query signatures 作为 storage control plane 影响 pruning 和 physical rewrite。

Teseo、GraphOne、LLAMA、Aspen 是重要动态图系统，但 workload/API 与 SemL0 不直接匹配。LLAMA 偏 analytics；Teseo/GraphOne 偏 dynamic graph container 和 structural analytics；Aspen 偏 low-latency graph streaming。它们应该进 design matrix 和 related work，不应硬塞进 latency baseline。

ByteGraph、BG3、Galaxybase、GES、Nebula Graph 适合作 industry/system paper 的写法参考，例如 motivation、deployment framing、lessons learned。这里的 NebulaGraph 数值只是 same-workload typed-neighbor baseline，不代表 SemL0 有 production deployment 或能直接和这些系统做 production-scale apples-to-apples。

## 8. Experience and Lessons Learned

**经验 1：query semantics 必须和 exactness discipline 绑定。** Semantic pruning 最大风险是错剪枝。SemL0 的 exact/conservative/unknown 模型让每次 skip 都变成 proof obligation。

**经验 2：semantic materialization 必须有预算。** SF100 证明 semantics 能显著减少 candidates 和 read bytes，但 full semantic materialization 会造成 memory cliff。实际系统应该把 query signatures 当作 budgeted control plane。

**经验 3：compaction 会抹掉 flush 创造的 evidence。** C2 是故事中心。如果 compaction 忽略 semantics，query-semantic flush 的收益会随着 LSM tree 演化而衰减。

**经验 4：conservative fallback 是设计机制，不是补丁。** Schema evolution、tombstones、snapshots 在 mutable graph storage 中不是罕见 corner case。SemL0 用 fallback 把不确定性的影响限制为多读，而不是漏读。

**经验 5：外部系统比较很难，除非问题定义一致。** 很多系统是 transaction、analytics、CSR snapshot 或 distributed graph service 的强 baseline，但不暴露 SemL0 研究的 property-graph typed-neighbor interface 和 semantic rewrite control。CIDR 比纯 benchmark paper 更合适，因为贡献是系统架构原则和工程经验。

## 9. 局限和下一步

SemL0 是 prototype，不是 production graph database。Deployment evidence 是远端实验部署，不应写成 production deployment、完整 query engine 或全面替代已有动态图系统。

Latency improvement 是 workload-dependent。最强 claim 是 read-amplification reduction、semantic surface retention 和 conservative correctness。Latency 只作为 supporting evidence，尤其是 W9 SF30 dynamic run。

C2 real SF30 是三层证据：controlled full-read workload + correctness、real SF30 retention/write cost、real SF30 metadata replay。不能写成 full SF30 body-read execution trace。

LiveGraph、Aster RocksGraph、Neo4j Community、TuGraph、NebulaGraph 都已有 SF1/SF10 measured evidence，并且 digest correctness 已通过。Verifier 固定 seed 采样 `(edge_type, src)`，用 dense edge-list truth 校验 neighbor counts/hashes，数值表中的 rows 都是 0 mismatches。Aster 和 NebulaGraph 仍要保持 scope-limited 口径；如果要进一步加强 artifact story，可以再做独立 clean-checkout / clean-image reproduction。

## 10. Conclusion

SemL0 的主张是：query signatures 应该成为 dynamic property graph 的 storage control plane。核心不是多加一个 query-time filter，而是让 graph semantics 在 flush、read、compaction、schema evolution、tombstones 和 snapshots 全生命周期可见。远端原型显示，这个 control plane 可以降低 read amplification，暴露 budgeted semantic materialization 的必要性，在 compaction 中保留 semantic pruning surface，并在 correctness uncertainty 下保守退化。更大的系统经验是：在 mutable graph storage 中，physical lifecycle policy 不只决定 bytes 放在哪里，也决定未来查询是否还保留避免读取这些 bytes 的 evidence。

## 投稿前 TODO

- 如 CIDR 投稿要求 LaTeX，需要把英文 Markdown 转成 CIDR LaTeX template。
- 补 references，并把 related work 收紧。
- 画 control-plane lifecycle 图：query signature -> flush metadata -> read pruning -> semantic compaction -> schema/snapshot fallback。
- 画 C2 图：naive vs semantic merge 的 exact-surface retention 和 write amplification。
- LiveGraph、Aster RocksGraph、Neo4j、TuGraph、NebulaGraph 的 digest correctness 已实现并更新 external baseline 表；后续只需保持 scope-limited 表述。
- 决定五个 measured external baseline 数字放主 evaluation 还是放 artifact/comparison sidebar。
