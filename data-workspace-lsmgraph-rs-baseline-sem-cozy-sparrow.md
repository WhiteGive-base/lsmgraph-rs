# SemL0 顶会差距分析 + 代码改进计划（目标：SIGMOD 2027）

## Context

用户要求全面分析 `baseline/seml0-linux-main-paper-draft-cn-20260611.md`：在草稿已列 TODO（naive 基线、correctness compare、维护代价、LiveGraph、read_bytes 注脚、端到端 LDBC、sustained feedback、full-compact）**之外**，还有哪些发顶会的关键缺口，以及代码要怎么改。目标会议定为 **SIGMOD 2027**（假定投 2026 年 10 月轮，需核实 CFP），批准后执行范围 = **分析报告 + 开始最高优先级修复**。

本计划基于对论文草稿、`baseline/seml0-linux-rerun-plan-20260611-cn.md`、SF100 原始数据（`remote-logs/qslsm-sf100-strong-baseline-20260610/`、`baseline/sf100-results-s5000.tsv`）和内核代码（graph.rs / csr/reader.rs / csr/cache.rs / metrics.rs / semantic.rs / harness）的逐行核查。

## 会话产出目录 + 资源共存约束（用户追加要求）

**产出目录**：本次所有文档、脚本、生成表格、监控日志统一放入新建目录
`baseline/seml0-gap-analysis-20260612/`（报告、extract 脚本及其输出表、修正版 summary 副本、monitor 日志）。不覆盖 `baseline/sf100-strong-baseline-traces/` 下任何既有结果文件；src/ 代码修复除外（源码本来就在原位）。

**机器上正在跑的、绝不能影响的进程**（已用 ps 确认，2026-06-12 01:07）：
- `run_sf100_correctness_csr_variant_20260611.sh`（VARIANT=budg-b256）→ PID 2744880/2744898/2744899，`target/release/lsmgraph import`，RSS 已 164GB，属于用户自己在跑的 P1 correctness 流水线；**该脚本后续还会再次调用 `target/release/lsmgraph`**。
- `/data/WorkSpace/dgs/.../log_graph-*` 两个长期测试进程（别的项目）、neo4j 容器、cursor-server——一律不碰。

**由此得出的硬规则**：
1. **本会话禁止 `cargo build --release`**、禁止任何写 `target/release/` 的操作（运行中的流水线会再次 exec 该二进制，换了二进制等于污染用户实验）。`cargo test` 走 `target/debug`，安全；但限 `-j 16` + `nice -n 10`，给用户进程让 CPU。
2. **禁止打开任何 SF100 store**（engine open 会建 ~260GB degree 目录，与用户 import 的 164GB+ 抢内存有 OOM 风险）；SF100 矩阵重测、真实 kv-style import 全部推迟到用户流水线结束且确认后，另起 store 目录并沿用其 MIN_AVAILABLE_GIB/MIN_FREE_GIB 闸门。
3. **磁盘紧张**（/data 仅剩 275GB，用户脚本自带 MIN_FREE_GIB=170 闸门）：本会话只产生 KB~MB 级文本 + debug 构建增量；任何步骤前若 df 可用 < 200GB 则停下来报告。
4. **实时监控**：会话开始即起一个后台监控循环（每 90s 追加一行到 `baseline/seml0-gap-analysis-20260612/machine-monitor.log`：loadavg、MemAvailable、df /data、用户测试 PID 存活状态及 RSS）；每个重步骤（cargo test、脚本运行）前后人工查一次；若 MemAvailable < 80GB 或用户进程消失（说明进入下一阶段/出错）则暂停我方重操作并在回复中报告。监控只读，不 kill 任何东西；会话结束只清理我自己启动的监控进程。

---

## 一、核心诊断：现有 TODO 之外的 8 个顶会级缺口（按风险排序）

