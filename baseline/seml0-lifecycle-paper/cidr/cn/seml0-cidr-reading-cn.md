# SemL0：面向动态属性图存储的查询签名

**作者：** 作者姓名

**机构：** 机构，国家

**邮箱：** email@example.com

## 摘要

带类型邻居读取本身已经暴露了起点标签、边类型、方向以及其他图语义，但 LSM 存储通常仍然只依据键、层级和字节进行文件准入与合并调度。这种不匹配会造成语义读放大，并使 compaction（合并整理）抹去原本能够证明某个文件与查询无关的元数据。我们提出 SemL0，一个面向生命周期的原型系统。在 SemL0 中，query signature（查询签名）成为持久化的 storage-admission evidence（存储准入证据）。其契约允许在 exact evidence（精确证据）或安全的 conservative over-approximation（保守过近似）下跳过文件，但绝不会依据 unknown evidence（未知证据）跳过文件。文件预算决定哪些标签/类型分组获得 degree-exact L0 partitions（度数精确 L0 分区）；未被选择的分组仍保留模式级剪枝能力。

在 LDBC SNB 数据上的分阶段实验展示了这一权衡。在 SF10 上，budget-64 使用 13.3 GiB 存储、444 个 L0 文件和 2.26 GiB 导入峰值 RSS；完整语义物化则使用 14.6 GiB、737 个文件和 8.38 GiB RSS。在 SF100 上，两者对应的 RSS 分别为 2.21 GiB 和 118.03 GiB。受控 compaction 实验和真实的 10.9 亿边 SF30 运行表明，semantic merge（语义合并）能够保留剪枝面，而 naive merge（朴素合并）会破坏它；其中 SF30 的读取后果是 metadata-replay proxy（元数据回放代理指标），不是完整的 body-read speedup（正文读取加速）。本文的证据按生命周期阶段分解，而不声称一次端到端实验验证了完整生命周期。

**CCS 概念：** 信息系统－基于图的数据库模型；信息系统－数据结构；信息系统－存储管理。

**关键词：** 动态图，LSM-tree，属性图，存储布局，compaction

## 1. 动机与论点

属性图读取请求的是语义邻域，例如从 `Person` 出发的 `KNOWS` 出边，而不是一个无差别的键范围。LDBC SNB 将 typed neighborhood（带类型邻域）作为其工作负载的核心 [2]。动态图存储仍然适合采用 LSM 组织方式：更新进入可变组件，随后 flush（刷写）为不可变 segment（数据段），再由 compaction 重写 [1,3]。问题在于，一个选择性查询可能已经知道所需边类型，但存储元数据却无法证明大多数 L0 segment 与之无关，因此只能把这些 segment 纳入候选并检查。

这使问题不再只是查询时过滤。假设三个 segment 分别精确表示 Person 的熟人关系、Person 对 Post 的点赞关系以及 Forum 成员关系。naive merge 可以保留全部逻辑边，却输出一个 mixed segment（混合数据段）。后续查询因此失去跳过无关字节所需的证明。由此可见，compaction 并非 semantic-neutral（语义中性）：它会改变未来的 pruning surface（剪枝面）。

SemL0 研究的核心论点是：查询语义应当成为一个 budgeted storage control plane（有预算的存储控制平面）。其实现贡献包括：(1) 一个存储签名和明确的安全跳过契约；(2) 一个在强 schema layout（模式布局）与完整语义物化之间进行插值的预算算法；(3) 能够保留或重建有用证据的 semantic-aware compaction（语义感知合并）。本文主张的是“面向生命周期的设计，由各阶段分别提供证据”，而不是声称一次实验验证了完整生命周期。目前的属性支持也被刻意限定：存储准入只总结属性是否存在，而不支持任意等值、范围或 `WHERE` 谓词。

这一问题设定产生了三个具体的系统问题。第一，不让正确性依赖可选索引的前提下，不可变 segment 可以暴露哪些语义状态？第二，当 schema layout 已经能够提供有用的标签/类型剪枝时，有限文件预算应该花在哪里？第三，如何在不把每次 compaction 都变成完整语义重分区的情况下保留这些状态？本文给出以下贡献：

