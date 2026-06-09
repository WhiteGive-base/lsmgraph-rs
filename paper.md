# SemL0：面向基于 LSM 的动态属性图的查询语义物理设计

> 摘要占位：这份 source-ready 的 Markdown 草稿保留了当前 SemL0 的核心论题、十章节结构、schema 演进回答以及 claim 边界。待会议、页数预算与最终证据决策获批后，再替换本占位摘要。

## 1. 引言

动态图属性图会被持续更新，但对它的查询却依赖语义谓词：顶点标签、边类型、方向、度条件、属性要求、属性值、快照以及 schema 版本。LSM 风格存储对这类场景很有吸引力，因为它通过顺序 delta 写入与后台 compaction 吸收写入。然而，让写入便宜的同一类 L0 overlap，也会让读取变得昂贵。一次图查询往往会探测到一些 segment，这些 segment 在拓扑上是相关的，但在标签、边类型、属性、快照可见性等语义维度上其实与查询无关。

核心问题在于，传统的 LSM 图存储通常对决定一个 segment 是否可能贡献查询结果的语义信息几乎是“盲”的。它可能知道最近的图更新位于哪里，但并不一定知道某个 segment 是否可能满足一次 `Person` 到 `Person` 的边遍历，某个必须的边类型是否不存在，某个属性谓词是否不可能满足，或者某个 schema / snapshot 边界是否会让摘要无法安全用于剪枝。这种不匹配会把语义上的无关性转化成读放大。

SemL0 围绕一个简单论点构建：

```text
Property-graph query semantics should be first-class inputs to LSM physical
layout, compaction, and metadata safety.
```

SemL0 将面向存储的图访问模式表示为查询语义签名。一个签名刻画对剪枝与布局选择真正重要的维度：标签、边类型、方向、度类别、属性存在性或属性值要求、快照可见性以及 schema epoch。写路径把语义摘要附着到 CSR 风格的 L0 segment 上。读路径只有在这些摘要能够证明不相交或证明缺失时才会使用它们。未知、混合、遗留、或对 tombstone 敏感的元数据仍然保留在候选集合中。

这条“精确证明”规则是将系统约束住的安全合约：

```text
metadata may reduce the candidate set only when it proves that a segment cannot
contain a visible match.
```

在这个合约之上，SemL0 发展出的是一条统一的存储链路，而不是一组彼此无关的特性：

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

C1 让图访问签名对 segment 布局与候选剪枝变得可见。基于收益评分的 materialization 在预算受限的条件下保持这些布局的选择性。P3 的反馈驱动语义 compaction 在运行时计数器显示热点语义范围发生变化时，对同一布局进行自适应维护。P2 的 schema-evolution-aware 元数据确保增量式 catalog 变化不会让旧 segment 失去可用性。P4 的 snapshot-correct semantic deltas 则让 tombstone、snapshot 与混合 epoch 仍然服从同一条“无假阴性剪枝”合约。P5 负责打包证据和开放 gate，确保论文 claim 不会跑在 artifact 之前。

最终结果并不是一个通用图数据库功能清单。SemL0 是一种面向存储的设计，它关注的问题是：图查询语义应如何塑造 LSM delta 区域及其 compaction 策略。这个范围是有意收紧的。当前 artifact 只支持：主性能 claim 的 latest-code SF1 证据、受控反馈工作负载迁移证据、针对 schema / snapshot / tombstone 的局部正确性证据、写入 / 重写成本代理记账，以及一个 source-ready 而非 final-submission-ready 的包。它不声称最终的 latest-code SF30 / SF100 性能，不声称生产级 write-stall 安全性，不声称完整的 schema migration 引擎，不声称已完成的持续反馈实验，不声称已完成的存储清理，也不声称已经 final submission ready。

本文的贡献如下：

1. 一种面向动态图属性图的查询语义 LSM 物理设计。SemL0 将标签、边类型、方向、度类别、属性要求、快照与 schema epoch 暴露为面向存储的元数据，而不是仅仅把它们当作查询计划中的谓词。
2. 一种用于语义 L0 布局的选择性 materialization 策略。收益评分在限制元数据和 fanout 的前提下保留高价值的语义分区，避免对每一种语义组合都完全 materialize 的成本。
3. 一个反馈驱动的语义 compaction 回路。运行时反馈可以识别热点图访问签名，并在受控工作负载迁移下重新定向 compaction 优先级；重写成本代理记账则与生产级 write-stall claim 明确分离。
4. 一个面向语义剪枝的 schema / snapshot 安全模型。版本化 schema catalog、segment `schema_epoch`、tombstone-sensitive 摘要以及“仅精确证明才可剪枝”的规则，使旧存储在增量 schema 变化和特定动态图 delta 下仍然可读，并避免假阴性。
5. 一个 source-ready 的证据包。当前包将 claim 映射到表格、命令、源码测试、store 保留决策和最终提交阻塞条件，使论文证据与未批准的后续实验彼此分离。

整个评估也遵循同样的链路。RQ1 问的是：查询语义布局是否减少了 LSM 读放大。RQ2 问的是：收益评分是否避免了完全语义 materialization 的成本。RQ3 问的是：反馈 compaction 是否能够适应一次受控的工作负载迁移。RQ4 检查 schema 演进和动态图 delta 是否在精确证明剪枝下保持正确性。RQ5 报告可见的写入、存储和重写成本代理。

通过回答这些问题，SemL0 主张：属性图查询语义可以指导 LSM 物理设计，而不必局限于拓扑与写入局部性。本文的中心观点不是“每个图数据库功能都已实现”，而是：标签、边类型、属性、schema epoch、snapshot 与运行时反馈，都可以在不牺牲保守正确性的前提下变得对存储层可见。

下一节将把这种不匹配说得更明确：LSM 的更新局部性固然有用，但如果缺乏语义元数据，delta 区域就会转化为读放大。

## 2. Background And Problem

动态图属性图既有高更新率，又有高度语义选择性的读取。一次邻域查询可能依赖源顶点标签、边类型、方向、目标标签、源点度类别、属性存在性或属性值、快照时间戳以及 schema epoch。这些谓词不仅仅是逻辑查询规划器中的细节。在存储边界上，它们决定一个最近的 segment 是否可能包含可见答案。

LSM 存储之所以适合动态图，是因为它能够以 append-heavy 的 delta 形式吸收更新，并在稍后通过 compaction 重组。图引擎可以把最近的边和属性更新刷入 CSR 风格的 L0 segment，而更旧的数据则逐步被整合。这使得写路径代价较低，并避免为每次插入、删除或属性更新而重写大块图区域。

但同样的设计也会带来读侧问题。L0 segment 会重叠。一个关于某个源顶点、边类型、度类别或属性的查询，可能必须检查许多最近的 segment，之后才发现其中大多数其实与该查询无关。传统 LSM 元数据通常围绕 key、timestamp 和 compaction level 做推理。对于属性图访问而言，这些维度还不够：某个 segment 在 key 空间上可能“很近”，但由于它包含的是不同边标签、不兼容的度类别、缺少必需属性、包含必须谨慎合并的 tombstone，或者其中数据是在旧 schema 解释下写入的，因此它对查询是无关的。

这正是 SemL0 要解决的背景性不匹配。LSM 的 locality 保住了写入效率，但图数据对查询是否有用，是由标签、边类型、属性、snapshot 和 schema epoch 决定的。当这些维度对存储层不可见时，语义上的无关性就会转化成读放大。

我们把这种不匹配称为 query-semantics blindness：

```text
the storage layer maintains update-local LSM segments, but it cannot directly
use graph query semantics to prove which segments are irrelevant.
```

因此，问题不仅仅在于 L0 中 segment 很多。更深层的问题是：图相关性是多维且动态的。边类型、源标签、方向、度类别、属性谓词、快照可见性和 schema epoch 都可能同时影响同一次读取。如果这些维度继续对物理布局和 segment 元数据不可见，那么存储引擎就无法区分“有用的重叠”和“无关的重叠”。

### 面向存储的查询语义

SemL0 用 `GraphAccessSignature` 表示图访问中面向存储的那一部分。一个签名并不是完整的逻辑查询计划，也不取代查询处理器。它是存储层能够安全用于候选选择的查询语义子集：

