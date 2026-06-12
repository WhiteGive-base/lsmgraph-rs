# SemL0 顶会差距全面分析（目标 SIGMOD 2027）

> 2026-06-12。事实源：`/data/WorkSpace/lsmgraph-rs`，分支 `codex/sf100-basegraph-bench`，HEAD `e3c178d`。
> 范围：在论文草稿（`baseline/seml0-linux-main-paper-draft-cn-20260611.md`）和补跑清单（`baseline/seml0-linux-rerun-plan-20260611-cn.md` 的 P0-P7）**之外**，还差什么才够顶会，以及代码要怎么改。
> 依据：对 SF100 原始数据（`remote-logs/qslsm-sf100-strong-baseline-20260610/`）和内核代码的逐行核查，所有断言均给出 file:line 或数据出处。

## 0. 总评

方向是成立的：query-semantic L0 物理设计 + budgeted materialization + feedback compaction 是一个有研究点的组合，SF30/SF100 内部消融完整，naive 对照已锁定（read -99.4%，candidate -88%）。**当前离顶会的差距不在"再跑一个更大的数据集"，而在三类问题**：

1. **证据完整性**：核心卖点（budgeted 延迟最低）建立在单次测量上，且 IO 计数器解释不了延迟差异（G1/G2）；
2. **结构性风险**：三处会被审稿人/artifact evaluation 直接抓住的硬伤——kv-style 模拟数据标 measured（G3）、维护表 RSS 22 倍差距无解释（G4）、benchmark edge type 硬编码进内核打分（G5）；
3. **研究升级空间**：feedback 统计已经存在但没有闭环到预算选择——把"一个新旋钮"升级为"自适应物理设计"是这篇文章从"solid"到"exciting"的最大杠杆（G5 对策）。

下面按风险排序展开。每条注明：现象 → 审稿人视角 → 对策 → 工作量。

---

## G1【最高风险】SF100 延迟结论没有机制支撑，且是单次测量

**现象**（`baseline/sf100-results-s5000.tsv`）：

| variant | read MiB | candidate L0 | body_reads | avg us |
|---|---:|---:|---:|---:|
| schema | 149.7 | 5,929,197 | 532,227 | 4906.2 |
| edge-type-only | 146.7 | 5,899,015 | 532,193 | 4288.8 |
| budg-b64 | 146.7 | 6,022,519 | 532,193 | **3658.3** |
| budg-b256 | 146.7 | 6,486,337 | 532,193 | 3681.1 |

budg-b64 与 schema 的 **read bytes、body_reads 几乎完全相同，candidate 还略多**，但 avg latency 低 25%。schema 比 edge-type-only 候选更多、还更慢（4906 vs 4289），而二者唯一差别是 label 分区——同样无 IO 层解释。换句话说：**延迟列的排序与所有 IO 计数器都不相关**。

最可能的解释：bench 是 REPEATS=1、变体顺序执行、无页缓存控制（store 131GB < RAM 503GB，全部读基本是 page cache 命中），延迟差异主要来自缓存状态/机器状态噪声。

**审稿人视角**：摘要里"budg-b64 平均延迟降低 32.0%"是论文对 budgeted 的核心定量主张。任何一个认真复算的审稿人会先问"为什么 IO 相同延迟差 25%"，答不上来则整个延迟列作废，连带 RQ2/RQ3 的结论。

**对策**：
1. bench 协议改造（本会话开工，代码项 C9）：`--repeats N` + warm-up 轮 + 每轮独立报告 + 可选 drop-caches 冷启动轮；JSON 里记录缓存状态与轮间方差。
2. 用户 correctness 流水线结束后重测 SF100 矩阵：每变体 import 一次（~1.1h）+ 多轮 bench（每轮只有 ~4 分钟，重复很便宜），报告 mean±sd 和 p99。
3. **写作预案**：若复测后 b64 ≈ schema ≈ edge-type-only（很可能），叙事改为——budgeted 的价值不是"更快"，而是"**以可控 fanout 拿到接近 full-semantic 的剪枝面、避开 fanout cliff（6615 文件、cache 抖动）**"，这个结论数据已经支撑，且依然成立。延迟主张退到"不劣于 schema"。