### G1【最高风险】SF100 延迟结论目前没有机制支撑，且是单次测量
- budg-b64 与 schema 的 body_reads（均 532,193）、read_bytes（146.7 vs 149.7 MiB）完全同量级，candidate 还略多（6.02M vs 5.93M），但 avg latency 低 25%（3658 vs 4906μs）。**IO 计数器解释不了这个差异** → 大概率是页缓存状态/单次运行噪声。
- bench 为 REPEATS=1、变体顺序执行、无缓存控制；而"budgeted 延迟最低、-32% vs naive"是论文核心卖点。审稿人复算后整个延迟列会被质疑。
- 对策：bench 协议改造（warm-up + ≥3 重复 + 可选 drop-caches 冷/热两组 + 方差），重测整个 SF100 矩阵。**若复测后 b64≈schema，论文叙事需重构**（budgeted 的价值改讲"以可控 fanout 接近 full-semantic 剪枝面、避开 cliff"）。

### G2【根因级机会】~391μs/次的 CSR 调用固定开销掩盖了全部收益
- "read bytes 降 99.4% 但延迟只降 9~32%"的根源：budg-b64 的 `csr_get_neighbors_avg_us=391`，而 body 读仅 ~180B/次——每次段探测有巨大常数开销（怀疑：每查询新建 CsrReader graph.rs:2043、async 每探测开销、分配）。
- perf 剖析 + 消减该常数后，语义剪枝的延迟收益会**按比例放大**（candidate 数差异将直接映射到延迟）——这是把"9% 延迟收益"变成"数倍延迟收益"的杠杆，比加任何新实验都值。

### G3 kv-style 是 Python 模拟却标注 "measured"，而引擎里有真实实现没用
- `codex_qslsm_sf100_strong_baseline.sh:180`（注释"derived from the schema bench json"）→ `kv-style-bench.json`，avg_us 与 schema 精确相同（4906.2）即为此故。该行已进 `baseline/sf100-strong-baseline-traces/summary-sf100.tsv` 并标 measured——**数据完整性问题，必须先修标注**。
- 引擎已有真实 `L0LayoutPolicy::RocksDbStyle`（graph.rs:1228, config.rs:75）。kv-style 是审稿人必问的"edge type 放 key 前缀 + prefix bloom 不就行了？"的对照，必须真跑。真实故事预计有利：KV 把 candidate 压到 ~1/查询但付出按边粒度的 4.8M 次 body 读（vs CSR 段扫描 532k 次）——CSR 邻接局部性 + 语义剪枝兼得是本文差异点。

### G4 维护代价表里的 RSS 列是潜在一票否决项
- SF30 schema 变体 import RSS 54.8GB vs naive 2.4GB（22 倍），SF100 重建 ~260GB——根源是 `degree_directory: HashMap<(VertexId, EdgeType), Vec<DegreeClass>>` 按源点全量驻留内存（graph.rs:2666-2696），且 schema/budgeted 这类 degree=Mixed 的文件也全量入目录（白付内存）。
- 审稿人看到这列会说"你用一个 O(V×T) 内存索引换的读收益"。修复（见 C4）后 schema/budgeted 的 RSS 应降到 naive 量级，这一列才能见人。

### G5 基准过拟合：LDBC 的 9 个 edge type 硬编码在内核打分里
- `is_core_ldbc_edge_type`（graph.rs:2982）直接给 benchmark 所用 edge type 加权重 → budgeted 的 query_weight 是"作弊"。artifact evaluation 一查即穿。
- 对策（也是最大的研究升级点）：feedback 统计（`record_l0_partition_query/probe`，metrics.rs:236）已按分区记录查询数/候选数/缓存 miss——**把 edge-type 权重改为从运行时统计导出，预算选择闭环成 self-tuning materialization**。贡献从"一个新旋钮"升级为"自适应物理设计"，与 feedback compaction 合并成一个统一的反馈回路故事。

### G6 缺 steady-state 混合读写实验——这是 LSM 动机本身
- 全部实验都是 import 完成后的只读 bench。但 L0 read-amp 之所以重要恰恰是"持续写入时 L0 不断churn"。需要一个前台查询 + 后台持续 ingest 的稳态实验（latency/candidate 随时间曲线，schema vs b64 vs full-compact）；full-compact 在此处才能体现"写停顿换读"的真实代价。可与 P6 sustained feedback 合并设计。

### G7 签名六维只评测了 ~1.5 维
- `GraphAccessSignature` 声称 label/edge_type/direction/degree/property/ts 六维，实测只有 edge_type（+隐含 label）。property predicate 机制存在（`retain_edges_for_property_predicate`、`with_required_property`）但零数据；direction/ts/dst_label 完全未用。
- 对策：补 property-presence 工作负载（SF30 级即可）+ 删边/schema-epoch 对抗性 microbench（展示保守回退的代价上界）；或在写作中明确收窄声明。二选一，不能现状裸奔。