```text
source vertex
source label
edge type
direction
degree class
destination label
snapshot boundary
property predicate
schema interpretation
```

例如，一次面向低度 `Person` 源点并带有必需属性的类型化邻域查询，与一次针对混合度源点的非类型扫描，是两种不同的存储访问。如果两者都走同一条 L0 探测路径，那么引擎就无法避开那些在语义上与查询不相交的 segment。

与之对应的 segment 侧对象是 `CsrSegmentMeta`。它记录了对存储层可见的摘要，例如源标签、边类型分区、方向、度类别、该度类别是否精确、`schema_epoch`、`property_presence_bitmap`、属性摘要完整性，以及该 segment 是否可能包含 tombstone。这些字段构成了后续 C1 物理设计以及 schema / snapshot 安全规则所使用的词汇表。

### 为什么剪枝是一份正确性合约

语义元数据只有在不会产生假阴性时才有价值。一个 segment 只有在其元数据能够证明其中不存在任何满足查询的可见记录时，才可以被跳过。如果元数据是未知的、混合的、遗留的、不完整的、对 tombstone 敏感的，或者对当前谓词来说不够强，那么引擎就必须把该 segment 保留在候选集中。

这就给 SemL0 带来了统一的剪枝合约：

```text
Only prune when metadata proves absence or disjointness.
Otherwise read conservatively.
```

精确不相交摘要可以移除一个 segment。精确的“属性缺失”摘要，可以在“必须存在该属性”的谓词下移除一个 segment。但一个带 tombstone 的 segment 不能仅仅因为其正向属性摘要看起来“不存在”就被丢弃：那个 tombstone 仍然可能是隐藏更旧可见版本所必需的。同样地，混合或未知的度元数据，也不能剪掉一个在当前查询快照下可能包含所请求源点的 segment。

不确定性的代价是额外探测；但这种代价绝不能是错误答案。

### 问题模型中的 Schema 演进

当 schema 发生变化时，query-semantics blindness 会变得更微妙。如果物理布局依赖标签、边类型和属性，那么人们自然会问：当新增一个边标签或属性时，旧 store 会不会因此变得没用？在 SemL0 的问题模型里，答案是否定的。

原因在于：逻辑 schema 解释与物理 segment 字节是分离的。逻辑 schema 一旦变化，catalog 就推进一个 `schema_epoch`。未来的写入使用当前 catalog 状态，而旧 segment 保留其写入时对应的 epoch 和元数据完整性状态。一个基于当前 catalog 编译出的查询，仍然可以通过 catalog 历史并应用“精确证明”规则来读取旧 segment。

对于增量式的边标签、顶点标签或属性变化，精确的旧元数据可能可以证明旧 segment 与新引入的语义区域不相交，因此可以跳过。如果旧元数据是未知的、混合的或对 tombstone 敏感，那么就保守读取。后续 lazy compaction 可以在新的解释下重建热点元数据，但那是一条性能修复路径，而不是正确性的前提条件。

```text
Schema changes do not automatically invalidate old storage.
```

这条边界对后文非常重要。SemL0 不声称自己实现了一个面向所有 rename、drop、type change 或 encoding change 的完整物理迁移引擎。它声称的是：增量式 schema 演进以及新旧混合元数据，可以通过 catalog epoch、精确证明剪枝与保守回退来保持正确性。

### Snapshot 与 Delta 可见性

动态图读取是快照读取。插入、删除、属性变化和 compaction 与查询语义相互作用，因为某个 segment 可能包含在当前 snapshot 下不可见、但在另一个 snapshot 下可见的记录。Tombstone 尤其重要：即便 tombstone 行本身不满足正向属性谓词，一个 tombstone segment 仍可能是删除旧边可见结果所必需的。

因此，存储问题包含两个耦合要求：

1. 当语义元数据能够给出精确证明时，减少 L0 候选；
2. 当无法给出这种证明时，保住可见历史正确性。

这也是为什么后文把 C1 查询语义布局、P3 反馈 compaction、P2 schema epoch 和 P4 snapshot / tombstone 处理看成一个系统，而不是彼此独立的优化。一个没有 schema 与 snapshot 安全性的 pruning surface 可能很快，但并不安全；反过来，如果只有 schema 与 snapshot 安全性、却没有语义 pruning surface，那么系统虽然正确，却无法真正解决 L0 读放大的核心问题。

### 问题陈述

SemL0 要解决的问题是：

```text
How can an LSM graph store make property-graph query semantics visible to L0
layout, candidate selection, and compaction, while preserving exact no-false-
negative behavior under schema changes, tombstones, and snapshot-visible deltas?
```

这个问题陈述隐含了三项要求：

1. 面向存储的查询语义必须被显式表示，而不能偶然地仅从 key range 中推断。
2. Segment 元数据必须区分 exact、mixed、unknown、legacy、incomplete 与 tombstone-sensitive 摘要。
3. 维护机制必须改善未来布局，但不能改变查询语义，也不能让旧存储失效。

SemL0 的技术章节将按顺序回答这些要求。System Overview 将连接写、读、维护以及 schema / snapshot 安全路径。C1 章节定义查询语义 pruning surface。Materialization policy 决定哪些语义布局值得维持。P3 章节展示反馈如何在工作负载迁移下维持这一 pruning surface。Schema / snapshot 章节解释：为什么增量式 schema 演进与动态图 delta 并不要求全局重建，也不要求不安全的剪枝。

这些要求定义了系统的整体形态。下一节将把写路径、读路径、维护路径与 schema / snapshot 路径放在同一个精确证明剪枝框架下进行总览。

## 3. System Overview

SemL0 是一种基于 LSM 的动态图属性图存储设计，它让“最近更新区域”具备查询语义可见性。它并不取代逻辑查询处理器。相反，它把图查询语义中面向存储的部分暴露给 L0 segment 布局、候选剪枝与维护过程。

系统围绕同一条共享安全合约组织：

```text
metadata may reduce the candidate set only when it proves that a segment cannot
contain a visible match.
```

这条精确证明合约是整个设计的主轴。C1 用精确语义元数据减少 L0 读放大。Materialization policy 决定哪些摘要值得维持。P3 使用反馈驱动的 compaction 在工作负载迁移时修复热点语义区域。P2 让旧 segment 在 schema epoch 下继续可读。P4 防止 insert、delete、tombstone、snapshot 与 compaction 造成假阴性剪枝。P5 则记录让这些部件达到论文可交付状态所需的证据与 claim 边界，同时避免夸大实现程度。

### Pipeline

SemL0 的 pipeline 有四条操作路径：

```text
write path
read path
maintenance path
schema/snapshot safety path
```

写路径缓冲图更新，并把它们刷入 CSR 风格的 L0 segment。每个 segment 携带 `CsrSegmentMeta`：源标签、边类型、方向、度类别、schema epoch、属性存在位图、tombstone 状态以及元数据完整性。这些字段让图语义对存储层可见，而不要求每次查询都检查每个最近 segment。

读路径把一次邻域查询或属性值访问编译为 `GraphAccessSignature`。这个签名是面向存储的，而不是完整逻辑查询计划。它包含影响 segment 剪枝的维度：源标签、边类型、方向、度类别、必需属性存在性、在实现边界内的属性值谓词、snapshot 和 schema 解释。语义 L0 索引和度目录利用这个签名选择候选 segment。精确不相交或精确缺失元数据可以用于剪枝；而 mixed、unknown、legacy、incomplete 或 tombstone-sensitive 元数据必须被保守读取。

维护路径观测运行时读取与 compaction 成本。候选 L0 segment、查询计数、语义范围桶、读字节数和重写大小估计等指标，会识别出那些布局已不再有效的热点区域。反馈 compaction 随后会为这些区域重写未来的物理布局。它不会改变查询语义，也不会放松“精确证明剪枝”合约。

schema / snapshot 安全路径则把物理布局与逻辑解释分离开来。Schema catalog 推进 epoch，并把逻辑标签或属性映射到物理标识符。旧 segment 保留各自的 `schema_epoch`，因此仍然可读。Snapshot 与 tombstone 规则决定哪些记录是可见的，而 compaction 只能在当前安全边界内压缩历史。

