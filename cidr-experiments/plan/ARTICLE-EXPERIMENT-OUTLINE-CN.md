# SemL0 论文实验大纲（七天冻结执行版）

本大纲决定最终论文必须覆盖的 RQ、指标和正确性边界。正式执行受 `168 h` clean-window 硬上限约束，因此采用预注册的代表性配置而不是穷举所有笛卡尔积；具体阶段、复用项和停止条件以 `../PROGRESS-CN.md` 第 3 节为准。七天从服务器释放且 clean-window sentinel 通过时起算，等待其他用户任务释放的时间不计入实验 wall-time。

## 评审总览

| 模块 | 要回答的问题 | 正式产物 | 已有数据如何复用 | 确认后需要补的正式证据 | 七天计划位置 |
|---|---|---|---|---|---:|
| E00 | 实验是否可复现、比较是否公平？ | Table A：环境、版本、数据、协议 | 复用已有 hash/registry；补齐当前 host/binary/store manifest | shared truth、ID bridge、clean-window sentinel | 0--14 h |
| E01 | 相同接口和查询列表下，相对现有系统如何？ | Fig A + Table B | 复用 1700-query truth、correctness digest 和通过 gate 的 stores | SemL0 四配置 + 五个外部系统 matched timing/resource | 14--26 h |
| E02 | 是否能做完整 LDBC 请求路径比较？ | 条件性附图/表 | 只复用能证明语义等价的 adapter/query | adapter gate 通过才跑；否则正式标 `BLOCKED` | 42 h 缓冲中的可选项 |
| E03 | 每个核心设计分别贡献什么？ | Fig B：A0--A6 staircase | W6 layout facts 用于选点和解释异常 | 正交 feature switches；SF10 完整、SF30 代表复验 | 26--40 h，与 E04/SF10 共享 |
| E04 | 性能收益带来多少 CPU/内存/磁盘/I/O 开销？ | Fig C：budget/resource Pareto | 复用 W6 import raw 和旧九点探索结果，不重复探索矩阵 | 六个正式 budget 点、全字段 telemetry、三类 workload 的冻结子集 | SF10 共享；SF100 40--80 h |
| E05 | 更新和 compaction 后 pruning 是否持续有效？ | Fig D + Fig E | 复用 C2 retention/WA/proxy 机制证据和 RQ3 raw | full-read 校准、real SF30、四策略 fixed trace×3 | 80--94 h |
| E06 | 是否适用于不同 query/update workload？ | Fig F：coverage heatmap | 复用 W8 property/2-hop、W7 shift、W13 schema tests | 预注册分层代表 cells；每维至少有正式证据 | 94--102 h |
| E07/E08 | 数据规模和并发增长时趋势如何？ | Fig G + Fig H | 复用通过兼容性 gate 的 immutable stores | SF1/10/30/100×四配置；并发 1/4/8/16/32 | 102--112 h |
| E09 | 保守 fallback 是否始终安全、代价多少？ | Table C | W13 10/10 bounded correctness 可直接作为一行 | 3 seeds×至少 20,000 ops differential stress | 112--117 h |
| 封板 | 数据能否正式进论文？ | raw/normalized/manifest/hash + 统计摘要 | 复用现有 normalizer 与 formal plotting gates | 缺字段检查、claim-to-evidence 审计 | 117--126 h |

期望执行为 `126 h`，另有 `42 h` 只用于 correctness 修复、缺字段重跑、高方差点从 3 次补到 5 次，以及语义 gate 已通过时的 E02。当前阶段只评审大纲，不启动任何补跑。

## 0. 实验总目标

实验部分需要形成一条完整证据链：

1. **Effectiveness**：在 SemL0 实际支持的查询接口上，能否降低端到端读延迟、尾延迟和物理读放大？
2. **Causality**：收益分别来自 query-signature admission、semantic routing、budgeted materialization 还是 lifecycle-aware compaction？
3. **Cost**：这些收益带来了多少 CPU、内存、磁盘、文件数和写放大？
4. **Generality**：效果是否只存在于一种 typed-neighbor workload，还是覆盖 property、degree、2-hop、动态读写和 workload shift？
5. **Scalability**：数据规模和并发增加后，收益及开销如何变化？
6. **Safety**：schema、snapshot、tombstone、旧 metadata 和 reopen 场景下是否保持零 false negative？