- 我们定义了一个存储准入接口，将查询语义签名与 MVCC 可见性、模式解析分离。它的 `Exact`/`Conservative`/`Unknown` 契约使每一次 segment 跳过都成为一项证明义务。
- 我们实现了文件预算化的 L0 布局。该布局根据配置需求或观测需求对起点标签/边类型分组排序，按预计的度数精确文件数收费，并使未选分组保持在 schema baseline（模式基线）。
- 我们把 semantic-surface retention（语义面保留）作为明确的 compaction 目标，并在受控输入和真实 SF30 元数据上测量其读写后果，同时明确说明每项实验的边界。

## 2. 设计

![图 1：SemL0 控制平面](../images/fig1_system_control_plane.png)

**图 1：** 与真实实现对齐的 SemL0 控制平面。`GraphAccessSignature` 包含存储准入事实；读取快照以及 schema/segment epoch 解析属于两个独立上下文。精确不相交、已证明的属性缺失，或者与查询不相交的保守过近似都允许 `SKIP`；未知证据或可能重叠则要求 `READ`。Compaction 通过 manifest（清单）、schema catalog（模式目录）和 degree sidecar（度数旁路元数据）重新发布证据。

### 2.1 准入证据

图 1 展示了已经实现的边界。图访问签名包含起点顶点及其标签、可选边类型、方向、可选度数类别和终点标签、可选记录时间范围，以及可选的属性存在性要求。两个时间戳字段是针对记录的 segment 准入范围，不是 MVCC snapshot（多版本快照）。读取 API 单独传递 snapshot。类似地，schema 名称与编码由 schema catalog 和各 segment 的 schema epoch 共同解析；epoch 不是签名字段。

Segment 元数据记录语义集合及其完整性。`Exact` 表示所记录集合与 segment 所表示的真实集合相等。`Conservative` 表示安全的过近似：它可以包含假阳性，但不会漏掉 segment 中真实存在的值，因此即使是保守集合，只要它与查询不相交，仍然足以证明可以跳过该 segment。`Unknown` 不提供集合包含关系保证，因此不能用于语义剪枝。`Mixed` 与这些状态不同：它是内容或布局状态，例如 mixed edge-type sentinel（混合边类型哨兵）或混合度数类别。它会使该维度上的所有相关查询都必须读取。这些规则将正确性与优化分开：证据较粗只会增加读取，不会产生 false negative（漏答案）。

物理单位仍然是 CSR segment，而不是一个逻辑边分组。其 header（文件头）记录层级、起点范围、排序键、方向、标签/类型分区、度数状态、时间戳范围、schema epoch、属性存在位图、tombstone（墓碑）标记以及摘要完整性。正文包含 offset array（偏移数组），随后是边记录和可选属性区。因此，许多起点顶点可以共享一个精确的 `(Person, KNOWS, Low)` segment；同一个元组也可能因为多次 flush 或 64 MiB 范围切分而跨越多个文件。Exact 描述的是每个物理文件所表示的值，并不意味着每个元组只有一个文件，也不意味着每个文件只有一个起点。

对于查询签名 $q$ 和 segment 摘要 $m$，令 $O_d(q,m)$ 表示查询与摘要在语义维度 $d$ 上可能重叠。读取准入规则为：

$$
\operatorname{admit}(q,m)=
\begin{cases}
\text{SKIP}, & \exists d:\neg O_d(q,m)\text{ 能被安全证明},\\
\text{READ}, & \text{其他情况}。
\end{cases}
$$

证明既可以来自精确集合，也可以来自仍与查询不相交的保守超集。对于未知证据，系统默认 $O_d$ 为真。Mixed 值在其自身维度上也采用相同处理，但不会抹去其他维度的证据：一个标签和边类型精确为 `Person/KNOWS`、但 degree 为 Mixed 的 segment，仍然可以拒绝 `Forum/HAS_MEMBER` 查询。起点范围和记录时间范围遵循相同规则。方向 `Both`/`Unknown` 以及查询中没有约束的维度都表示可能重叠，绝不能作为跳过证明。