### 四个相互连接的回路

SemL0 的实现可以视为四个彼此相连的回路：

| 回路 | 存储对象 | 目的 | 安全规则 |
|---|---|---|---|
| C1 query-semantic layout | `GraphAccessSignature`、`CsrSegmentMeta`、semantic L0 index、degree directory | 通过让标签、边类型、方向、度和属性要求对存储可见来减少 L0 读放大 | 只在有精确证明时剪枝 |
| P3 feedback compaction | metrics、L0 partition snapshots、compaction decisions | 当热点查询范围迁移时调整语义布局 | 只重写布局，不改变查询语义 |
| P2 schema interpretation | `SchemaCatalog`、segment `schema_epoch`、property encoding epochs | 让旧 segment 在增量 schema 变化后仍然可读 | 对 unknown、mixed、legacy 或 incomplete 元数据保守读取 |
| P4 snapshot/delta correctness | snapshots、tombstones、merge-visible logic、snapshot-GC safe point | 在更新与 compaction 下保持可见历史正确性 | 绝不剪掉可能包含可见匹配的 segment |

这些回路是有意耦合的。C1 创造了 P3 可以修复与改进的元数据；P2 解释了为什么这些元数据在 schema 变化后仍可解释；P4 解释了为什么 compaction 和 tombstone 不会破坏 snapshot 可见答案；P5 则把这些回路与评估相连接，并把 claim 强化放在显式证据 gate 之后。

### Schema 演进边界

System Overview 保留了与 Background 和 Schema Evolution 章节相同的 schema 回答：

```text
Schema changes do not automatically invalidate old storage.
```

新增边标签、顶点标签或属性时，catalog epoch 会为未来写入推进。旧 segment 通过记录的 `schema_epoch`、元数据完整性状态、精确证明剪枝以及对 legacy、mixed、unknown、tombstone-sensitive 元数据的保守读取，依然保持可读。Lazy compaction 可以在稍后修复布局质量，但正确性并不要求全局重建。对更强的 rename、drop、type-change、encoding-change 或 canonical-id normalization 情况下的完整物理迁移，仍属于未来工作。

### Figure Hook

未来可以用一张图表示如下流程：

```text
updates
  -> CSR-like L0 segment writer
  -> schema_epoch-aware semantic metadata
  -> semantic L0 index + degree directory
  -> GraphAccessSignature read path
  -> exact-proof pruning or conservative read fallback
  -> runtime metrics
  -> feedback compaction decision
  -> lazy semantic re-layout
```

这张图应把 schema catalog 与 snapshot / tombstone 层表现为围绕读路径和 compaction 的安全带。它不应暗示 schema 变化会触发全局重建，也不应暗示反馈 compaction 可以在未获批准时运行，更不应暗示 mixed 元数据可以被安全剪枝。

### 通向技术章节的桥接

后文将沿着如下链路展开：

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

C1 章节定义面向存储的语义摘要与 pruning surface。反馈 compaction 章节说明运行时证据如何改进未来热点语义区域的布局。Schema / snapshot 章节建立起 catalog epoch、tombstone 与可见历史规则如何在元数据不确定时仍保持正确性的解释。最后，Evaluation 则把这些部分映射到当前证据边界，而不是把日志作为彼此无关的结果堆在一起。

在这些操作路径都建立起来之后，下一节将聚焦第一条回路：查询语义如何变成物理 segment 元数据与 L0 候选选择。

## 4. Query-Semantic Physical Design

LSM 图存储对更新友好，但它的读路径可能被迫探测许多重叠的 L0 segment。SemL0 的第一步设计就是把那些真正影响存储访问的图谓词暴露给 LSM 布局。SemL0 不把所有最近边 segment 一视同仁地看作可能候选，而是将一次面向存储的图访问映射为一个查询语义签名。

因此，本节应被理解为一个机制章节，而非表格逐行解读。它的内部逻辑是：

```text
problem pressure -> query-semantic signature -> exact segment summary -> bounded materialization -> SF1 evidence boundary -> bridge to feedback
```

这个签名并不是完整的逻辑查询计划，而是能够指导物理剪枝的查询语义子集：

```text
source label
edge type
direction
degree class
required property presence
property value predicate within the implemented boundary
snapshot/schema safety state
```

在实现中，这一角色由 `GraphAccessSignature` 表示。邻居扫描可以绑定一个边类型，附加一个 `DegreeClass`，并要求属性存在性或属性值谓词。Segment 元数据则回答一个面向存储的问题：这个 segment 是否可能包含对该签名可见的边？

SemL0 的剪枝合约是保守的：

```text
exact summaries may prune; mixed, unknown, legacy, or tombstone-sensitive
metadata must stay in the candidate set.
```

正是这条合约让 C1 能够与后续 schema 和 snapshot 章节兼容。查询语义元数据只有在能够证明一个 segment 与请求签名不相交时，才允许减少读取；否则，系统宁可多读，也不冒假阴性的风险。

### Segment 摘要

每个 CSR 风格 segment 都携带它所含范围的语义元数据。当前实现会追踪源标签、边类型、度类别和属性存在位图等维度。对于精确语义 segment，这些字段可以证明某个 segment 只包含特定的边类型和度类别组合。而对 mixed 或 unknown segment，同样的字段则会刻意失去剪枝能力。

这带来三种操作情形：

| Segment 元数据状态 | 读路径动作 |
|---|---|
| exact match | 保留为候选 |
| exact disjointness 或 exact absent required property | 剪枝 |
| mixed、unknown、legacy 或 tombstone-sensitive summary | 保守保留 |

关键点在于：SemL0 能减少读放大，而不改变逻辑查询语义。物理设计改变的是哪些 L0 文件被优先考虑，而正确性仍然依赖精确证明剪枝和行级验证。

这也是它区别于“仅做 benchmark 优化”的地方。C1 不声称某个阈值永远胜出。它声称的是：让图语义对存储层可见，会创造一个安全的 pruning surface；后续的策略和反馈机制再决定这个 pruning surface 的哪些部分值得维持。

### Semantic L0 Index 与 Degree Directory

SemL0 在最近的 CSR segment 元数据之上构建 semantic L0 index。该索引把签名维度映射到候选 L0 文件，因此一个关于特定边类型和度类别的查询可以避免扫描无关的精确 segment。

度感知剪枝非常重要，因为图邻域往往是高度偏斜的。一个高度源点与一个低度源点，其读放大轮廓可能完全不同。因此 SemL0 记录 `DegreeClass` 摘要，并维护一个 degree directory，把度敏感查询导向正确的 segment 类别。同时，在需要时读路径仍会把 mixed 和 unknown 类别包含进来，从而保持同样的保守回退规则。

本节应将其描述为一种物理设计机制，而不是一种新的逻辑查询语言特性。用户请求的是“邻居”；而 SemL0 决定的是哪些 L0 语义分区是安全候选。

### 属性存在性与属性值边界

属性图查询往往不只依赖拓扑。一个缺失必需属性的 segment，不可能满足要求该属性存在的谓词。SemL0 为可表示的属性 id 记录一个属性存在位图，并且只在“安全地证明某属性不存在”时使用它。

当前实现边界被有意限制为：

```text
fixed-width property equality and required-property presence
```

论文不应把它泛化为字符串 / 范围 / 复合谓词，也不应泛化为完整 SQL null 语义。如果某个属性 id 无法在位图中表示，或者某个 segment 的属性元数据不完整，SemL0 就会保守读取。

### 选择性 Materialization

完全语义 materialization 可以减少读取，但也会增加 segment fanout 与元数据成本。因此，SemL0 采用基于收益评分的 materialization policy。该策略估计：保留一个精确语义分区是否值得付出额外的文件与元数据 fanout 成本；考虑维度包括边类型、度类别、segment 大小与 benefit score 阈值。

于是 C1 不再是“把所有东西都 materialize”，而变成一项有界物理设计策略：

```text
materialize high-value semantic partitions
merge low-benefit partitions into mixed segments
preserve correctness by reading mixed segments conservatively
```