所有实验必须同时给出性能结果和 correctness gate，避免用更少工作量换取表面上的低延迟。

---

## 1. Experimental Setup

### 1.1 实现与版本

- SemL0 Git SHA、dirty 状态、release flags、Cargo features、binary SHA-256。
- 外部系统版本、commit/image digest、编译参数和客户端版本。
- 自动 compaction、durability、fsync、cache、线程数、NUMA/CPU affinity。

### 1.2 硬件与软件环境

- CPU 型号、物理核/逻辑核、NUMA；内存和 swap。
- 存储设备型号、filesystem、mount options、容量和实验前空闲空间。
- OS、kernel、Rust/C++/Java 版本。
- 正式时延实验必须在服务器负载释放后执行；记录实验前/中的 host load。

### 1.3 数据与 workload

- LDBC SNB SF1/SF10/SF30/SF100 的 vertex/edge/property 数、输入哈希。
- dense ID 与 original ID 的双向映射及哈希。
- 每份 query truth/trace 的 seed、采样方法、查询数、类型分布、方向、degree/selectivity 分层。
- 冷缓存与热缓存结果分开；不得混合求平均。

### 1.4 统计方法

- 所有正式点先做 3 个独立进程 run；若 run-level QPS CV >3% 或 P99 CV >5%，则从预留缓冲补到 5 个。长时动态、导入和 compaction 固定至少 3 个独立 run。
- variant 执行顺序随机化或轮换，避免固定顺序和缓存偏差。
- 报告所有独立 run 点、run-level median/mean 和不确定性；n=3 时报告范围并标注样本数，n>=5 时再报告 bootstrap 95% CI。每个 run 内报告 P50/P95/P99。
- checkpoint 只用于时间序列，不视为独立重复。

---

## RQ1：SemL0 在相同接口和相同查询列表上，相对现有系统表现如何？

### E01：Matched typed-neighbor end-to-end

**Claim**：在固定 source、outgoing typed-neighbor 这一当前优化接口上，SemL0 的端到端收益可以在严格 apples-to-apples 条件下测量。

**系统**：

- SemL0：`naive`、`schema`、`budg-b64`、`semantic`。
- 外部：LiveGraph、Aster RocksGraph、TuGraph、Neo4j、NebulaGraph。

**协议**：

- SF10，同一 dense edge set。
- 所有系统直接消费同一个版本化 query truth 文件；第一版可采用 34 edge types × 50 sources，即 1700 queries。
- 相同查询顺序、warmup、并发、计时边界和 correctness digest。
- 分开报告 positive/outgoing scope 与 unsupported/negative direction，不用不同接口混成一个平均数。

**主指标**：QPS、Avg/P50/P95/P99、correctness mismatches。

**次指标**：load/build time、open time、CPU/op、peak/steady RSS、disk footprint、physical read bytes。

**边界**：这是 matched interface benchmark，不等同于完整图数据库功能排名。

### E02：支持范围内的 LDBC 端到端

**Claim**：如果 SemL0 的查询适配层能执行一组明确的 LDBC query templates，则可比较完整请求路径，而不只比较 storage kernel。

**设计**：

- 先审计 SemL0 可完整执行的 LDBC templates；只纳入语义和结果完全等价的查询。
- 使用同一 operation stream、参数文件、客户端并发和 correctness oracle。
- 系统至少包括 SemL0、TuGraph 和一个成熟 DBMS；LiveGraph/Aster 仅在能表达同一查询时进入。
- SF10 为主，时间允许再做 SF30；并发 1/8/16/32。

**主指标**：throughput、P50/P95/P99、超时率、正确率。

**边界**：若查询适配层不完整，E02 保持 `BLOCKED`，不得用 legacy adjacency-cache 结果代表 SemL0 CSR 路径。

---

## RQ2：每个核心设计分别贡献了什么？

### E03：查询控制面逐组件消融

采用单因素阶梯；若当前 layout enum 无法隔离组件，需要增加独立 feature/config switches。