### G8 正确性证据链有两个锚点问题
- P1 计划全部 compare 以 schema 为基准——schema 自身也是语义布局，需至少一组 vs naive 的锚定。
- 代码里现成的 `OracleSemantic` policy（graph.rs:2056）可做**剪枝上界基线**（"达到 oracle 的 X%"是顶会喜欢的论证），但 `OracleL0Index::build_from_edges` 有运算符优先级 bug（graph.rs:179-184，`&&`/`||` 无括号 → 条件实际为 `(A&&B)||C`）+ O(E×F) 复杂度，用前必须修。

另：p99/p50 与剪枝漏斗（candidate→bloom_filtered→range_filtered→filter_passed→matched→body_reads）**已在现有 bench JSON 中**，零成本可出表；直方图桶过粗（metrics.rs:12，p99 落在 250ms 整桶），终稿数字需加细桶后重测。

---

## 二、代码改进清单（file:line 级）

### P0 — 正确性/数据完整性（小改动，立即做）
| # | 问题 | 位置 | 修法 |
|---|---|---|---|
| C1 | 预算重启虚耗：reopen 把**所有**非 MIXED 文件计为已用预算，而 flush 时只对 selected 计费 → 重开后预算永久耗尽 | graph.rs:2940 vs 1584/2914 | 统一口径：只计 `degree_class_exact` 文件；compaction 移除 L0 文件时递减 |
| C2 | 索引重建静默吞 IO 错误 → degree 目录不完整 + degree 路由 = **可能漏读**（违反 exact-proof 契约） | graph.rs:2680, 2728 `read_offsets().unwrap_or_default()` | 传播错误 |
| C3 | LRU 队列每次 get/insert push 不去重 → 无界增长 + 热条目可能被误逐 | csr/cache.rs:51-76 | 去重或改 LinkedHashMap 式实现 |
| C5 | Oracle 索引优先级 bug + `contains` 二次方复杂度 | graph.rs:179-198 | 加括号修语义；重写为 HashMap 分组 |
| C6 | kv-style 模拟标 "measured" | baseline/summarize_strong_baseline.py + summary 文件 | 标注 simulated/model，主表剔除或注脚 |

### P0 — 性能证据（中等改动，决定论文数字）
| # | 改动 | 位置 |
|---|---|---|
| C7 | **precise offset read**：cache miss 时不再整读 offset 数组，用现成 `find_offset`（reader.rs:645）做 pread 二分（O(log n)×24B）；SourceBloom 持久化到段 footer（现在每次 miss 用全量 offsets 重建，CPU+IO 双倍付）→ 消除 read_bytes caveat，semantic/b1024 重跑 | csr/reader.rs:499-513, csr/writer.rs |
| C8 | metadata cache 容量 4096 硬编码 → 配置化 + 按字节预算 | graph.rs:577 |
| C9 | bench 协议：REPEATS/warm-up/可选 drop-caches/记录缓存状态；细化延迟桶或 bench 模式记录原始样本 | bin/lsmgraph.rs storage-bench、metrics.rs:12、harness 脚本 |
| C10 | perf 剖析 391μs/CSR 调用常数（每查询新建 CsrReader graph.rs:2043、async 探测开销、分配）；现成 direct_io/uring 后端（src/io/）评估接入 | graph.rs:2040-2160 |

### P1 — 可扩展性 + 研究升级
| # | 改动 | 位置 |
|---|---|---|
| C4 | degree_directory：只为 `degree_class_exact` 文件建目录（Mixed 文件语义上等价于无信息，需用测试验证等价性）→ schema/budgeted 的 RSS 从 54GB→GB 级；进一步：持久化 sidecar + 紧凑表示（packed bitmask 替代 `Vec<DegreeClass>` HashMap） | graph.rs:2658-2745 |
| C11 | `rebuild_semantic_indexes` O(edges) 每次 open 全量重建（SF30 ~14min）→ 随 C4 持久化解决 | graph.rs:2658 |
| C12 | **feedback→budget 闭环**：edge-type 权重从 L0 分区统计导出（替换 is_core_ldbc_edge_type，graph.rs:2962-2984），flush 时按观测收益升降级分区 → G5 的研究升级 | graph.rs + metrics.rs |
| C13 | `read_offsets` 整 Vec clone（reader.rs:423-427，索引重建路径） | 返回 Arc |