修复后的 SF1 消融实验把这种权衡展示得很清楚。完全语义 materialization 是一个高读削减的上界行。细阈值变体会保留很多精确文件，并能实现强读削减，但 fanout 很高。微阈值变体仍然保持正确性，但带来的读收益几乎可以忽略。基于收益评分的候选是当前平衡的论文行：它在保留零 core mismatch 和零 all-types mismatch 的同时，减少了读字节，而不需要对每一种语义分区完全 materialize。

当前修复后的指标边界如下：

```text
No latest-code SF30/SF100 final performance claim.
```

| Variant | 角色 | Core read reduction | All-types read reduction | Correctness |
|---|---|---:|---:|---|
| `schema` | baseline | 0.00% | 0.00% | baseline row |
| `full_semantic` | upper bound | 99.35% | 94.88% | upper-bound row |
| `fine64k` | high-fanout threshold | 99.32% | 94.84% | 0 core / 0 all-types mismatches |
| `fine512k` | coarser threshold | 97.06% | 92.64% | 0 core / 0 all-types mismatches after fix |
| `micro544k` | low-benefit threshold | 0.18% | 0.15% | 0 core / 0 all-types mismatches |
| `benefit_scored` | current balanced candidate | 72.86% | 58.89% | 0 core / 0 all-types mismatches |

这些行支持一个有界 claim：

```text
query-semantic physical design can reduce SF1 read amplification, and benefit
scoring is needed to avoid the cost of indiscriminate semantic fanout.
```

但它们并不能证明 latest-code SF30 / SF100 最终性能、全局优化最优性，或可忽略的写入开销。

### 正确性边界

C1 只有在保持查询正确性时才有价值。这里最重要的回归历史，是对 edge-type mismatch 的诊断与修复：早期阈值变体曾暴露 all-types mismatch，而后续修复则通过在必要时扩展 mixed segment 类别，保住了保守的度行为。修复后的摘要记录为：

```text
core_mismatches=0
alltypes_mismatches=0
failed=0
```

因此，论文应强调“精确证明剪枝”，而不是“激进的语义剪枝”。一个 segment 只有在其元数据能够证明它不可能包含匹配的可见边时才可跳过；否则，多做一些读是可以接受的，但假阴性绝不可以接受。

静态语义布局只是起点。下一节将说明，当热点图访问模式发生移动时，运行时反馈如何维护这一布局。

## 5. Feedback-Driven Semantic Compaction

查询语义布局之所以有用，是因为 C1 让一个安全的语义 pruning surface 对读路径可见。但这个 pruning surface 仍然需要维护。一个静态布局可能会逐渐偏离当前工作负载：在某一阶段有价值的语义分区，可能在工作负载转向不同的源范围、边类型或属性要求之后就不再重要。因此，SemL0 在 C1 的语义布局之上增加了一个反馈回路。运行时读取会暴露哪些查询语义范围是热点，而 compaction 则可以有选择地把这些范围重写为读放大更低的布局。

本节应被理解为一个自适应维护机制，而不是一个生产级调度器证明。它的内部逻辑为：

```text
C1 pruning surface -> runtime feedback counters -> hot semantic range selection -> bounded rewrite -> controlled workload-shift evidence -> sustained-run boundary
```

这里的运行时反馈，是指从读路径收集的存储级计数器，而不是学习型查询优化器或完整的生产调度模型。

反馈回路记录的是面向存储的信号，而非完整查询计划：

```text
query count
candidate L0 segments
range bucket
edge type
estimated rewrite bytes
selected L0 segments
read bytes before and after compaction
compaction input/output bytes
```

核心决策是“每单位重写成本对应的收益”。一个热点范围只有在重写它很可能在当前查询模式下节省读工作时，才值得处理。因此 SemL0 会给候选 L0 分区打分，并选择一个有界范围来进行语义 compaction。Compaction 改变的是未来的物理布局，而不是查询语义。如果元数据仍然不确定，读路径就继续保守。

这与 C1 使用的是同一条精确证明合约。反馈可以决定哪个语义范围值得重写，但它不会让 mixed、unknown、legacy 或 tombstone-sensitive 元数据变得可以安全跳过。自适应改变的是维护优先级，而不是查询结果的含义。

### 工作负载迁移证据

当前 P3 的证据是一组受控工作负载迁移微基准。它包含两个阶段。Phase A 重复查询一个热点源范围。在收集反馈指标之后，SemL0 选择该热点 L0 范围并对其进行 compaction。随后，Phase B 重置指标并把热点查询源移向另一个范围。若要说明适应成功，就要求被选中的范围随工作负载一同变化。

当前摘要记录为：

```text
test_hot_partition PASS
test_workload_shift PASS
check_feedback_bench PASS
bench_workload_shift PASS
phase_a_before_l0=9
phase_a_after_l0=0
phase_b_before_l0=9
phase_b_after_l0=0
selected_ranges_changed=True
no_feedback_phase_a_after_l0=3
no_feedback_phase_b_after_l0=3
phase_a_selected_l0=3
phase_b_selected_l0=3
phase_a_output_segments=1
phase_b_output_segments=1
```

Table 3 应被解释为适应结果。启用反馈后，每个阶段的热点查询 L0 候选数都能从 9 降到 0。Phase B 中选中的范围发生了变化，说明在指标重置之后，优先级确实会跟随新的热点范围移动。若不使用反馈，同样的查询模式在阶段结束后仍保留非零 L0 候选，因此结果并不是简单由重复读取造成的副产物。

### 重写成本代理

反馈故事也必须包含成本。当前证据并不声称生产级 write-stall 安全性，也不声称开销可以忽略。相反，Table 7 与归一化 rewrite-cost CSV 只提供了该微基准的确定性重写成本代理。

当前行记录为：

```text
selected_l0_segments=3
output_segments=1
estimated_rewrite_bytes=552
compaction_input_bytes=552
compaction_output_bytes=248
io_write_bytes_compaction=248
read_bytes_before=288
read_bytes_after=96
read_bytes_saved_per_compaction_input_mib=192.000
```

这些字段让论文能够把读侧改进与产生该改进所需的重写工作联系起来。但它们应当被描述为“记账证据”，而非生产级延迟或 write-stall 特征描述。这个微基准记录了 compaction 输入 / 输出字节、IO 写字节以及延迟代理；但它并没有建模生产环境中的并发、后台调度或长时间写压力。

### No-Feedback 对照

无反馈路径很重要，因为它将“适应”与“固定布局副作用”区分开来。在受控摘要中，无反馈的 Phase A 和 Phase B 都保留了非零 L0 候选：

```text
no_feedback_phase_a_after_l0=3
no_feedback_phase_b_after_l0=3
```

论文应当对这种对照作出收窄解释。它支持的 claim 是：反馈 compaction 对受控热点范围减少了候选 L0 读取。它并不能证明所有未来工作负载都会快速收敛，也不能证明打分是全局最优，更不能证明生产级 compaction 调度已经被解决。

### 持续运行边界

更长时间的 sustained feedback experiment 已经被设计出来，但尚未获批或执行。审批包中固定了新的日志目录、新的 benchmark store、`--reset-store` 安全规则和接受标准。当前状态为：

```text
experiment_approval_status=not_approved
safe_to_run_now=no
experiments_run=0
stores_deleted=0
```

因此，本节目前只能使用如下表述：

```text
controlled workload-shift microbenchmarks show feedback can adapt semantic
compaction priority.
```

而不能写成：

```text
feedback compaction has negligible overhead
feedback compaction is production write-stall safe
feedback compaction is globally optimal
feedback compaction was validated on long-running SF30/SF100 workloads
```

反馈可以决定“重写什么”，但 schema 与 snapshot 解释决定了“什么能被安全剪枝”。下一节将把这一安全边界明确化。

## 6. Schema Evolution and Snapshot-Safe Metadata

SemL0 的 C1 与 P3 机制让标签、边类型、属性要求、度类别和 schema 摘要对存储层可见。而这种可见性立刻带来一个关键安全问题：如果图 schema 在某个 segment 写入之后发生变化，那么该 segment 的语义元数据会不会变陈旧、不安全，或者失去意义？尤其是，新增一个边标签或属性，绝不能迫使整个现有 store 全局重建。

