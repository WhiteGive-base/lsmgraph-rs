# SemL0 CIDR 定位

## 建议标题

**Query Signatures as a Storage Control Plane for Dynamic Property Graphs**

备选：

- **Query-Semantic Control Planes for LSM-Based Dynamic Property Graphs**
- **Making Graph Query Semantics a First-Class Signal in LSM Storage**

不建议继续用：

- Query-Semantic Lifecycle Management for LSM-Based Dynamic Property Graphs

原因：`lifecycle management` 容易被读成工程管理或 compaction policy，CIDR 读者更关心系统架构思想。`storage control plane` 更能表达：query semantics 不只是优化项，而是控制存储行为的信号。

## 一句话 thesis

Property-graph query signatures should not stop at the query layer; they should become a storage-level control plane that shapes segment metadata, pruning, physical rewrite, and correctness fallback in mutable LSM graph stores.

中文：

属性图查询签名不应只停留在查询层；它应成为可变 LSM 图存储中的存储控制面，统一影响段元数据、剪枝、物理重写和正确性回退。

## 创新点

### 1. Query signature 下沉为 storage control plane

传统图存储通常优化 adjacency layout、update path、transaction 或 analytics。SemL0 的设计点是把 source label、edge type、direction、degree、property predicate、schema epoch 等查询语义变成 storage metadata 和 rewrite policy 的输入。

### 2. Exactness-aware pruning contract

SemL0 不是激进跳过 segment，而是维护 `Exact / Conservative / Unknown` 三态。只有 exact disjointness 或 exact absence 才剪枝；不确定就读。这让优化具有数据库系统可接受的 no-false-negative 边界。

### 3. Semantic-aware physical rewrite

Naive compaction 会把 exact semantic partitions 合并成 mixed outputs，摧毁 pruning surface。SemL0 的 semantic merge 按 `(src_label, edge_type)` 等语义分区输出，保留或重建 pruning surface。

### 4. Schema/snapshot fallback

SemL0 把 schema epoch、property encoding epoch、tombstone、snapshot visibility 都纳入 pruning contract。它把 schema evolution 下的优化问题写成 correctness problem。

### 5. Budgeted control rather than full materialization

Full semantic materialization 会触发 fanout/RSS cliff。SemL0 的经验是：semantic control plane 必须 budgeted，不能追求 materialize everything。

## CIDR 版贡献写法

- **C1：A storage-level query-semantic control plane.**  
  将 property-graph query signatures 编译成 storage decisions，并用 segment semantic state 实现。

- **C2：A safety contract for semantic pruning in mutable graph stores.**  
  用 exact/conservative/unknown 和 schema/snapshot fallback 保证不漏读。

- **C3：A physical rewrite lesson: compaction can destroy semantics.**  
  用 C2 证明 naive merge 会摧毁 pruning surface，semantic merge 可以保持/重建。

- **C4：Prototype evidence and comparison limits.**  
  在远端原型系统上用 SF100/SF30/C2/W13 展示效果，同时诚实说明外部系统难以 apples-to-apples 对比。

## 不应写的 claim

- 不写 SemL0 是完整图数据库。
- 不写 production deployment。
- 不写 full external SOTA comparison。
- 不写 uniform latency speedup。
- 不写 Aster RocksGraph 是完整 AsterDB/Gremlin benchmark。
- 不写 NebulaGraph 是生产部署或完整 LDBC Interactive benchmark。
- 不写 Teseo/GraphOne/LLAMA/Aspen 数值 baseline 已完成。

## CIDR 摘要草稿

Dynamic property-graph stores expose rich query signatures, including labels, edge types, direction, degree classes, properties, snapshots, and schema epochs. Today these semantics usually stop at the query layer, while LSM-style graph storage continues to organize reads and compaction around key ranges and levels. We argue that query signatures should become a storage control plane for mutable graph stores. SemL0 is a prototype LSM-based graph store that materializes per-segment semantic state at flush time, uses exact evidence for safe pruning, preserves or rebuilds semantic pruning surfaces during compaction, and falls back conservatively under schema and snapshot uncertainty. Experiments on a remote prototype deployment show that this control plane reduces candidate segments and read bytes on LDBC SNB up to SF100, preserves semantic surfaces on real SF30 compaction inputs, and maintains no-false-negative behavior across schema/snapshot tests. We also document why existing dynamic graph systems are difficult to compare directly: they optimize transactions, analytics, or layout, but do not expose the same query-semantic control surface.