属性位图支持两类条件：属性必须存在，或者缺失值/默认值也可接受。只有不含 tombstone 的 segment 才能依据“已证明属性缺失”进行剪枝。可能含有 tombstone 时必须读取。属性值等值查询使用独立的原型路径，并没有扩展签名；一般值谓词仍属于未来工作。

Snapshot 可见性被刻意放在准入之后计算。一个可能包含目标拓扑的 segment 会被读取，随后才按照 API 提供的 snapshot 合并记录版本。同样，在信任某项证据之前，catalog 会根据 segment 保存的 epoch 解析边或属性标识符。如果旧编码或 alias（别名）能够被精确映射，剩余摘要仍可用于剪枝；如果解析存在不确定性，该维度就回退为读取。这一顺序防止把记录时间范围误认为 MVCC snapshot，也防止把当前 schema 名称直接错误地应用到旧 segment。

### 2.2 有预算的 L0 布局

完整语义布局会按照 `(source label, edge type, degree class)` 对所有分组进行划分。这可能成倍增加文件和元数据。SemL0 为每个 `(source label, edge type)` 构造一个候选分组 $g$，测量其字节数 $b_g$，并依据 64 MiB 目标 segment 大小 $T$ 估算其度数精确文件成本：

$$
\begin{aligned}
c_g &= \left\lceil b_g/T \right\rceil, \\
f_g &= \sum_{r\in R_g}\min(q_r\max(a_r,1),10^6), \\
w_g &= \max(w_g^{cfg},f_g), \\
s_g &= w_g(b_g/1024)/c_g.
\end{aligned}
$$

其中，$R_g$ 包含分组 $g$ 已观测到的 range snapshot（范围快照）；$q_r$ 是某个范围的查询次数，$a_r$ 是其平均候选 segment 压力。九种核心 LDBC 边类型的配置权重为 4.0，它们的反向边权重为 2.0，其他边类型为 0.5。无效或缺失的反馈视为零，在取最大值前会对反馈进行封顶。候选按照 $s_g$ 降序排列；分数相同时优先边数更多的候选，然后使用稳定的标签/类型顺序。给定文件预算 $B$，只有满足 $\mathrm{used}+c_g\le B$ 的候选才会被选择；选择后 `used` 增加 $c_g$。W6 的 budget-64 行将字节门槛设为零，并设置 $B=64$。因此，64 表示最多收费的额外度数精确 L0 文件数，而不是 L0 文件总数，也不是每次 flush 都有 64 个文件。

虽然选择发生在 flush 阶段，但该账本的含义能够跨重启保持一致。Reopen（重新打开）时，`used` 只通过具有真实边类型并且具有精确、非 Mixed 度数类别的 L0 文件重建。具有真实边类型但 degree 为 Mixed 的 schema-style file（模式式文件）成本为零。这与准入策略一致：预算只为超出 schema 的精度付费，所以重启不会错误地把数千个已有 schema 文件计入 $B=64$，导致控制器永久耗尽。

对于入选候选，每个 source 根据其局部邻居数分类：Low 为 1–16，Medium 为 17–1024，High 为大于 1024。通过实现中 per-class benefit gate（每类别收益门槛）的度数类别会成为独立精确分区；低收益类别仍保持 Mixed。未入选候选则保留起点标签和边类型分区，但 degree 为 Mixed。它仍然具有 schema layout 的标签/边类型剪枝能力，只放弃额外的度数拆分。正因如此，budgeted control 是一种有界插值，而不是全有或全无的语义布局。

第二层门槛防止一个已选分组为每个观测到的类别都生成近乎空的文件。对于类别 $k$，设其字节数为 $b_k$、source 数为 $n_k$、source 总数为 $n$、最小精确字节为 $M$、配置 degree 权重为 $w_d$，Low/Medium/High 的类别权重 $\alpha_k\in\{1,1.25,2\}$，实现中的收益分数为：