本节是前面 C1 pruning surface 与 P3 maintenance loop 的 P2 / P4 安全层。C1 定义了语义元数据能够证明什么；P3 决定在工作负载反馈变化后，哪些语义区域值得维护；P2 / P4 则决定某一份元数据在当前查询的 schema catalog 与 snapshot 视图下是否仍然安全。其内部逻辑为：

```text
schema change concern -> catalog epoch -> segment schema_epoch -> exact-proof pruning -> conservative read fallback -> lazy compaction as performance repair -> future physical migration boundary
```

SemL0 将逻辑 schema 演进与物理存储布局区分开来。Schema catalog 为标签、边类型和属性分配稳定的物理标识符，记录 alias 与 encoding epoch，并在逻辑 schema 变化时推进 `schema_epoch`。每个 LSM 或 CSR segment 都记录其语义摘要生成时对应的 epoch 与元数据完整性状态。因此，一个依据当前 catalog 编译的查询，仍然可以通过 segment 自身的 epoch 来解释旧 segment。

安全规则仍然是精确证明剪枝：

```text
Only prune a segment when metadata proves that it cannot contain a visible
match; otherwise read or merge it conservatively.
```

这条规则把查询语义布局与 schema 演进连接在一起。Schema 变化可能会让剪枝精度下降，直到未来的 compaction 刷新元数据为止；但它不会让旧字节失效。回退代价是额外读取，而不是假阴性或数据废弃。

换句话说，schema 演进首先改变的是“解释”，其次才可能改变“物理布局”。一个 catalog epoch 会立刻影响未来写入；旧 segment 仍然可用，因为其自身的 `schema_epoch` 与元数据完整性状态告诉读路径：哪些内容还可信，哪些必须保守处理。

### Schema 变更决策边界

该设计把 schema 变化中经常被混淆的三个问题分开：

```text
Does the catalog interpretation change now?
Does correctness require old segments to be read or rewritten now?
Does performance later benefit from rewriting selected hot segments?
```

SemL0 对这三个问题分别回答。Catalog 对未来写入立刻生效。正确性则通过在旧 segment 自身的 `schema_epoch` 下读取它们，并且只在有精确证明时才剪枝来保持。物理重写则被保留给 lazy compaction 或未来迁移工作。

| Schema change | Immediate action | Old segment status | Required for correctness | Later work |
|---|---|---|---|---|
| Add edge label | 分配 / catalog 物理 edge-type id；推进 catalog epoch；路由未来写入 | 旧 segment 在其存储 epoch 下仍可读 | 不需要全局重建；仅在精确摘要能证明 absence 时才剪枝 | 如果新标签成为热点，可做 lazy compaction |
| Add vertex label | 分配 / catalog 物理 vertex-label id；推进 catalog epoch | 旧 segment 仍可读；精确旧标签摘要可能证明不相交 | 不需要全局重建；若元数据 unknown 或 mixed，则保守读取 | 对热点签名可选重布局 |
| Add property | 分配 property id；记录 default/null 规则和 encoding epoch | 旧行仍可读；缺失属性通过 catalog 语义解释 | 正确性不需要回填 | 可选属性摘要 materialization |
| Alias or logical rename | 将逻辑名解析到 canonical 物理 id | 旧物理 id 仍有效 | 若 alias 解析足够，则不需要重写 | 可选规范化 |
| Drop label/property | 在某个 epoch 标记 catalog 项为 dropped | 在 snapshot / history 需要时旧字节仍然可读 | 不声称立即回收 | 未来的 snapshot-safe 回收 |
| Type or encoding change | 推进 encoding epoch 并保留旧 decoder 元数据 | 旧行通过各自存储的 encoding epoch 分派解码 | 不声称广义迁移 | 未来的 type-normalization migration |
| Unknown、legacy、mixed 或 tombstone-sensitive metadata | 不信任任何 catalog shortcut | 该 segment 保留在候选集中 | 保守读取或合并 | lazy compaction 可恢复剪枝精度 |

这张表是对“schema 变化是否会让旧存储失效”的操作性回答。增量式变化是 catalog 和未来写入事件，而不是 store 废弃事件。不确定元数据会降低剪枝精度，但不会让旧字节变得不可用。因此，lazy compaction 是对热点或不精确区域的性能修复路径，而 rename / drop / type-change 的物理迁移仍属于未来工作。

### 增量边标签

当用户新增一个边标签时，SemL0 会创建 catalog 项，分配或暴露物理 edge-type 标识符，推进 catalog epoch，并将未来写入路由到新 epoch。现有 segment 保留其较旧的 `schema_epoch`。对于一个关于新标签的查询，旧 segment 只有在其精确 edge-type 摘要能够证明不相交时才可以跳过。如果该 segment 属于 legacy、mixed、tombstone-sensitive，或者其摘要完整性未知，那么读路径就必须把它保留在候选集中。

因此，新增边标签是一件 catalog / future-write 事件，而不是一次全局存储重建：

```text
add edge label => catalog epoch + future-write routing
not => old store invalidation
not => mandatory segment rewrite
```

这就是对边标签担忧的直接回答：新增一个标签并不会让以前的存储失去意义。它可能会在 legacy、mixed 或 unknown 摘要区域上带来额外读取，直到未来 compaction 刷新热点元数据为止，但旧 store 仍然是正确读取路径的一部分。

同样的规则也适用于增量顶点标签。旧元数据有时足以证明旧 segment 不可能包含新的物理 id；而在不足以证明时，SemL0 会选择 over-probe，而不是激进剪枝。

### 增量属性

新增属性时，会记录一个属性标识符、default-or-null 语义以及未来行的 encoding epoch。旧行不会因为要添加一个物理列而被重写。相反，查询会通过 catalog 规则解释“缺失属性”。在当前实现边界内，定宽相等谓词可以利用精确属性存在性 / 属性值摘要。

对于要求某个新属性存在的谓词，一个精确“属性不存在”的 segment 摘要可以证明旧行不可能满足该谓词。对于投影查询，旧行则可以返回 catalog 定义的 `NULL` 或默认值，而无需读取一个根本不存在的属性列。如果某个 segment 缺乏精确属性元数据，则保守读取。

这支持论文中的如下 claim：

```text
additive property changes do not invalidate old storage under the implemented
fixed-width equality boundary.
```

但这并不声称支持一般 SQL null 语义、字符串 / 范围谓词、复合属性谓词或完整的类型迁移引擎。

### Alias、Drop 与 Encoding 边界

Rename、drop 以及 type / encoding change 都是重要的边界案例，但不应被夸大。逻辑 rename 是到 canonical 物理 id 的 catalog alias，它不要求重写旧 segment。被 dropped 的属性可以从当前面向外部的值查询中隐藏，但在 snapshot 规则要求时，旧拓扑和历史字节仍保持可读。Type 或 encoding change 会推进 encoding epoch，使得行数据能够根据其存储 epoch 调度到正确的 decoder。

这些情况说明：SemL0 的 catalog 能表示非增量式 schema 历史。但这并不意味着当前 artifact 已经成为完整的物理 schema migration 系统。Drop 后的物理回收、广义 rename / drop 重写以及通用类型迁移仍然属于未来工作。

### 与 Snapshot-Correct Delta 的交互

Schema 演进与 snapshot 可见性必须被一起处理。某个 segment 可能同时包含可见插入、被隐藏的 tombstone、旧属性编码，或是在旧 schema epoch 下写入的元数据。Compaction 不能跨越 snapshot safe point 压平这些历史，而剪枝也不能忽略 tombstone-sensitive 元数据。

因此，SemL0 将 tombstone-sensitive、mixed-epoch、legacy 或 incomplete 摘要都视为保守候选。系统在这些情况下可能失去一部分剪枝能力，但它保住了“无假阴性”不变量：

```text
semantic pruning must never remove a segment that can contain a visible match
under the query snapshot and schema interpretation.
```

未来 compaction 可以为热点区域重新生成 schema-versioned semantic metadata。这种 lazy re-layout 会在保守读取变得昂贵时改善性能，但它并不是增量 schema 变化后立即正确性的必要条件。

### 证据与边界

当前证据支持的是：增量 no-rebuild schema 演进与目标化的 schema / snapshot 正确性。证据包括跨 schema epoch 的旧 segment 可读性、schema epoch 与 snapshot-visible delta 的组合、property schema epoch、保守 tombstone 剪枝、alias 解析、drop-property 边界行为，以及 epoch-aware 的定宽解码。