| 阶段 | 配置 | 隔离的问题 |
|---|---|---|
| A0 | Naive LSM-CSR | 无语义控制的锚点 |
| A1 | + exact label/type evidence，关闭 semantic routing | metadata 判断本身的收益/CPU |
| A2 | + semantic L0 index/routing | 路由结构减少多少 candidate lookup |
| A3 | + budgeted degree promotion | 额外语义维度的边际收益与 fanout |
| A4 | + feedback-based priority | query feedback 是否优于静态/size-only |
| A5 | + semantic-aware compaction | pruning surface 是否跨 lifecycle 保留 |
| A6 | Full SemL0 | 完整系统工作点 |

**数据/workload**：SF10 对 A0--A6 完整执行 typed one-hop、degree-stratified、property-presence；SF30 对 A0/A2/A4/A6 做三类 workload 的代表性复验。由此覆盖每个组件和每种语义，但不把 SF30 全阶梯重复一遍。

**主指标**：latency/QPS、candidates/op、body reads/op、read bytes/op。

**代价指标**：CPU/op、metadata lookup/build time、RSS、metadata bytes、L0 files、compaction write amplification。

**必须解释**：当前 W6 中 edge-type-only 与 richer semantic 接近，且 budget 增大时 candidate 数不单调；新消融必须解释文件碎片、routing 和 body-read 的相互作用。

---

## RQ3：预算控制带来怎样的性能—资源权衡？

### E04：Budget and resource tradeoff

**配置**：固定六点 `naive`、`schema/no-degree`、B=64、256、1024、full semantic；旧 `kv-lsm/edge-type-only/oracle` 只作为明确标注的历史 appendix 数据，不在正式主矩阵重复执行。

**workload**：uniform、Zipf/hot-edge-type、workload shift 三种分布。七天矩阵中，六个 budget 点都执行 uniform；`naive/schema/B64/full semantic` 再执行 Zipf 和 shift，避免六点乘三分布的完整笛卡尔积。

**阶段**：

1. import/build；
2. cold open/rebuild；
3. warm read-only；
4. mixed read/write；
5. compaction。

六个 budget 点都采 import、cold open 和 warm read；mixed 与 compaction 阶段在 `naive/schema/B64/full semantic` 四个核心点执行。所有列出的 workload 和生命周期阶段都有正式证据，但不声称穷举其全部组合。

**资源指标**：

- wall/user/system CPU、CPU/op；
- peak RSS、steady RSS/PSS、semantic-index memory；
- payload、metadata、catalog、sidecar、manifest、WAL 分项大小；
- total/temporary disk peak、file count、bytes/edge；
- actual read/write bytes、compaction input/output、write amplification；
- load/build/reopen time。

**预期回答**：B64 是否在接近 schema 资源包络时获得主要收益；full semantic 的额外收益是否值得 RSS、文件数和磁盘代价。

---

## RQ4：语义 pruning surface 能否在真实更新和 compaction 中持续有效？

### E05：Compaction lifecycle and dynamic mixed workload

**策略**：

- `none`：只作 L0 累积诊断，不进入最终“稳态”结论；
- `capacity-naive`：相同触发条件下不保留语义表面；
- `semantic-static`：语义感知重写，固定策略；
- `semantic-feedback`：根据查询压力选择目标。

**两阶段验证**：

1. Controlled SF1/SF10：同时执行 full body read 和 metadata replay，校准 proxy。
2. Real SF30：固定 update/query trace，真实 compaction，完整查询执行。

**主指标**：post-compaction latency、read bytes、candidate bytes、surface retention、mismatches。

**维护代价**：compaction count/wall/CPU/RSS、input/output bytes、write amplification、temporary disk、writer throughput、stall 和最大 writer latency。

**时间序列**：标注 flush/compaction、L0 files、candidate bytes、CPU、RSS、QPS 和 P99 事件线。

**必须修正的旧口径**：W9 的 `COMPACT_EVERY_SECS=0`，不能作为 compaction 闭环或低维护成本证据。

---

## RQ5：SemL0 对不同查询和更新 workload 是否都适用？

### E06：Workload coverage matrix

| 维度 | 取值 |
|---|---|
| Edge selectivity | rare / medium / frequent type |
| Source degree | Low / Medium / High |
| Query semantics | typed-only / destination label / record time / property presence / property absence |
| Traversal | one-hop / two-hop |
| Read-write ratio | 100/0、90/10、50/50、10/90 |
| Update pattern | uniform / burst / hotspot |
| Workload evolution | stable / A→B hotspot shift |
| Schema state | stable / epoch change / alias-drop / encoding change |
| Version state | low/high tombstone、old/new snapshots |