$$
h_k=\frac{b_k}{\max(M,1)}\rho_k\alpha_k w_d,\qquad
\rho_k=\begin{cases}(n-n_k)/n,&n>n_k,\\1,&n=n_k.\end{cases}
$$

当所有 source 都属于同一类别时，实现将 competing-query fraction（竞争查询比例）视为 1；只有当 $h_k$ 达到配置阈值时，类别 $k$ 才保持 Exact，否则其 source 会并入该分组的 Mixed 分区。因此，group score（分组分数）决定文件预算可以花在哪里，而 degree score（度数分数）决定每个更细分区是否具有足够的局部收益。

### 2.3 语义保留

Flush 将 segment 元数据发布到 manifest，并更新 degree sidecar。读取在访问正文前，先将签名与这些证据比较。在 compaction 期间，semantic merge 按照有价值的精确分区组织输出；naive merge 则可能把键折叠为 Mixed sentinel（混合哨兵）。无法安全重建的输出会被降级，而不会被错误地信任。该设计并不要求每个阶段都保持 Exact，只要求每一次跳过都有充分依据。

由此形成的生命周期包含四个明确转换。(1) Flush 构造初始标签/类型分组，应用预算门槛和 degree 门槛，并把元数据写在不可变 CSR 正文旁边。(2) Open/reopen 从 manifest 和 schema catalog 重建内存准入索引，而不是根据文件名推断精度。(3) 读取使用这些证据，并更新按标签、边类型和起点范围组织的压力计数器。(4) Compaction 要么保留精确分组，要么根据解码后的记录重建精确分组，要么发布 Mixed/Unknown 输出。如果 manifest 发布前发生崩溃，先前版本仍然有效；成功发布的粗粒度输出依然正确，只会让未来读取纳入更多候选。

这里不能混淆两种反馈决策。前述预算公式决定哪些 flush 分组在全局文件预算内获得 degree 精度。另一个可选的 L0 compaction controller（L0 合并控制器）则根据查询次数、候选压力、offset-cache miss rate（偏移缓存未命中率）和预计重写 MiB，对观测到的起点范围进行排序。它选择的是物理重写目标，而不是一个新的签名字段。W7 检验第二种优先级在 workload shift 后是否跟随热点范围移动；C2 检验被选中的重写是否保留语义面。

## 3. 评估

评估回答四个有明确范围的问题。**RQ1：**语义证据能否减少 segment 准入，哪些指标可信？**RQ2：**文件预算能否在保留 schema baseline 的同时避免完整物化的资源悬崖？**RQ3：**compaction 保留精确分区时会形成怎样的读写权衡？**RQ4：**反馈移动以及 schema/snapshot 回退是否按设计工作？没有任何一次运行能够同时回答四个问题；本文把每项主张映射到真正执行该阶段的实验。

### 3.1 实验设置与变体

实验运行在一台 Intel Xeon 6982P-C 主机上，具有 64 个物理核心、128 个硬件线程和 496 GiB 内存，使用 blocking I/O（阻塞式 I/O），memgraph 和 segment 目标均为 64 MiB。W6 使用引擎冻结提交 `324a1e2`；C2 记录的阶段提交为 `c2aa16c`。SF100 对边类型 1、2、3、7、8、9、10、11 和 12 分别选择 5,000 个确定性 stride sample（步长样本），每次重复共执行 45,000 个操作；SF10 每类使用 1,000 个。实验没有随机数种子，因为采样不是随机的。本文报告三次进程内重复，没有显式 warmup，也没有清空操作系统 page cache。Percentile（百分位数）是各次重复中各边类型百分位数的均值，不是所有操作合并后的全局百分位数。所有参与比较的 W6 行都在同一 snapshot 上通过 count/hash 检查，mismatch 为零。