由此支持的论文 claim 是：

```text
SemL0 keeps old storage readable across additive schema changes by combining a
versioned schema catalog, segment schema epochs, exact-proof pruning, and
conservative reads for unknown or mixed metadata.
```

当前边界仍然是：

```text
complete schema migration
automatic physical reclamation after drop
negligible schema-change overhead
general type migration
range/string/compound property predicate support
SQL null semantics
```

通向 Evaluation 的桥接句应该是：

```text
The evaluation treats schema evolution and snapshot-correct deltas as a
correctness boundary for semantic pruning, not as a full physical migration
claim.
```

这些设计规则直接引出评估问题：按顺序衡量 pruning surface、materialization policy、自适应维护、正确性边界与成本记账。

## 7. Evaluation

整个评估遵循论文使用的同一条存储链路：

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

这条链路让五个研究问题是“阶段相关”的，而不是“日志相关”的。RQ1 和 RQ2 测量 C1 pruning surface 与 materialization policy。RQ3 测量受控反馈维护。RQ4 检查 P2 / P4 的 schema / snapshot 安全性。RQ5 报告 P5 的成本 / 证据记账。

当前包是 source-ready 且 venue-neutral 的。当前支持的评估边界为：

```text
latest-code SF1 evidence only for the main performance claim
controlled feedback workload-shift evidence only
targeted schema/snapshot/tombstone correctness only
write/rewrite cost proxy accounting
source-ready package, not final-submission ready
```

当前不支持的评估边界为：

```text
final latest-code SF30/SF100 performance
production write-stall safety
full schema-migration engine
completed sustained feedback
completed store cleanup
final submission readiness
```

### RQ1：查询语义布局是否减少读放大？

RQ1 评估的是核心 C1 claim。Table 1 比较 schema-only 布局、full semantic materialization 以及当前 benefit-scored policy 在 latest-code SF1 行上的表现。

这一研究问题应当与 C1 机制直接关联：查询语义签名与精确 segment 摘要共同构成 semantic pruning surface。Table 1 测量的正是：在当前 SF1 证据包下，这个 pruning surface 是否减少了读放大。

当前 Table 1 的解释为：

```text
schema baseline:
  core_read_bytes=303,689,504
  alltypes_read_bytes=410,942,896

full semantic:
  core_reduction=99.35%
  alltypes_reduction=94.88%
  correctness=pass

benefit scored:
  core_reduction=72.86%
  alltypes_reduction=58.89%
  correctness=pass
```

RQ1 的回答是：

```text
Yes for the current SF1 evidence package: exposing graph query signatures to L0
segment metadata materially reduces read amplification.
```

边界是：

```text
No final latest-code SF30/SF100 performance claim.
```

历史上的 SF30 / SF100 候选数字可以保留为动机或未来范围的背景，但在没有新的批准刷新之前，它们不应被当作最终评估结果。

### RQ2：收益评分是否避免了完全 Materialization 的成本？

RQ2 评估 SemL0 是否能够避免把每一种语义组合都 materialize 的成本。Table 2 比较了 full semantic、default budgeted 和 benefit scored 策略。Table 5 以及 C1 command-bundle audit 保留了修复后阈值上下文。

当前 Table 2 的解释为：

```text
full semantic:
  l0_files=3,727
  manifest_bytes=2,033,009
  core_reduction=99.35%
  alltypes_reduction=94.88%

default budgeted:
  l0_files=2,059
  manifest_bytes=1,126,473
  core_reduction=0.00%
  alltypes_reduction=0.00%

benefit scored:
  l0_files=3,251
  manifest_bytes=1,774,004
  core_reduction=72.86%
  alltypes_reduction=58.89%
```

RQ2 的回答是：

```text
Benefit scoring is needed because a fanout-only budget can preserve low file
count while losing read benefit, whereas indiscriminate full semantic layout has
higher fanout.
```

修复后的阈值行将这种敏感性展示得更明显：细阈值变体可以在高 fanout 下取得强读削减，而微阈值变体虽然正确，但几乎没有读收益。Benefit-scored 行是当前的平衡策略，而不是全局最优性的证明。

这一研究问题应被看作是维持 C1 pruning surface 的策略证据。它解释了为什么 SemL0 不会把每一种语义组合都 materialize，也解释了为什么仅仅基于 fanout 的预算可能会丢失读收益。

边界是：

```text
No global optimizer optimality claim.
No claim that benefit scoring is universally best for every workload.
```

### RQ3：反馈 Compaction 是否适应工作负载迁移？

RQ3 评估的是 P3。Table 3 是适应性表；Table 7 是 rewrite-cost proxy 表。它们应当一起讨论，但不能混为一谈。

这一研究问题应与 P3 的 prose 润色对应起来：C1 定义 semantic pruning surface，而 P3 决定当工作负载反馈变化时，这个 pruning surface 如何被维护。Table 3 测量的是适应，Table 7 记录的是重写成本代理字段。

当前 Table 3 的解释为：

```text
feedback phase A:
  before_l0=9
  after_l0=0
  selected_l0=3
  output_segments=1

feedback phase B:
  before_l0=9
  after_l0=0
  selected_l0=3
  output_segments=1
  selected_ranges_changed=True

no-feedback phase A/B:
  before_l0=9
  after_l0=3
  selected_l0=0
  output_segments=0
```

RQ3 的回答是：

```text
Controlled workload-shift evidence shows that feedback can move compaction
priority to the current hot semantic range and reduce hot-phase candidate L0
segments.
```

边界是：

```text
No production write-stall characterization.
No long-running production adaptation claim.
No sustained run result yet.
```

当前 sustained feedback experiment 仍受批准 gate 限制：

```text
experiment_approval_status=not_approved
safe_to_run_now=no
experiments_run=0
```

### RQ4：Schema 演进与动态图 Delta 是否保持正确性？

RQ4 评估的是 P2 / P4。Table 4 总结目标化正确性覆盖；Table 10 则把增量 schema 演进 claim 与未来迁移工作区分开来。

这一研究问题应该回答设计章节留下的安全问题：P3 可能改变哪些语义范围优先被维护，但 schema 与 snapshot 解释仍然决定哪些元数据可以安全使用。

当前 Table 4 的解释为：

```text
schema:
  cases=5
  passed=5

snapshot/delta:
  cases=4
  passed=4

schema+snapshot:
  cases=3
  passed=3
```

归一化正确性行与 mixed schema-delta inventory 覆盖：

```text
old segments remain readable across schema_epoch
new edge labels prune exact old segments but read mixed segments
required-property predicates prune exact-absent records
legacy missing property summaries stay conservative
tombstones and snapshots preserve visible history
schema epoch plus snapshot-visible deltas survive compaction and reopen
property schema epoch composes with snapshot/delta behavior
```

RQ4 的回答是：

```text
Targeted regressions support the no-false-negative pruning invariant under
additive schema evolution and dynamic deltas.
```

其中也包括直接的 schema-change 回答：

```text
Schema changes do not automatically invalidate old storage.
```

旧 segment 在其存储的 `schema_epoch` 下仍然可读；unknown、mixed、legacy 或 tombstone-sensitive 元数据被保守读取；lazy compaction 是性能修复，而不是正确性的先决条件。

边界是：

```text
No exhaustive dynamic-graph correctness proof.
No full rename/drop/type-change physical migration claim.
No range/string/compound predicate or SQL null semantics claim.
```

### RQ5：可见的写入、存储与重写成本是什么？

RQ5 把成本讨论限制在我们当前真正拥有的指标上。Table 6 记录 SF1 的 import / store proxy。Table 7 记录反馈 compaction 的 rewrite proxy。

这一研究问题是评估内部的 P5 证据包边界。它记录当前 artifact 可以说明什么，而不把 proxy 指标硬转换成生产级写路径 claim。

当前 Table 6 的解释为：

```text
schema:
  import_s=86.58
  store_bytes=3,794,892,645
  store_overhead=0.00%
  l0_files=2,059

full semantic:
  import_s=83.06
  store_bytes=3,802,298,981
  store_overhead=0.20%
  l0_files=3,727

benefit scored:
  import_s=83.25
  store_bytes=3,802,003,921
  store_overhead=0.19%
  l0_files=3,251
```