**复用基础**：W8 property/2-hop、W7 workload shift、W13 schema tests。

**七天代表集**：采用预注册的分层覆盖表，确保 rare/medium/frequent、low/high degree、typed/property/two-hop、四种读写比、uniform/burst/hotspot、stable/shift、schema/version 异常均至少出现一次；不运行所有维度的笛卡尔积。主性能矩阵使用 `naive/schema/B64/semantic`，prototype/unsupported 路径只做正确性覆盖。

**注意**：property equality 当前属于独立 prototype，必须单独标注；`In/Both` 在形成等价优化路径之前只做功能/正确性覆盖，不做性能优势宣称。

---

## RQ6：随数据规模和并发增加，性能与开销如何变化？

### E07：Data-size scaling

**规模**：SF1、SF10、SF30、SF100；可选增加严格 2× 的 SF1/2/4/8/16/32。

**配置**：naive、schema、budg-b64、semantic。

**协议**：每个 scale 使用相同采样算法、相同 query 数量和相同分层比例；结果统一按 op/edge 归一化。

**指标**：latency/QPS、candidates/op、read bytes/op、import throughput、CPU/op、RSS、disk、L0 files、compaction WA。

**展示**：对数据边数采用 log-x，报告绝对值和相对 naive/schema 的倍率；至少四点，不把 SF10→SF100 两点称为完整 scaling curve。

### E08：Concurrency scaling

**并发**：1/4/8/16/32；统一 CPU quota/cpuset，避免一个系统使用更多核。

**指标**：QPS、P50/P95/P99、CPU utilization、context switches、RSS、I/O bandwidth、stall/timeout。

**边界**：这是单机并发扩展，不是分布式 scale-out。

---

## RQ7：保守 fallback 是否始终安全，其代价是什么？

### E09：Differential correctness and recovery stress

**场景**：

- exact/conservative/unknown/mixed metadata；
- schema epoch 与 property encoding change；
- tombstone、version blocker、不同 snapshot；
- compaction 前后、reopen/rebuild、旧格式 metadata；
- property IDs > 64、缺失/默认属性；
- 随机 update/query/flush/compaction/reopen 序列。

**oracle**：dense truth 或 semantics-blind path；逐查询 count/hash digest。

**规模**：至少 3 个固定 seed、每 seed 至少 20,000 个操作；若发现失败则保留并自动缩减最小复现 trace。

**指标**：false negative 必须为 0；另报告 fallback rate、extra candidates/read bytes 和恢复时间。

---

## 2. 完整论文图表清单

| 编号 | 建议产物 | 来源 |
|---|---|---|
| Table A | Experimental setup、系统版本、数据集规模 | E00 |
| Fig A | Matched typed-neighbor latency/QPS | E01 |
| Table B | Load/CPU/RSS/disk 外部系统对比 | E01 |
| Fig B | Query-control staircase ablation | E03 |
| Fig C | Budget 的性能—RSS—磁盘—文件数 Pareto | E04 |
| Fig D | Compaction retention、full-read consequence、write amp | E05 |
| Fig E | 动态 workload 事件时间线 | E05 |
| Fig F | Property/degree/2-hop/workload-shift heatmap | E06 |
| Fig G | SF1/SF10/SF30/SF100 scaling | E07 |
| Fig H | 并发扩展与 tail latency | E08 |
| Table C | Correctness/fallback coverage 与 false-negative gate | E09 |

## 3. 最终可支持与不可支持的结论

完成 E01/E03-E09 后，可支持：

- query-semantic evidence 在明确的 supported interface 上减少读放大并改善端到端性能；
- budgeted control 在接近 coarse baseline 的资源包络内保留主要收益；
- semantic-aware compaction 以可量化的写入和资源代价保持 pruning surface；
- safety-first fallback 在动态 schema/version 场景下保持零 false negative。

即使实验完成，也不自动支持：

- SemL0 是完整生产图数据库；
- 在所有 property-graph 查询上都优于外部系统；
- 更大的 semantic budget 单调改善性能；
- 单机数据/并发 scaling 等于分布式 scale-out。