九个 ID 分别对应 LDBC 关系 Knows、HasCreator、HasTag、LikesComment、LikesPost、ReplyOfComment、ReplyOfPost、ContainerOf 和 HasMember。Candidate L0 会统计正文过滤前被准入的每个 L0 文件，包括不同操作对同一文件的重复准入。Import RSS 是 loader 进程的峰值常驻内存；store size 是导入后实际分配的磁盘字节；L0 files 来自 manifest。Latency 包括 typed-neighbor driver 内部的准入和正文处理，但不包括数据导入。

| 变体 | 已实现的布局/准入证据 | SF10 Store GiB | SF10 L0 files | Peak RSS GiB（SF10/SF100） |
|---|---|---:|---:|---:|
| naive | 每次 flush 输出一个不拆分文件；不主动进行语义分区 | 13.4 | 170 | 2.22 / 2.43 |
| kv-lsm | 按范围限制的 KV 风格顺序，语义元数据为 Unknown/Mixed | 13.4 | 170 | 2.33 / 2.49 |
| schema | 按起点标签/边类型分区；degree 保持 Mixed | 13.3 | 376 | 2.12 / 2.42 |
| edge-only | 按边类型分区；起点/终点标签和 degree 较粗 | 13.3 | 380 | 1.79 / 2.53 |
| budg-b64 | schema 基础加上在 $B=64$ 下按分数选择的 degree-exact 分区 | 13.3 | 444 | 2.26 / 2.21 |
| semantic | 所有起点标签/边类型分组都参与 degree-class 拆分 | 14.6 | 737 | 8.38 / 118.03 |
| oracle | 使用精确内存 L0 索引作为剪枝上界参考 | 13.4 | 170 | 2.27 / 2.67 |

**表 1：** 已实现变体及导入资源。Store size 和 L0 file 数来自 SF10；RSS 以 SF10/SF100 表示。不应预期 budget-64 在每一种 workload 上都优于强静态 schema baseline。

外部 artifact 还包含 LiveGraph、Aster RocksGraph、TuGraph、NebulaGraph 和 Neo4j 在 SF10 上通过 digest 的运行。它们与 W6 的 loader、查询次数和范围不同，因此本文只用这些结果说明接口覆盖和可复现性，不进行端到端系统速度排名。

### 3.2 预算与资源权衡

![图 2：W6 SF100 预算与资源权衡](../images/fig2_sf100_read_budget.png)

**图 2：** W6 SF100。(a) 将每个指标除以 naive 值，并使用对数坐标；candidate L0 是主要指标，read bytes 是次要指标，因为 offset-array/cache over-read（偏移数组/缓存过读）会产生较高方差。(b) 是特定工作负载下的延迟分布；budget-64 与 schema 之间的延迟 gate 为 `FALLBACK`。(c) 展示 full-semantic memory cliff（完整语义内存悬崖）。

图 2(a) 表明语义元数据改变了准入：naive 和 kv-lsm 分别累计 49.26M 次 candidate-L0 准入，而 schema 为 5.93M，budget-64 为 6.02M。Read-byte 测量仅用于完整展示，不是主要结论：metadata cache miss 后，reader 可能重新读取完整 offset array，并且 SF100 标准差很大。同样，面板 (b) 不能证明 budget-64 相对 schema 有稳定延迟优势；它们的一倍标准差 gate 为 `FALLBACK`。

面板 (a) 中的“normalized cost (naive=1, log)”是对每个指标 $x$ 和每个变体 $v$ 计算 $x_v/x_{naive}$，再使用对数轴显示，使 candidate、byte 和 file 比率能够同时可见。它不是一个加权综合分数。Schema 将 candidate admission 降到 naive 的 12.0%，budget-64 为 12.2%。Full semantic 准入 7.78M 个 candidate，因为更细的文件会改善部分维度，但也会增加与某个起点范围重叠的文件数。Oracle 的精确内存 L0 索引只准入 0.53M 个 candidate，但它是在导入后构建的上界参考，不是具有可比构建成本的持久化布局。这些结果说明，文件数和元数据精度必须结合解释。