**工作量**：协议代码 1 天；重测一晚；写作调整半天。

## G2【根因级机会】每次 CSR 探测 ~391μs 的固定开销掩盖了全部收益

**现象**（budg-b64-bench.json，edge_type=1）：`csr_get_neighbors_avg_us = 391`，而对应 body 读平均只有 ~180 B/次（body_bytes 14.7MB / 81,477 次）。也就是说一次段探测里 IO 占比可忽略，**绝大部分是常数开销**。这正是"read bytes 降 99.4% 但 avg latency 只降 9~32%"的根源：所有变体的延迟都被同一个大常数地板托着，语义剪枝省下的探测数体现不出来。

**嫌疑点**（按可能性）：
- 每次查询新建 `CsrReader`（graph.rs:2043-2048，含 PathBuf clone）；
- async/tokio 每探测的调度开销（读路径每个候选段一次 await）；
- 探测路径上的分配（`L0PartitionProbe`、HashSet `touched_partitions`、metrics 锁）；
- `find_offset_in_offsets` 之前的 `metadata_for` Arc/锁路径。

**为什么这是机会而不只是问题**：把这个常数打下来 10 倍，candidate 数的差异（naive 1095/查询 vs schema 132/查询 vs kv ~1/查询）会**直接映射成延迟差异**，论文的延迟图会从"勉强 9-32%"变成"数倍"，这比补任何新实验对评分的影响都大。

**对策**：`perf record` 跑一次 SF30 bench（debug 符号或 release+debuginfo；注意要等用户流水线结束后才能动 release），定位 top 火焰；优先消减分配与每查询构造；评估批量探测（同段聚合）与 `src/io/` 现成的 direct_io/uring 后端。

**工作量**：剖析半天；第一轮优化 1-2 天；重测一晚。7 月做。

## G3 kv-style 是 Python 模拟却标 "measured"——必须先修标注，再真跑

**现象**：`baseline/codex_qslsm_sf100_strong_baseline.sh:180` 注释明写 "kv-style appendix **simulation** (derived from the schema bench json)"，由 `baseline/kv_style_baseline.py` 从 schema 的 bench json 推导生成 `kv-style-bench.json`。所以 summary 里 kv-style 的 avg_us 与 schema **精确相同**（4906.2），且无 import/store 数据。但 `baseline/sf100-strong-baseline-traces/summary-sf100.tsv` 与 summary md 把它标为 "measured"。

**审稿人视角**：这是数据完整性问题。一旦进论文再被发现（artifact evaluation 必发现），伤害远大于这条基线本身的价值。

同时 kv-style 又是**审稿人必问的对照**："把 edge type 编进 key 前缀 + prefix bloom（RocksDB column family / Nebula/JanusGraph 的工业做法）不就解决了吗？"——好消息是引擎里已有**真实实现**：`L0LayoutPolicy::RocksDbStyle`（config.rs:75 接受 `kv-lsm` 等别名，graph.rs:1228 分发，graph.rs:1501 builder），只是 harness 没用它。

模拟数据预示的真实故事其实对本文有利：KV 编码把 candidate 压到 ~1/查询，但付出**按边粒度**的 4.8M 次 entry 读（vs CSR 段扫描 532k 次 body 读）——"CSR 邻接局部性 + 语义剪枝兼得"正是 SemL0 与 KV 编码的本质差异点。这一段值得写进正文，但前提是数字是真跑出来的。

**对策**：① 立即修标注（本会话，C6）：summarize 脚本将 kv-style 标 `simulated`，修正版 summary 出在本目录；② 用户流水线结束后，用引擎真实 RocksDbStyle 跑一轮 import+bench（一晚内完成）。

## G4 维护代价表的 RSS 列是潜在一票否决项（degree_directory 全量驻留内存）

**现象**（草稿 RQ5 表，SF30）：schema 变体 import max RSS **54.8 GB** vs naive **2.4 GB**（22 倍）；SF100 时该结构重建需 **~260 GB**（见 memory：`rebuild_semantic_indexes` 的 degree_directory）。