### P2 — 卫生（投稿前 artifact 清理）
- 清理 Legacy 死代码（见 `待做.md`：LegacySnbGraph、snb-cache、3 个 legacy 验证函数）；`merge_l0_flush_segments_to_cap` 静默塌缩为 fully_mixed（graph.rs:3079）至少加计数/日志。

---

## 三、SIGMOD 2027 时间线（假定 2026-10 轮截稿，需核实）

| 阶段 | 内容 |
|---|---|
| **6 月（本计划）** | 分析报告落档；零成本补表（p99/漏斗/per-edge-type，修 kv 标注）；P0 代码修复（C1-C3,C5-C9）；bench 协议改造完成；**等用户在跑的 correctness 流水线全部结束后**再重测 SF100 矩阵（含真实 kv-style import+bench；各变体重 import ~1.1h，import 一次多次 bench，跑通宵），并补一组 vs naive 的 correctness 锚点 |
| **7 月** | C10 perf 优化 + 重测；C4/C11 degree 目录修复 + RSS 重测；oracle 基线入表；LiveGraph SF10→SF100（既有 P4）；混合读写稳态实验设计+首跑（G6） |
| **8 月** | C12 feedback→budget 闭环 + sustained feedback run（既有 P6 合并）；property-predicate / schema-epoch 对抗 microbench（G7）；full-compact streaming@SF100（资源允许） |
| **9 月** | 数字冻结、全表从原始 JSON 重新生成、写作（叙事重排：budgeted+feedback 自适应物理设计为主线，消融为证据）、内审、投稿 |

## 四、批准后本会话立即执行（全程遵守资源共存规则）

0. **建目录 + 起监控**：`mkdir baseline/seml0-gap-analysis-20260612/`；启动后台监控循环写 `machine-monitor.log`（90s 间隔，只读采样）。
1. **分析报告**：写入 `baseline/seml0-gap-analysis-20260612/seml0-top-conf-gap-analysis-cn.md`（G1-G8 全文展开 + 代码清单 + 与既有 P0-P7 的合并视图）。
2. **零成本补表**：新脚本 `baseline/seml0-gap-analysis-20260612/extract_funnel_p99.py` 从现有 bench JSON（只读）提取（a）各变体 p50/p90/p99 表（注明桶量化误差）（b）剪枝漏斗分解表，输出 TSV/MD 到同目录；修 `baseline/summarize_strong_baseline.py` 的 kv-style 标注（源码小改），**修正版 summary 输出到新目录**，不覆盖旧文件。
3. **P0 修复第一批**（小而确定，纯源码+单测）：C1 预算计数、C2 错误传播、C3 LRU、C5 oracle bug、C8 cache 配置化；用 `nice cargo test -j 16` 验证（debug profile，不碰 release 二进制）。
4. **C9 bench 协议改造**开工（REPEATS/warm-up/缓存状态记录 + 细桶，纯代码）——但**不在本会话编译 release、不跑任何 SF100**；矩阵重测等用户的 correctness 流水线全部结束并确认后另行安排。
5. 注：既有 P1 correctness compare **用户已自行在跑**（budg-b256 进行中），从我的执行队列移除，仅在报告中引用其进展。

## 五、验证方式

- 每个 P0 修复配 Rust 单测（预算重开计数、LRU 容量上界、oracle 等价性 vs 暴力扫描、IO 错误传播）；`nice cargo test`（debug）全绿。
- 补表脚本输出与现有 `sf100-results-s5000.tsv` 交叉核对共有列。
- 全程通过 `machine-monitor.log` 复核：用户的 correctness 流水线 PID 全程存活、MemAvailable 未跌破 80GB、df /data 未跌破 200GB。
- bench 协议改造的验证（SF1/SF30 小店方差 < 5%）与 SF100 重测，**推迟到用户流水线结束后**；届时用 `baseline/run-status.sh` + summarize 重新生成 summary，与旧表对照写差异说明（按 leave-benchmark-traces 惯例留 dated trace）。