主要的预算结果是资源边界。表 1 表明，在 SF10 上，budget-64 保持在 13.3 GiB 和 2.26 GiB RSS，而 full semantic 增长到 14.6 GiB 和 8.38 GiB RSS。在 SF100 上，full semantic 达到 118.03 GiB，而 budget-64 为 2.21 GiB。Schema 是一个强静态 baseline；budgeted control 增加了资源上限以及重新定向精度的能力。在一个从真实 SF30 派生的 W7 workload shift 中，feedback-only control 在两个阶段都选择了热点分区，并在热点范围变化后移动了所选范围。这是派生 workload 证据，不是生产 trace，但它检验了静态 schema 不具备的机制。

因此，budget-64 不应被描述为 schema 的通用替代。在该读取矩阵上，它的 candidate 数略高，而且延迟置信 gate 无法区分二者。它的额外价值是对 degree 精度设置硬性账本边界，并为 workload adaptation（工作负载自适应）提供输入。Full semantic 展示另一个端点：更多元数据能够降低该 workload 上的部分读取字节，但其文件和导入内存成本在 SF100 上变得不可接受。有意义的比较是资源/性能包络，而不是宣称某一个 b64 工作点支配所有静态布局。

### 3.3 Compaction 保留

![图 3：C2 compaction 语义面保留](../images/fig3_c2_lifecycle_retention.png)

**图 3：** C2 compaction 证据。(a) retention 等于合并后的 exact-surface ratio 除以合并前的 ratio。(b) write amplification 等于输出字节除以逻辑更新字节。(c) 等于合并后的总 candidate bytes 除以合并前的 exact partition bytes；SF30 是元数据回放，不是正文读取执行。

对于 C2，exact-surface ratio 是 topology-exact segment（拓扑精确数据段）中的边数除以该次 merge 覆盖的全部边数。Retention 是合并后 ratio 与合并前 ratio 的比值。受控 synthetic input（合成输入）刻意构造四个和六个精确的起点标签/边类型分区，从而把 merge policy 隔离为因果变量。Naive merge 将每组分区折叠为 Mixed 输出，使 retention 变为 0，并产生 4x/6x 完整读取放大。Semantic merge 保持 retention 为 1、read cost 为 1x；其 write amplification 为 1.87/1.85，而 naive 为 1.22/1.14。

更形式化地，对一次 merge 覆盖的逻辑边多重集合 $D$，令 $E(M)$ 表示在元数据状态 $M$ 下位于 topology-exact segment 中的边。三个报告量为：

$$
\operatorname{ret}=\frac{|E(M_{after})|/|D|}{|E(M_{before})|/|D|},\qquad
\operatorname{WA}=\frac{\text{output bytes}}{\text{logical update bytes}}.
$$

读取后果代理指标是：合并后元数据所准入的总字节，除以合并前每个被查询精确分区的字节。分母是理想情况下保留精确分区后需要暴露的正文，而不是整个数据库。所谓“post-merge segment metadata（合并后数据段元数据）”，是指使用与前述准入公式相同的契约，将每个起点标签/边类型签名与输出 header 比较；它不表示输出正文已经被解码。

前两行使用 synthetic data 是合理的，因为它在保持逻辑内容不变的同时控制精确分区的数量和大小。Naive 输出因此必须准入全部四个或六个等大分区，使 4x/6x 后果可以直接归因于 merge policy，而不是 LDBC 数据倾斜。真实 SF30 行则使用 loader 和 manifest 实际生成的分区提供 external validity（外部有效性）。

真实 SF30 运行处理全部 10.9 亿条有向边。排空 L0 后产生 40 个实际观测到的起点标签/边类型分组和 528 个精确 L1 segment；因此 40 来自数据，不是任意选择的查询次数。Naive L1-to-L2 merge 使 retention 为 0、write amplification 为 1.07；semantic merge 使 retention 为 1、write amplification 为 1.24。面板 (c) 在合并后元数据上回放这 40 个签名。Naive merge 相对于 43.25 GB 的精确分区字节准入 282.16 GB，即 6.52x；semantic merge 准入 43.25 GB，即 1.00x。这一回放不执行正文解码，也不支持 SF30 延迟结论。