**根源**（代码已核实）：`degree_directory: HashMap<(VertexId, EdgeType), Vec<DegreeClass>>` 按**源点 × edge type** 全量驻留内存（graph.rs:2666-2696 重建路径；graph.rs:2708-2745 增量路径，import 中每次 flush 后回读新段 offsets 累积）。而且 **schema/budgeted 这类 degree=Mixed 的文件也全量入目录**——条件只排除 `edge_type_partition == MIXED_EDGE_TYPE`（graph.rs:2671、2719），不要求 `degree_class_exact`。Mixed-degree 文件的目录条目只会产生 `{Mixed}` 类，对路由是无信息的（保守等价于查不到条目），这部分内存是白付的。

**审稿人视角**："你用一个 O(V×T) 的内存索引换读收益"——如果 RSS 列原样进论文且无解释，足以单杀。而这一列恰恰是 P2 维护代价表计划要报告的字段。

**对策**（C4，7 月）：
1. 只为 `degree_class_exact` 的文件建目录（需先用测试确认"无条目"与"{Mixed} 条目"的路由等价性，见 graph.rs:103-130 的 override 分支）→ schema/budgeted/edge-type-only 的 RSS 应直接掉回 naive 量级；full semantic 仍付费，但那本来就是它的成本故事，反而强化 budgeted 叙事；
2. 进一步：目录持久化为 sidecar（随 flush/compaction 维护可合并的有序 run），紧凑表示（packed bitmask 取代 `Vec<DegreeClass>`+HashMap，~10B/条 vs 当前 ~80B+/条）；这同时解决 C11（每次 open O(edges) 重建、SF30 ~14 分钟）。
3. 论文里把 degree directory 的内存成本作为 budgeted 的一部分**如实报告**（budget 小 → 精确 degree 分区少 → 目录小，又一条 budget 控制成本的证据线）。

## G5 基准过拟合：LDBC 的 9 个 edge type 硬编码在内核打分里 → 改成 feedback 闭环（最大研究升级）

**现象**：`is_core_ldbc_edge_type`（graph.rs:2982-2984）把 `1|2|3|7|8|9|10|11|12` ——**恰好是 benchmark 跑的那 9 个 edge type**——硬编码为高权重；budgeted 打分 `score = query_weight × (group_bytes/1024) / estimated_exact_files`（graph.rs:2826）里的 `query_weight` 即由它决定（graph.rs:2962-2979）。

**审稿人视角**：budgeted 的"benefit score"本质上预知了测试负载。artifact evaluation 打开源码即见。这会把 RQ3 的 budget sweep 全部染色。

**对策（同时是研究升级）**：内核里已有按 L0 分区记录的运行时统计——`record_l0_partition_query` / `record_l0_partition_probe`（metrics.rs:236-241），记录 query count、candidate、filter-passed、cache miss；feedback compaction 已经在消费它们（graph.rs:2352-2410，`score = query_count × avg_candidate × (1+miss_rate) / rewrite_MiB`）。**把 edge-type 的 query_weight 也从这套统计导出**（如滑动窗口内该 edge type 的查询份额），配置权重仅作冷启动默认值：

- 机制上：flush 时用观测负载决定哪些 edge type 值得精确 degree 物化，预算成为自适应分配；
- 叙事上：feedback 不再只是 compaction 的附件，而是统一的**反馈回路**（观测 → 预算分配 → 布局 → compaction），论文贡献从"budgeted 旋钮"升级为"self-tuning semantic materialization"；
- 实验上：workload-shift 场景下展示预算自动迁移（与 P6 sustained run 合并设计：phase A 热 edge type X → phase B 热 Y，看精确分区跟着走）。

**工作量**：机制 2-3 天 + microbench 1 天。8 月主项。

## G6 缺 steady-state 混合读写实验——这是 LSM 动机本身

所有现有实验都是 import 完成后的只读 bench。但 L0 read-amp 之所以是问题，恰恰是"**持续写入时 L0 不断 churn**"的场景；只读快照下 full-compact 永远是读最优解，审稿人会问"为什么不直接全部 compact 掉？"