当前 Table 7 的解释为：

```text
selected_l0_segments=3
output_segments=1
estimated_rewrite_bytes=552
compaction_input_bytes=552
compaction_output_bytes=248
io_write_bytes_compaction=248
read_bytes_before=288
read_bytes_after=96
```

RQ5 的回答是：

```text
The current evidence reports storage overhead, file-count fanout, import-time
proxies, and deterministic rewrite accounting for feedback compaction.
```

边界是：

```text
No production write-stall proof.
No negligible-overhead claim.
No full workload-concurrency characterization.
```

### Evaluation 流程

本节应按以下顺序呈现表格：

```text
RQ1: Table 1
RQ2: Table 2 and Table 5
RQ3: Table 3 and Table 7
RQ4: Table 4 and Table 10
RQ5: Table 6 and Table 7
```

这样的顺序可以让论文叙事保持一致：

```text
read amplification reduction
selective materialization policy
adaptive maintenance
correctness under schema/snapshot/delta changes
visible cost accounting
```

同样的顺序也应当在 prose 中被描述为：

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

在研究问题证据之后，Related Work 会把 SemL0 与相邻的存储、图系统、自适应设计、视图系统和 schema 演进系统进行定位，而不会扩大 claim 边界。

## 8. Related Work

SemL0 处于若干成熟的存储与数据库研究脉络之间，但它的 claim 被刻意限制得比这些方向都更窄。它不是一个新的通用 LSM merge policy，不是一个完整的动态图 DBMS，不是一个逻辑 materialized-view 系统，也不是一个完整的 schema migration 引擎。它的贡献在于：通过 segment 元数据、精确证明剪枝、反馈 compaction 与 schema / snapshot-aware 解释，让属性图查询语义对 LSM delta 区域可见。

比较也沿用与 Evaluation 和 Limitations 相同的系统链路：

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

本节的目的，是把“借鉴来的原则”与“SemL0 实际做出的更窄物理设计 claim”区分开。

### 基于 LSM 的存储

Log-structured merge-tree 提出了通过缓冲更新与后台合并来实现写友好存储的核心思想 [@oneil1996lsm]。像 RocksDB 这样的生产系统展示了 compaction、内存管理和大规模 key-value 部署调优的重要性 [@dong2021rocksdb]。后续工作又从内存分配与过滤器 [@dayan2017monkey]、merge policy 设计 [@dayan2018dostoevsky]、key-value separation [@lu2016wisckey] 和碎片化 LSM 结构 [@raju2017pebblesdb] 等角度探讨 LSM 的权衡。

SemL0 与这条脉络是互补的。它并没有提出一个新的通用 LSM policy，也不声称对任意 key-value store 的 merge 行为最优。相反，它改变的是 LSM 图存储的物理设计信号：最近图 segment 的组织与剪枝，依据的是图访问签名，例如边类型、度类别、属性存在性、snapshot 可见性与 schema epoch。LSM 背景解释了为什么 L0 overlap 会造成读放大；SemL0 更窄的贡献则是：让图查询语义在这种 overlap 中变得可用。

### 动态图存储

动态图系统通常优化的是可变图存储、事务访问与新鲜分析。LiveGraph 使用事务边日志来支持顺序 adjacency-list 扫描 [@zhu2020livegraph]。Teseo 关注结构性动态图及其更新 / 扫描性能 [@leo2021teseo]。LLAMA 使用大规模多版本数组来支持基于 CSR 的可变图分析 [@macko2015llama]。LDBC Social Network Benchmark 则提供了一套评估图系统的标准交互式工作负载 [@erling2015ldbc]。

SemL0 与它们共享动态图动机，但它优化的对象是 LSM delta 区域，而不是一个完整图执行引擎。它的 segment 元数据将标签、边类型、度类别、属性摘要、schema epoch 与 tombstone sensitivity 暴露给候选剪枝与 compaction。这里的存储层问题更窄：查询语义能否减少需要读取的最近 segment 数量，同时不违反 snapshot 或 schema 正确性？

### 自适应物理设计

Database cracking 与 adaptive indexing 说明：访问路径可以在工作负载驱动下逐步细化，而不必完全离线固定 [@idreos2007cracking; @idreos2012adaptive]。SemL0 与这一思路共享一个更高层的理念：观测到的读取应当影响物理布局。区别在于物理对象与安全边界。SemL0 不是在原位调整一个关系索引；它利用运行时反馈识别热点图访问签名，并选择 L0 语义范围进行 compaction。

反馈回路是刻意收窄的。运行时反馈可以改变未来布局并减少未来候选，但它不会改变查询语义。一个 segment 只有在元数据给出 absence 或 disjointness 的精确证明时才可剪枝。这让自适应维护能够与 schema epoch、tombstone 与 snapshot-visible delta 相兼容。

### Materialized Views

Materialized-view 系统维护逻辑查询结果或派生关系，并研究如何以较低成本保持这些结果新鲜 [@gupta1995views]。View redefinition 工作则研究逻辑定义变化时现有 materialization 是否可被适配 [@gupta1995viewredef]。

SemL0 materialize 的是另一种对象。一个 semantic L0 segment 不是缓存的查询答案，也不取代查询执行。它是一种物理访问布局，使未来读取能够在精确 segment 元数据证明跳过安全时，略过无关的 LSM 记录。Schema 变化可能会使旧元数据的选择性下降，但并不会让旧字节失效。Lazy compaction 稍后可以修复高价值布局；它是一种物理维护机制，而不是一个通用的 view-redefinition 引擎。

### Schema Evolution

Schema-evolution 系统在逻辑定义变化时保持对既有数据的访问。PRISM 通过 schema modification operators 支持 legacy query [@curino2008prism]。PRIMA 将事务时间数据与历史 schema 版本连接起来 [@moon2008prima]。F1 提供面向分布式关系系统的在线异步 schema change [@rae2013f1schema]。Tesseract 则把 snapshot database 中的 schema evolution 建模为 data-definition-as-modification [@hu2022tesseract]。

SemL0 将这种 versioning 原则应用到了图 LSM segment 元数据上。Schema 变化会推进 catalog epoch，并影响未来写入；而旧 segment 继续在其原始 `schema_epoch` 下被解释。语义剪枝之所以仍然安全，是因为缺失、legacy、mixed、tombstone-sensitive 或不可表示的元数据，都不会被当成 absence 的证明。因而，当前论文 claim 是“在已实现边界内的增量 no-rebuild schema 演进”，而不是“对所有 schema 变化都提供完整的物理迁移”。

这个区别也是整篇论文使用的 schema-evolution 边界：schema 变化不是全局重建触发器，但完整的 rename / drop / type-change 物理迁移仍是未来工作。

### 定位总结

| 相关方向 | 它贡献了什么 | SemL0 的不同关注点 |
|---|---|---|
| LSM / key-value storage | 写友好 delta、compaction、空间 / 时间权衡 | 让图查询签名成为 LSM delta 区域的物理设计信号 |
| Dynamic graph storage | 可变图表示、事务图访问、新鲜分析 | 对最近 LSM 图 segment 的语义剪枝与 compaction |
| Adaptive indexing | 工作负载驱动的访问路径细化 | 带精确证明剪枝的反馈驱动语义 compaction |
| Materialized views | 逻辑查询结果维护与 view redefinition | 物理访问布局，而不是缓存查询答案 |
| Schema evolution | 跨逻辑 schema 变化的版本化解释 | 图 LSM segment `schema_epoch` 与保守剪枝回退 |

这种定位让论文 claim 保持具体：

```text
Property-graph query semantics can safely guide LSM delta layout, candidate
pruning, feedback compaction, and schema/snapshot metadata.
```

这种定位也澄清了当前 artifact 尚未证明什么。下一节将把这些边界集中到 Limitations 中。

## 9. Limitations And Future Work

SemL0 当前的证据包是有意收紧的。论文主张：属性图查询语义可以指导 LSM delta 布局、候选剪枝、反馈 compaction 以及 schema / snapshot 元数据。它并不声称当前原型是一个完整的图 DBMS、一个经过生产环境调优的存储引擎，或一个已经完成会议特定要求的最终提交 artifact。