### 3.4 生命周期覆盖与正确性

证据按阶段分解。W6 测量 flush layout、准入和资源；C2 隔离 compaction retention 与重写成本。W7 使用由真实 SF30 CSV 派生的 store，包含两个阶段，每个阶段执行八次 flush、64 个热点 source，以及每个 source 八次查询。Feedback-only 和 static-budgeted policy 都会在每个阶段第一次 flush 后合并热点范围；feedback-only 在阶段 B 移动到新范围，而 no-feedback 不执行热点 compaction。在最后一次 flush 中，被选 compaction 后，每次查询的 candidate segment 从 2 降到 0，body read 从 3 降到 1。这是受控 workload-shift 机制测试，不是完整 SF30 生产 trace，也不能证明 flush budget 本身能够预测未来需求。

W9 在以 SF30 store 为起点的环境中运行 30 分钟混合读写，并记录六个 checkpoint（检查点）。在最后一个 checkpoint，schema 的 p99 为 8,740.2 μs，semantic 为 1,274.0 μs，吞吐约为 163 query/s，writer error 为零。然而，所有变体的 rewrite bytes 都为零。因此，该趋势只能证明持续 flush、L0 累积和起始布局的保持；它没有执行并发 semantic compaction，不能验证完整生命周期。

W13 是一个有界的十项测试套件。它检查 compaction/reopen 期间的混合 schema epoch、snapshot 下的属性 delta、旧 segment 可读性、catalog 持久化、报告边界、定宽属性编码变化、边标签 alias、行编码 epoch、属性删除行为，以及新增边标签后的 Exact/Mixed 剪枝。十项测试全部通过。这些是实现正确性测试，不是通用在线 schema migration benchmark（模式迁移基准）。这些阶段共同支持面向生命周期的设计，但不假装某一次运行端到端执行了整个生命周期。

因此，measured/proxy（实测/代理）边界是明确的。W6、W7、W9 和 W13 分别执行各自对应的原型路径；受控 C2 执行 merge 和读取后果；真实 SF30 C2 执行 merge，但读取后果只回放元数据。SF100 read bytes 虽然是实测值，但由于已知 reader over-read，只作为次要指标。论文中的每个主要数字都来自已完成的 artifact，但 artifact 完成并不意味着不同范围可以互换。

## 4. 相关工作

动态图存储研究了事务性邻接扫描和多版本结构 [4,5]。GraphOne 和近期 DGS 研究考察了可变图表示及其权衡 [6,7]。LSMGraph 将动态更新与多层 CSR 结合 [3]；SemL0 则关注同时用于读取准入和物理重写的语义证据。这些系统已经表明，更新路径、版本管理和邻接布局对演化中的图非常重要。它们并未提出相同主张，即属性图访问签名应当被持久化为 proof object（证明对象），并在 LSM 重写过程中得到保留。我们的外部 artifact 仅通过共同的 typed-neighbor 正确性 gate 运行若干系统；loader、API 和操作数不同，因此不能进行端到端速度排名。

BACH 已经使用 workload 和 degree distribution 调整基于 LSM 的图格式与 merge 选择 [8]。ArceKV 根据不断变化的 KV workload 调整通用 LSM compaction [9]。因此，SemL0 并不声称自己是第一个 workload-aware merge scheduler（工作负载感知合并调度器）。它的区别在于被保留的对象：属性图语义证据、安全跳过的证明规则，以及跨 compaction 测量的 semantic-surface retention。Functional storage decomposition（功能化存储分解）主张将访问需求与固定格式分离 [10]。面向图的列式布局也提出了图访问需要专用物理组织的相关观点 [11]。