**对策**：新增稳态实验模式——后台持续 ingest（控制速率）+ 前台查询，输出时间序列：query latency / candidate L0 / L0 files / write stall proxy。对照 schema vs budg-b64 vs full-compact（full-compact 在此场景必须持续做 L0→L1，写放大与停顿会显形——这是它在静态表里看不到的真实代价）。与 P6 sustained feedback run 共用 harness（同一个长跑框架，一个开 feedback 一个不开）。

**工作量**：harness 1-2 天，SF30 跑 30-60 分钟/变体。8 月。

## G7 签名六维只评测了 ~1.5 维

`GraphAccessSignature`（semantic.rs:73-83）声称 src_label / edge_type / direction / degree / dst_label / ts / property predicate 多维，但全部实验只用 `neighbor_scan(src, edge_type)`（semantic.rs:86-98，direction 恒 Out）。property predicate 的机制是存在的（`retain_edges_for_property_predicate`，`with_required_property` semantic.rs:110-118，property summary 剪枝有单测）但**零性能数据**；direction/ts/dst_label 完全未用。

**审稿人视角**："贡献 1 列了六个维度，评测只有一个半"——要么被要求补，要么被要求删。

**对策**（二选一，建议前者）：
1. 补一个 property-presence workload（SF30 即可）：查询带 `RequiredPresent` 谓词，对照"有 property summary 剪枝 vs 无"；再加一个删边/schema-epoch 对抗 microbench（删除重负载下展示保守回退的代价上界，顺便把 §7 正确性故事从纯单测升级为有性能数字的声明）。
2. 或在写作上明确收窄：贡献 1 改为"以 edge type 为主、label/degree 为辅的签名维度"，其余维度标 future work。

**工作量**：路线 1 约 2 天（机制都在，只缺 workload 与表）。8 月。

## G8 正确性证据链的两个锚点问题

1. **compare 锚点**：P1 计划的所有 SF100 compare 都以 schema 为基准——schema 自身也是语义布局，若其剪枝有共因 bug，全对照仍会显示 mismatch=0。需至少一组 `X vs naive`（naive 无任何语义剪枝，是真正的 ground truth 锚）。SF100 上 naive compare 较重可用 sampled（与 storage-bench 同一 sample plan）。
   *注：用户的 correctness 流水线正在跑（budg-b256 进行中，samples=100/edge type）；只需在其矩阵里补一条 vs naive 即可。*
2. **oracle 上界**：代码里现成的 `OracleSemantic` policy（graph.rs:2056 读路径分发）可以给出**剪枝上界基线**——"SemL0 的 exact-proof 剪枝达到 oracle 的 X%"是顶会喜欢的论证（它把"我们还能更好吗"变成一个量化答案）。但 `OracleL0Index::build_from_edges` 当前有**运算符优先级 bug**（graph.rs:179-184：`range && type && label_unknown || label_match`，Rust 中 `&&` 先结合，实际语义是 `(A&&B)||C`，导致只要 label 匹配就入索引，oracle 过度包含）+ O(E×F) 双重循环与 `Vec::contains` 二次方复杂度（graph.rs:191-198），用前必须修（本会话 C5）。

另外两条与正确性声明相关的代码事实（详见代码清单 C1/C2）：预算计数在重启后口径不一致；索引重建静默吞 IO 错误后 degree 路由可能漏读——后者直接违反论文 §2.2 的安全契约，必须修并配测试，否则"exact-proof 不漏读"的声明在故障路径上不成立。

## G9 写作与定位（不跑实验也该改的）