因此，开放 gate 是显式的，而不是隐式的：

```text
final latest-code SF30/SF100 performance
production write-stall safety
full schema-migration engine
completed sustained feedback
completed store cleanup
final submission readiness
```

这些 gate 没有一个能通过“改写措辞”来关闭。它们都要求独立批准的执行阶段、必要时新增证据，以及刷新后的 claim-boundary 决策。

最重要的限制是规模新鲜度。当前 latest-code 性能 claim 只覆盖 SF1。该包包含 query-semantic layout、benefit-scored materialization、受控反馈适应、目标化 schema / snapshot 正确性以及写入 / 重写成本代理的 latest-code SF1 证据。但它不包含在最新 schema 和 snapshot 变化之后重新刷新的 latest-code SF30 / SF100 最终结果。更大规模的旧证据仍可用作历史背景，但在没有新的批准运行、重新生成表格以及更新后的 claim 决策之前，它们不能被用作当前最终性能 claim。

这些限制沿用与 Evaluation 相同的链路：

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

本节是这条链路的 claim-boundary 层：它说明哪些部分当前已有支持，哪些部分在论文能够强化 claim 之前仍需要独立批准的阶段。

反馈 compaction 同样是有边界的。当前 P3 证据显示的是受控工作负载迁移适应：反馈可以把 compaction 优先级移动到热点语义范围，且论文把 rewrite-cost proxy 与 read-byte 节省分开记录。持续反馈实验已经准备好，但尚未获批：

```text
experiment_approval_status=not_approved
safe_to_run_now=no
experiments_run=0
```

因此，论文不应声称生产级长时间适应、全局最优性、可忽略反馈开销或生产级 write-stall 安全性。未来若要强化，应使用新的日志目录、该目录下的新 benchmark store，以及 `paper/sustained-feedback-experiment-approval-packet.md` 中的接受标准。

用系统术语来说，这是 P3 的限制：P3 能在受控工作负载迁移下维护 C1 的 semantic pruning surface，但当前 artifact 并不能证明这层维护机制已经具备生产级调度、长时间运行能力或 write-stall 安全性。

写路径特征仍然是成本代理研究，而不是生产研究。当前证据可以讨论 import time、store bytes、file counts、manifest size、feedback compaction rewrite bytes 和 compaction-window latency proxy。它并不刻画并发生产流量下的写停顿、active-reader snapshot garbage collection、混合前台工作负载下的 compaction 调度，或资源隔离。这些都需要超出当前 source package 的工作负载与运行时控制。

Schema 演进的范围也是刻意收紧的。SemL0 支持论文中的如下 claim：增量 schema 变化不会使旧存储失效。Catalog epoch 会推进，未来写入使用当前解释，旧 segment 在各自的 `schema_epoch` 下仍然可读，而 unknown 或 mixed 元数据会被保守读取。这并不等于“完整物理 schema migration”。Rename、drop、type 或 encoding change，以及 label split / merge 行为，仍是边界案例或未来工作，除非新增实现与证据。当前属性边界仍是：定宽相等和 presence / absence 元数据；范围、字符串、复合谓词和 SQL null 语义仍是未来工作。

用系统术语来说，这是 P2 / P4 的限制：schema 与 snapshot 解释决定何时元数据可安全使用，但当前论文只声称目标化正确性和增量 no-rebuild 行为，而不声称完整的物理迁移引擎。核心回答仍然是：

```text
Schema changes do not automatically invalidate old storage.
```

正确性证据是目标化的，而不是穷尽性的。该包包含针对 schema epoch、tombstone、snapshot-visible delta、property epoch、alias / drop / encoding 边界、compaction 与 reopen 的源码级测试和摘要。这些测试支持在已实现边界内的 no-false-negative pruning；但它们并不能取代对任意动态图工作负载的随机差分测试，也不能取代穷尽的 schema-change 序列测试，更不能取代生产环境 active-reader snapshot-GC 验证。

Artifact 也刻意将 store cleanup 与 paper claim 分离。Store cleanup packet 列出了精确候选路径和必须保留的证据，但目前没有任何清理获批，也没有任何 store 被删除：

```text
delete_approval_status=not_approved
safe_to_delete_now=no
stores_deleted=0
```

未来的清理必须逐路径执行，限制在批准的候选列表中，并记录在独立的 cleanup execution stage 中。宽泛的模式、前缀或目录级清理都不是可接受的证据保留操作。

用系统术语来说，这是 P5 的限制：当前包是 source-ready 且 evidence-mapped 的，但 cleanup 与 final submission 是分离的执行 gate。文档阶段并不能授权删除，也不能授权最终就绪状态。

最后，论文并未达到 final-submission ready。当前 source package 已整理好，但 owner decision record 中的 venue 和 TeX route 仍未设置。剩余 gate 包括：会议 / 模板选择、匿名性和 appendix policy、页数预算、TeX backend approval、PDF 编译、表格和图像的视觉检查，以及最终的 source-to-PDF 一致性检查。在这些 gate 获批并执行之前，正确状态仍应为：

```text
final_submission_ready=no
```

### 边界总结

| 领域 | 当前支持的 claim | 尚不支持 |
|---|---|---|
| performance scale | latest-code SF1 evidence | final latest-code SF30/SF100 performance |
| feedback compaction | controlled workload-shift adaptation | production long-run adaptation or write-stall safety |
| write cost | import/store/rewrite proxies | production write-overhead characterization |
| schema evolution | additive no-rebuild old-storage readability | full physical schema migration |
| property predicates | fixed-width equality and presence/absence boundary | range/string/compound predicates and SQL null semantics |
| correctness | targeted schema/snapshot/tombstone regression coverage | exhaustive dynamic-graph differential proof |
| artifacts | source-ready MD/table/evidence package | venue-specific compiled final PDF |
| store cleanup | retention and approval packet | deletion execution |

开放 gate 既然已经明确，结论就可以回到这个有界系统贡献，而不引入新的证据或最终就绪 claim。

## 10. Conclusion

SemL0 表明：属性图查询语义可以在 LSM 物理设计边界上变得可见。标签、边类型、度类别、属性要求、schema epoch、snapshot 与运行时反馈，并不只是查询规划概念。在 SemL0 中，它们成为了面向存储的信号，用于 L0 segment 布局、候选剪枝和维护。

论文的贡献链与开头引入的是同一条：

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

C1 使用精确语义摘要减少 L0 读放大。基于收益评分的 materialization 让布局保持选择性，而不是 materialize 所有语义组合。P3 使用运行时反馈在受控工作负载迁移下调整语义 compaction 优先级。P2 阻止增量 catalog 变化让旧 segment 失效。P4 防止 tombstone、snapshot 与 mixed epoch 产生假阴性。

共享规则始终是保守的：

```text
metadata may reduce the candidate set only when it proves that a segment cannot
contain a visible match.
```

这条规则把布局、反馈 compaction、schema 演进与 snapshot-visible delta 连接成了一个统一存储设计，而不是一堆彼此独立的优化。当元数据精确时，SemL0 可以避免不必要的 L0 读取；当元数据是 unknown、mixed、legacy 或 tombstone-sensitive 时，它宁可 over-probe，也不冒错误答案的风险。P5 在论文层面保持了同样的纪律：当前 source-ready package 把 claim 映射到证据，同时把未批准实验、store cleanup、会议选择、TeX / PDF 生成、页数预算与视觉检查留在当前 claim 之外。

当前的 source-ready package 以 latest-code SF1 布局证据、benefit-scored materialization 证据、受控反馈工作负载迁移证据、目标化 schema / snapshot / tombstone 正确性以及写入 / 重写成本代理记账，支持这一有界贡献。它不声称最终的 latest-code SF30 / SF100 性能，不声称生产级 write-stall 安全性，不声称完整的 schema-migration engine，不声称已完成的 sustained feedback，不声称已完成的 store cleanup，也不声称已 final submission ready。

可以得到的有界结论是：

```text
query semantics can guide LSM physical design and maintenance while preserving
conservative correctness under schema evolution and dynamic deltas.
```