传统 LSM filter 和 range metadata 同样能够拒绝无关文件，但它们的证据通常基于键或成员关系。SemL0 的维度包括图标签、带类型关系、方向、局部度数边界和属性存在性，并且这些维度的解释可能依赖 schema catalog。创新点不在于“摘要能够剪枝”，而在于：摘要完整性属于存储契约；预算选择从 schema-aware baseline（模式感知基线）出发；并且 compaction 是否保留未来的证明面会被明确测量。这一定位比新的图查询引擎更窄，但比只作用于 L0 的过滤器更广。

## 5. 局限与结论

SemL0 是原型，不是完整属性查询引擎或生产级图数据库。除属性存在性外的属性值处理仍然只是原型。SF100 read bytes 存在 reader over-read caveat；SF100 latency 没有通过 budget-64/schema 分离 gate；W7 来自真实数据派生 workload，而不是 trace；SF30 C2 的读取后果是元数据代理。Schema/snapshot 证据来自有界正确性套件，不是通用在线迁移。

当前预算优化的是预计文件单位，而不是 CPU 时间、长期写放大或多租户目标。其权重由配置常量与局部反馈组成，Low/Medium/High 阈值也是固定值，而非学习得到。生产级控制器还需要反馈衰减、准入迟滞、后台 I/O 限制，以及 flush 精度与 level compaction 之间的协调。更丰富的属性谓词还需要能够跨编码变化保持精确性的值摘要。这些扩展可以遵循相同证明规则，但本文没有评估它们。

该设计最适合不可变图 segment 上的选择性 typed-neighbor workload。请求所有边类型的扫描几乎无法从语义分区获益；由高度数 hub（枢纽顶点）主导的 workload 可能付出更多文件和 merge 成本；如果一个存储的 schema 已经为每种边类型创建独立物理关系，它可能只需要 retention contract（保留契约）。Budgeted control 的价值恰恰出现在 workload selectivity（工作负载选择性）和 materialization cost（物化成本）都不均匀的场景。

在这些边界内，结果是一致的：图查询语义可以成为持久存储证据；每一次跳过都必须遵循精确性契约；语义证据需要文件/资源预算；compaction 必须显式保留或降级这些证据。把 compaction 视为 semantic rewrite（语义重写），是本文最核心的系统启示。

## 参考文献

1. P. O'Neil, E. Cheng, D. Gawlick, and E. O'Neil. “The log-structured merge-tree.” *Acta Informatica*, 33(4):351–385, 1996.
2. O. Erling et al. “The LDBC social network benchmark: Interactive workload.” *SIGMOD*, 2015.
3. S. Yu et al. “LSMGraph: A high-performance dynamic graph storage system with multi-level CSR.” *Proceedings of the ACM on Management of Data*, 2(6), Article 243, 2024.
4. X. Zhu et al. “LiveGraph: A transactional graph storage system with purely sequential adjacency list scans.” *PVLDB*, 13(7):1020–1034, 2020.
5. D. De Leo and P. Boncz. “Teseo and the analysis of structural dynamic graphs.” *PVLDB*, 14(6):1053–1066, 2021.
6. P. Kumar and H. H. Huang. “GraphOne: A data store for real-time analytics on evolving graphs.” *FAST*, 2019.
7. J. Su et al. “Revisiting the design of in-memory dynamic graph storage.” *Proceedings of the ACM on Management of Data*, 3(1), Article 70, 2025. DOI: 10.1145/3709720.
8. J. Huang, Y. Cao, S. Ren, B. Wu, and D. Miao. “BACH: Bridging adjacency list and CSR format using LSM-trees for HGTAP workloads.” *PVLDB*, 18(5):1509–1521, 2025.
9. J. Liu, H. Xie, and S. Luo. “ArceKV: Towards workload-driven LSM-compactions for key-value store under dynamic workloads.” *PVLDB*, 19(5):958–972, 2026.
10. M. Prammer et al. “Towards functional decomposition of storage formats.” *CIDR*, 2025.
11. P. Gupta, A. Mhedhbi, and S. Salihoglu. “Columnar storage and list-based processing for graph database management systems.” *PVLDB*, 14(11):2491–2504, 2021.