1. **novelty 必须对三类已有工作画清界限**，否则会被"这不就是 X"打掉：
   - *KV/列族工业实践*（RocksDB column family per edge type、key 前缀 + prefix bloom）：靠真实 kv-style 基线 + "CSR 局部性与语义剪枝兼得"论证（G3）；
   - *filter/budget 自适应分配*（Monkey/Dostoevsky 一系把内存预算按 level 分配给 bloom filter）：SemL0 分配的是**物化粒度预算**（exact 语义分区的文件数），对象与机制不同，但必须引用并明确对比——这反而是好定位（"Monkey 之于 filter ≈ SemL0 之于 semantic partition"）；
   - *动态图系统*（LSMGraph[VLDB'24]、LiveGraph、Teseo、Sortledton、GraphOne、Spruce）：相关工作需一张 feature matrix（更新模型 / L0 组织 / 语义元数据 / 自适应维护），明确本文管的是"LSM delta/L0 层的查询语义物理设计"这一与它们正交的层。
2. **加一条 soundness 形式化声明**：用 §4.2 的三态元数据表写成一个小引理——"在 snapshot visibility + schema epoch + tombstone 语义下，裁剪仅发生于 metadata 可证不相交/不存在；mixed/unknown/legacy 一律保留"，加半页 case-analysis 证明梗概 + 现有 10 类单测作为证据矩阵。成本半天，顶会评审对这种"小而严谨"的安全论证非常买账。
3. **叙事主线重排**：当前摘要把九布局消融当主菜。建议主线改为"**自适应、预算化的语义物化**（budgeted + feedback 闭环）"，消融降级为支撑证据；SF100 的 full-semantic 退化（6615 文件、cache 抖动）顺势成为"为什么必须 budgeted"的动机实验而不是尴尬注脚。
4. **指标口径统一**：candidate L0 惩罚细分区（full semantic 候选更多但单段更小），read bytes 又受 over-read 污染——建议主指标改为"**candidate bytes**（候选段总字节）+ 漏斗各层段数 + 延迟(mean±sd, p99)"三件套，全文统一。
5. p50/p90/p99 **已经在现有 bench JSON 里**（per edge type，`get_neighbors_p99_us` 等字段），零成本可出表——但注意直方图桶很粗（metrics.rs:12 共 16 桶，p99 会落在 100ms/250ms 这种整桶边界），终稿数字应在 C9 细桶后重测，当前表只用于内部判断尾延迟形状。

---

## 二、代码改进清单（file:line）

### P0 — 正确性/数据完整性（小改动，本会话做）

| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| C1 | 预算重启虚耗：reopen 把所有非 MIXED 文件计为已用预算（包括未选中、degree=Mixed 的 schema-style 文件），而 flush 时只对 selected 计费 → 重启后预算永久耗尽，budgeted 退化为 schema | graph.rs:2940-2955 vs 1584-1589/2913-2921 | reopen 只计 `degree_class_exact && edge_type != MIXED` 的文件，与 flush 口径一致；后续（P1）compaction 移除 L0 文件时递减 |
| C2 | `read_offsets().unwrap_or_default()` 静默吞 IO 错误 → degree 目录不完整 → degree 路由可能漏段 = **违反 exact-proof 契约** | graph.rs:2680、2728 | 传播错误（`?`），open/flush 失败显式报错 |
| C3 | `CsrMetadataCache` 的 LRU deque 每次 get/insert push 不去重 → 无界增长 + pop_front 可能驱逐刚访问过的热条目 | csr/cache.rs:51-76 | 改为带代数/去重的 LRU（或 entry 计数 + 惰性清理），加容量与正确驱逐单测 |
| C5 | Oracle 索引优先级 bug（`(A&&B)||C`）+ O(E×F) 与 `contains` 二次方 | graph.rs:174-198 | 加括号修语义；按 file 分组用 HashSet；配 oracle vs 暴力扫描等价单测 |
| C6 | kv-style 模拟数据标 "measured" | baseline/summarize_strong_baseline.py；summary-sf100.tsv | source 列改 `simulated(model)`；修正版 summary 输出到本目录，不覆盖旧 trace |
| C8 | metadata cache 容量 4096 硬编码（full semantic 6615 文件必抖动） | graph.rs:577 | 配置化（`--csr-metadata-cache-entries`），默认仍 4096；为 C7 字节预算留接口 |

### P0 — 性能证据（中等，决定论文数字）

| # | 改动 | 位置 |
|---|---|---|
| C7 | **precise offset read**：cache miss 不再整读 offset 数组（read_metadata_from_disk 现状 reader.rs:499-513），用现成 `find_offset`（reader.rs:645-663）做 pread 二分（O(log n)×24B）；SourceBloom 持久化到段 footer（现在每次 miss 从全量 offsets 重建，CachedCsrMetadata::new cache.rs:17-24，CPU+IO 双付）→ 消除 read_bytes caveat，semantic/b1024 重跑后主表干净 | csr/reader.rs、csr/writer.rs、csr/format.rs |
| C9 | bench 协议：storage-bench 加 `--repeats/--warmup`，每轮独立输出 + 方差；细化延迟桶或 bench 模式记录原始样本（45k 样本可全存）；JSON 记录缓存状态 | bin/lsmgraph.rs、metrics.rs:12、harness |
| C10 | perf 剖析 ~391μs/CSR 探测常数（每查询新建 CsrReader graph.rs:2043、async 每段开销、分配、metrics 锁）；评估 src/io/ 现成 direct_io/uring 后端 | graph.rs:2040-2160 |

### P1 — 可扩展性 + 研究升级（7-8 月）

| # | 改动 | 位置 |
|---|---|---|
| C4 | degree_directory 只收 `degree_class_exact` 文件（先证"无条目≡{Mixed}条目"路由等价）；持久化 sidecar + packed 表示 | graph.rs:2658-2745、103-130 |
| C11 | open 时 O(edges) 重建 → 随 C4 持久化解决（SF30 open ~14min → 秒级） | graph.rs:2658 |
| C12 | feedback→budget 闭环：edge-type query_weight 从 L0 分区统计导出，替换 `is_core_ldbc_edge_type`（冷启动用配置默认）；workload-shift 下预算迁移 microbench | graph.rs:2962-2984 + metrics.rs:236 |
| C13 | `read_offsets` 整 Vec clone（索引重建路径每文件一次全量拷贝） | reader.rs:423-427 → 返回 Arc |

### P2 — 卫生（投稿/artifact 前）

- Legacy 死代码清理（`待做.md`：LegacySnbGraph、snb-cache、3 个无人调用的 legacy 验证函数）；
- `merge_l0_flush_segments_to_cap` 超限静默塌缩为 fully_mixed（graph.rs:3079-3099）→ 至少记 metrics/日志，否则预算实验里观察不到；
- bench/汇总脚本里 "measured/simulated/reused" 的 source 字段全链路保留。

---

## 三、与既有补跑清单（P0-P7）的合并视图

| 既有项 | 状态 | 本分析的增量 |
|---|---|---|
| P0 naive@s5000 | 已完成 | 无变化；待 commit |
| P1 SF100 correctness compare | **用户正在跑**（budg-b256 进行中） | 矩阵里补一条 vs naive 锚点（G8.1） |
| P2 维护代价表 | 待做 | **先修 C4，否则 RSS 列自爆**（G4）；manifest/flush/rewrite 字段照旧 |
| P3 reader over-read | 原计划"注脚或修" | **明确走修复路线**（C7），理由：它污染的是主表头号指标，注脚挡不住复算 |
| P4 LiveGraph | 待做 | 不变（7 月）；写作时按 G9.1 的 feature matrix 定位 |
| P5 端到端 LDBC | 待做 | 优先级在 G1/G6 之后；若时间不够，按原计划只跑 typed-neighbor-heavy 子集 |
| P6 sustained feedback | 待做 | 与 G6 稳态混合读写、G5/C12 预算迁移**合并成同一个长跑 harness**，一鱼三吃 |
| P7 full-compact@SF100 | 边界处理 | 不变；在 G6 稳态实验里它会以"写代价显形"的方式发挥更大作用 |

新增（本分析独有）：G1 延迟复测协议、G2 常数开销剖析、G3 真实 kv-style、G5/C12 feedback→budget 闭环、G7 签名维度覆盖、G8.2 oracle 上界、G9 写作五条、C1/C2/C3/C5 四个代码 bug。

## 四、SIGMOD 2027 时间线（假定 2026-10 轮，**CFP 待核实**）

| 月份 | 实验/代码 | 备注 |
|---|---|---|
| 6 月 | 本报告；零成本补表（p99/漏斗）；C1-C3/C5/C6/C8 修复；C9 协议改造；用户流水线结束后：SF100 矩阵重测（含真实 kv-style、vs-naive 锚点） | 重测前禁动 release 二进制 |
| 7 月 | C10 perf 优化+重测；C4/C11 degree 目录修复+RSS 重测；C7 precise read+semantic/b1024 重跑；oracle 入表；LiveGraph SF10→SF100 | G2 是杠杆项，优先 |
| 8 月 | C12 feedback→budget 闭环；G6+P6 稳态长跑 harness（含预算迁移）；G7 property/对抗 microbench；full-compact streaming（资源允许） | |
| 9 月 | 数字冻结、全表从 JSON 重新生成、按 G9 重排叙事、soundness 引理、内审、投稿 | |

## 四点五、补表后的三个新发现（2026-06-12，零成本提取自现有 JSON）

提取脚本 `extract_funnel_p99.py` 全部通过与 `sf100-results-s5000.tsv` 的交叉核对。两张表：`funnel-sf100.{tsv,md}`、`latency-percentiles-sf100.{tsv,md}`。

**1）漏斗分解：SourceBloom 承担语义索引之后 ~91% 的过滤量。**
naive：49.26M 候选 → bloom 滤掉 46.07M（93.5%）→ 3.19M filter_passed → 3.12M body 读。schema：5.93M → bloom 滤 5.38M（90.7%）→ 548k → 532k。range 过滤层全程为 0（语义索引查找已带 min/max src 检查，graph.rs:144-147）。
→ 论文必须主动给这个归因（正是 `需要补的论文实验.md` 预言的审稿人问题）：**语义索引贡献 8.3× 候选缩减（49.26M→5.93M），bloom 在其后再砍 ~10×；但 bloom 无法替代语义索引**——naive 即使有 bloom 仍付 5.9× body 读（3.12M vs 532k）+ 49M 次 bloom 检查的 CPU + 26GB offset 数组加载。

**2）read_bytes 成分分解：naive 27.3GB 里 26.3GB 是 offset 数组，body 只有 1.0GB。**
semantic 10.48GB 里 10.33GB 是 offset 数组（body 153.8MB，与 schema/budgeted 完全相同）。
→ 两个推论：① C7（precise offset read）修复后，read_bytes headline 会从"178×"缩到 **body 口径的 ~6.5×（1.00GB vs 153.8MB）**——所以 G9.4 的指标口径切换（candidate bytes/漏斗段数为主）**必须先于** C7 落地，否则修完 bug 论文数字"变差"；② semantic/b1024 的 over-read caveat 被精确量化了，注脚可以写成确切数字。

**3）分位数故事远强于 avg：p50 上 budgeted 比 naive 快 6.2×，重尾把 avg 差距压扁了。**
p50（插值）：budg-b64 245μs < b256 268 ≈ b1024 269 < semantic 365 < edge-type 414 < schema 447 ≪ naive 1530。p99：全员 43-77ms 重尾，**schema 最差（77.5ms，比 naive 42.7ms 还差）**。
→ ① "avg 只降 9-32%"的真相：中位数收益 3-6×，被 50-100ms 级尾部事件（疑似 offset 数组冷加载，单次 MB 级）平均掉了；② b64 vs schema 的 avg 差异主要来自尾部事件频率，这与 G1 的"IO 计数器解释不了"一致——repeats + C7 之后尾部应大幅收敛，届时延迟故事应以 **p50/p99 双列**呈现而非 avg；③ 桶仍粗（p99 落在 50ms/100ms 桶界），终稿需 C9 细桶。

## 五、本次会话产出索引

- 本报告：`seml0-top-conf-gap-analysis-cn.md`
- 机器监控：`machine_monitor.sh` + `machine-monitor.log`（90s 采样，保护用户在跑的 correctness 流水线；只读，不 kill）
- p99/漏斗补表：`extract_funnel_p99.py` 及其输出（见同目录 TSV/MD）
- 修正版 summary（kv-style 标 simulated）：同目录
- 代码修复：C1/C2/C3/C5/C8 + C9（见 git diff；debug 单测验证，未碰 target/release）
