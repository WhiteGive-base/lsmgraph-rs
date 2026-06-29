# SemL0 → SIGMOD 2027 投稿冲刺：Codex 执行交接文档

> 写于 2026-06-12。本文档自包含：每个工单（W1–W12）可在一个独立 Codex 会话内完成，
> 开工前只需读本文档 + §0 指向的仓内证据文档，不依赖任何对话历史。
> **执行顺序按 §2 的批次与依赖；每个会话先过 §1 自检。**

## 目标与截稿（已核实官网）

- 主投：**SIGMOD 2027 Round 4**——摘要/COI **2026-10-10**，全文 **2026-10-17**（AoE）；
决定 2027-01-19，revision 2027-02-19，final 2027-03-12（自带 revision 轮，borderline 可补救）。
- 兜底：**PVLDB Vol.20** 每月 1 号滚动截稿（2026-04 → 2027-03），miss R4 后 11/1、12/1 可投。
- 论文主线（已定稿，勿扩功能）：**query semantics 作为 LSM L0 段级 exact-proof pruning，
full semantic 物化会过度分段，因此需要 budgeted + feedback-driven 的语义物理设计（SemL0）**。
- 新颖性边界（已核查）：LSMGraph (VLDB'24，多级 CSR，开源 github.com/iDC-NEU/LSMGraph) 与
BACH (PVLDB'25，LSM 层间 adjacency↔CSR 布局变换，无公开代码) 都是**图布局**维度，
与我们的**查询语义剪枝/物化**正交——它们是 related work 锚点 + 外部基线候选，不构成撞车。

---

## §0 现状基线（2026-06-12 晚，先读这节，**勿重做已完成项**）

### 0.1 已完成——代码（全部 debug 测试通过，详见 `baseline/seml0-gap-analysis-20260612/implementation-status-20260612.md`，该文件是权威完成清单）


| 项        | 内容                                                                                                                                                    | 关键位置                                                                                                                     |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| C1       | 预算 reopen 计数口径统一（只计 exact 非 Mixed L0）                                                                                                                 | graph.rs `initial_semantic_budget_used_extra_l0_files()`                                                                 |
| C2       | 索引/目录重建路径传播 `read_offsets` IO 错误                                                                                                                      | graph.rs                                                                                                                 |
| C3       | CSR metadata cache LRU 队列有界 + 热条目保活                                                                                                                   | src/csr/cache.rs                                                                                                         |
| C4       | degree_directory 三阶段瘦身：仅 exact 文件入目录 + packed `DegreeClassMask`(u8) + 持久化 sidecar                                                                     | graph.rs:481（HashMap<(VertexId,EdgeType),DegreeClassMask>）、graph.rs:399-434（`DEGREE_DIRECTORY` sidecar，magic `L0DGDIR1`） |
| C5       | Oracle 索引优先级 bug 修复 + O(E×F) 路径移除                                                                                                                     | graph.rs（OracleL0Index）                                                                                                  |
| C6       | kv-style 模拟行标注 `simulated(model)`，主表剔除                                                                                                                | baseline/summarize_strong_baseline.py                                                                                    |
| C7       | **precise offset read**（cache miss 走 24B pread 二分，不再整读 offset 数组）+ **SourceBloom 持久化**到段尾/manifest                                                    | reader.rs:565-599 `find_offset_on_disk()`、writer.rs:261-265、reader.rs:561                                                |
| C8       | `--csr-metadata-cache-entries` 配置化并接入 Engine open                                                                                                     | src/bin/lsmgraph.rs、config.rs                                                                                            |
| C9       | **bench 协议**：`--warmup-runs`/`--repeats`、每轮 JSON（rounds/warmup_rounds）、repeat mean/stddev/min/max、/proc/meminfo+loadavg 缓存快照、延迟桶 16→32 细化             | lsmgraph.rs:1058-1070、1417-1426                                                                                          |
| C11      | open 时 sidecar 加载替代全量 offset 扫描重建                                                                                                                     | graph.rs `load_or_rebuild_semantic_indexes()`                                                                            |
| C12 第一阶段 | feedback→budget：`semantic_budget_feedback_edge_type_weights()` 按 (src_label,edge_type) 聚合 L0 分区统计，打分取 `max(静态权重, feedback)`，诊断标 `feedback_score_gate` | graph.rs；静态权重仍在 `is_core_ldbc_edge_type` graph.rs:3244-3246 / `semantic_budget_edge_type_weight()` graph.rs:3230         |
| C13      | 缓存 offsets 改 `Arc<[EdgeOffset]>`，消除整 Vec clone                                                                                                        | cache.rs、reader.rs `read_offsets()`                                                                                      |


**未做的代码项只有 C10**（每次 CSR 调用 ~391μs 固定开销的 perf 剖析与消减）→ W2。

### 0.2 已完成——数据与表格（证据路径）


| 产出                                                                               | 路径                                                                                                                                                                         |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SF100 read-amp 主表（修正版，kv-style 已标 simulated）                                     | `baseline/seml0-gap-analysis-20260612/summary-sf100-corrected.md`                                                                                                          |
| p50/p90/p99 表（粗桶，标注量化误差）                                                         | 同目录 `latency-percentiles-sf100.md/.tsv`                                                                                                                                    |
| 剪枝漏斗表                                                                            | 同目录 `funnel-sf100.md/.tsv`，提取脚本 `extract_funnel_p99.py`                                                                                                                    |
| naive@s5000 锚点锁定数字                                                               | read_bytes=27,318,428,688 / candidate=49,257,601 / avg=5382.8μs / import=3801.7s；JSON 在 `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-*.json`                    |
| SF100 correctness（4 变体 vs schema，0 mismatch，CSR sampled compare）                 | `remote-logs/qslsm-sf100-correctness-parallel-20260611-215300/`、`remote-logs/qslsm-sf100-correctness-csr-20260611/`（注意：旧 `SNB_SKIP_SEM_INDEX=1 neighbor-compare` 结果无效，勿引用） |
| SF100 维护代价表（6 变体，import wall/store/manifest/L0/maxRSS）                           | `remote-logs/qslsm-sf100-maintenance-table-20260612/maintenance-table.tsv`，runner `baseline/run_sf100_maintenance_table_20260612.sh`                                       |
| P6 sustained feedback（synthetic，30min，feedback 0.0 vs no-feedback 3.0 candidate） | `remote-logs/p6-sustained-feedback-20260612/`                                                                                                                              |
| LiveGraph SF10 完整实测                                                              | `remote-logs/livegraph-sf10-20260612/`                                                                                                                                     |
| LiveGraph SF100 终止存档（17–21 天外推，不可行性观察）                                           | `baseline/livegraph-sf100-attempt-20260612-cn.md`                                                                                                                          |
| LDBC SF10 server-path smoke（P5 收窄后的 server 证据）                                   | `remote-logs/e9-ldbc-sf10-smoke-20260612-abs/`                                                                                                                             |
| 全局状态文档                                                                           | `baseline/seml0-linux-rerun-status-20260612-cn.md`（P0–P7）、`baseline/seml0-gap-analysis-20260612/seml0-top-conf-gap-analysis-cn.md`（G1–G8/C1–C13 全文）                        |


### 0.3 真正未做（= 本文档工单范围）

C10 perf；SF100 全矩阵复测（新代码+新协议+多 repeat）；真实 kv-style import+bench；
correctness 的 **naive 锚**（现有 compare 全以 schema 为左侧）；oracle 剪枝上界实测行；
C12 第二阶段（feedback-only 模式 + workload-shift 验证）；property-aware 工作负载；2-hop 工作负载；
真实 store 稳态混合读写；LSMGraph 外部对比；论文 claims 安全化与表格再生；legacy 清理。

### 0.4 发表充分性判断（指导取舍）

- 一票否决项（G1 单次延迟测量、G3 kv-style 假标注、G4 RSS、G5 LDBC 硬编码、G8 锚点、read_bytes caveat）
——代码侧已修，**全部依赖 W6 复测落地成数字**。W6 是全计划的重心。
- 审稿人最可能追加的两问：外部 SOTA 基线（→W12 LSMGraph）、超出 1-hop 的查询收益（→W4 2-hop + property + SF10 server smoke）。
- 最大不可控：Gate 1（budg-b64 延迟优势是否经得起复测）与 C10 成效——两者都有 fallback 叙事（§3），不会毁稿。

### 0.5 schema-evolution：机制已实现，结论（2026-06-13 核查，决定不破冻结）

审稿人必问「新增 edge label / property 后旧存储是否失效、会不会漏边」。**核查结论：机制已建好且已测，无需新引擎代码。**
- `src/schema.rs`：完整 `SchemaCatalog`（add/drop/alias/resolve vertex-label/edge-label/property、`encoding_epoch`、
  `valid_from/to_epoch`、`is_visible_at`）——即 GPT 说要建的 Versioned Schema Catalog，已存在。
- `src/csr/format.rs:117/125`：段 footer 持久化 `schema_epoch` + `property_encoding_epoch`（编解码已测）。
- `src/graph.rs`：catalog 随 store 加载/保存；`add_edge_label/add_property/alias_*/drop_*` engine API 推进 epoch 并落盘；
  `segment_count_by_schema_epoch` 统计；flush 给段打当前 epoch。
- `tests/engine_tests.rs:1942 schema_epoch_change_keeps_old_segments_readable` + `:2020 schema_catalog_persists...`
  + `:395 schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen`：已证旧段在 schema 演进 + 重开后仍可读、查询正确。
- **关键设计点**：`GraphAccessSignature`（semantic.rs:73）不含 schema_epoch——剪枝靠 **stable edge_type id**。
  新增 label = 新 type id，旧 exact 段按 type 不相交**自动被现有 exact-disjoint 路径跳过**；schema_epoch 仅用于
  property-encoding 解释和 snapshot 可见性。这正是「旧存储不失效」想要的性质，且零额外剪枝代码。

→ 因此把 GPT 的提议落成 **W13（实验 + invariant/theorem 写作）**，放 Batch C，**不破 `engine-freeze-sigmod2027` 冻结**
（runner 属允许项；真要给 catalog 补能力是 future work，不在投稿路径上）。贡献升级为「schema/snapshot-safe semantic pruning」，
是区别于「普通语义索引（schema 变了要重建）」的差异点，几乎零成本拿下。

---

## §1 硬性规约（每个工单会话开头自检，违反即停）

1. **资源门限**：`df -h /data` 可用 < 200GiB 或 MemAvailable < 80GiB → 硬停报告。
  构建/测试一律 `nice -n 10`、`cargo ... -j 16`。`/tmp` 很小（~3.6GiB），勿写大文件。
2. **单 SF100 任务原则**：同一时间最多 1 个 SF100 级 import/compact/bench；可并发 SF1/SF10 级与纯文本工作。
3. **release 重建前检查**：`ps aux | grep -E 'lsmgraph|run_'` 确认没有任何在跑的流水线之后还会
  exec `target/release/lsmgraph`（换二进制 = 污染在跑实验）。当前（2026-06-12 晚）机器已空闲。
4. **长任务事前时间评估（新规约，用户拍板）**：预计 >30min 的任务，启动前必须
  (a) 用 SF1/SF10 小规模外推出 ETA 写进运行说明；(b) runner 内置进度信号（fd offset / write_bytes / 已完成变体数）；
   (c) 预设 abort 条件（如「30min 内推进 < X」即自动停）。LiveGraph SF100 的 17–21 天尾部就是教训。
5. **留痕惯例**：每次跑批产出 `remote-logs/<run-id>/`（含 DONE 标记）+ `baseline/` 下 dated 总结 md；
  **不覆盖任何旧结果文件**；表格一律可由脚本从原始 JSON 再生。
6. **勿重做 §0.1/§0.2 的已完成项**；改 `src/` 后必须 `nice -n 10 cargo test -j 16` 全绿再继续。
7. LDBC 输入：`/data/WorkSpace/ldbc-sf{1,10,30,100}/social_network`。现有 store：
  `store/sf{1,10,30,100}-base-graph`、旧 SF100 变体 store `store/qslsm-sf100-strong-baseline-20260610`（132G，
   旧格式无 SourceBloom 段；W6 用新引擎重新 import，**此旧 store 在 W6 完成后报请用户确认删除**）。

---

## §2 工单

### Batch A——现在就可开工（W3/W4/W5 纯代码可并行；W1 是 W6 的就绪判据）

**W1：release 安全重建 + SF1/SF30 实证冒烟（C7/C4/C11/C9 效果验证）**

- 目标：证明今天落地的代码在真实 store 上产生预期效果，给 W6 放行。
- 步骤：§1.3 检查后 `cargo build --release`；用 SF1 → SF30 各跑：
(a) 新 import 一个 schema 与一个 semantic 变体小 store；
(b) `storage-bench --warmup-runs 1 --repeats 3`，对照旧 JSON：semantic 的 read_bytes 应**大幅下降**（C7 消除整读 offset；旧值见 §0.2 主表：SF100 semantic 曾 9994 MiB vs schema 150 MiB）；
(c) Engine open 时间与 RSS（C4/C11：sidecar 命中时 open 不再全量扫描；`/usr/bin/time -v` 记 maxRSS）；
(d) repeats 方差应 <5%，bench JSON 里 rounds/缓存快照字段齐全。
- 验证/产出：`remote-logs/w1-smoke-<date>/` + `baseline/w1-smoke-<date>-cn.md` 前后对照小表；
结论行：「SF100 战役就绪 / 不就绪 + 原因」。
- ETA 参考：SF30 import 约 20–40min/变体（SF100 是 ~1.3h）。

**W2：C10 时间盒（启动起 ≤1 周，到期冻结引擎）**

- 目标：消减 ~391μs/次 CSR 调用固定开销（budg-b64 的 `csr_get_neighbors_avg_us=391` 而 body 仅 ~180B/次），
把「read_bytes 降 99% 但延迟只降 9–32%」的杠杆扳过来。
- 步骤：
  1. 先落**分阶段计时插桩**（挂到现有 metrics.rs 计数器体系；per-probe 分解：reader 构建 / bloom 判定 / offset 查找 / body 读 / 分配 / async 调度）——这份分解数据同时回答 G1「延迟差异机制」；
  2. SF30 release store 上 `perf record` + 插桩数据定位 top 开销；
  3. 高杠杆候选（按 gap-analysis）：每查询新建 CsrReader（graph.rs:2043 一带）、async 每探测开销、热路径分配；`src/io/` 已有 direct_io/uring 后端，评估接入成本；
  4. 每改一项跑 SF30 前后对照 + 全量 debug 测试。
- 时间盒纪律：**到期无论成果，打 tag（如 `engine-freeze-sigmod2027`）冻结引擎**，之后到 W6 结束只许 bug fix。
- 产出：`baseline/w2-c10-profile-<date>-cn.md`（分解表 + 已落地优化清单 + SF30 前后延迟对照）。

**W3：C12 第二阶段——feedback-only 模式 + workload-shift runner（纯代码，先行）**

- 目标：彻底回应 G5「LDBC 9 个 edge type 硬编码在打分里 = benchmark 过拟合」，
并把贡献升级为 self-tuning materialization。
- 步骤：
  1. 加配置（如 `--semantic-budget-feedback-only`）：关闭 `is_core_ldbc_edge_type`（graph.rs:3244-3246）
    与 `semantic_budget_edge_type_weight()`（graph.rs:3230）的静态权重，冷启动一律均匀权重，
     打分只用已有 `semantic_budget_feedback_edge_type_weights()`；单测：冷启动行为 + feedback 促升回归
     （已有 `budgeted_semantic_feedback_promotes_hot_non_core_edge_type` 可参照）；
  2. 写 workload-shift runner（脚本或扩展 `src/bin/p3_feedback_sustained.rs` 模式到真实 store）：
    相位 A（查询 edge type 集合 A）→ 相位 B（切到集合 B），对照 feedback-only / 静态 budgeted / no-feedback 三组，
     记录每次 flush 的预算分配 + candidate/latency 曲线；先 SF1 验证 runner，SF30 正式跑放 W7。
- 验证：debug 测试全绿；SF1 上 feedback-only 在相位切换后 N 个 flush 内把预算迁到新热点。
- 产出：代码 + 单测 + `baseline/run_w7_workload_shift_<date>.sh`（含 ETA/abort 逻辑）。

**W4：工作负载扩展——property 谓词 + 2-hop（纯代码，先行）**

- 目标：回应 G7「签名六维只测 1.5 维」与「只有 1-hop 点查」。
- 步骤：
  1. property-aware bench 模式：复用已有 `retain_edges_for_property_predicate` / `with_required_property`，
    storage-bench 增加四组谓词模式：required_property§0 / property presence / equality / absent-default；
  2. 2-hop typed expansion 模式：sampled 源点 get_neighbors 后对（截断的）邻居集再做 typed get_neighbors，
    记录两跳合计 candidate/body/latency（防爆炸：每源一跳邻居截断如 ≤64）；
  3. SF30 runner 脚本（变体 schema / budg-b64 / semantic，沿用 C9 协议）。
- 验证：SF1 冒烟两种模式 JSON 字段齐全、谓词语义正确（与暴力过滤对照的单测）。
- 产出：代码 + `baseline/run_w8_property_2hop_<date>.sh`。

**W5：G6 真实 store 稳态混合读写 runner（纯代码，先行）**

- 目标：LSM 动机本身——持续写入下 L0 churn 时的读放大对比；P6 的 synthetic 版已完成，缺真实 store 版。
- 步骤：参照 `src/bin/p3_feedback_sustained.rs` 写真实 SF30 store 版：后台持续 ingest/delete（控制速率），
前台 sampled typed 查询；30–60min；checkpoint 记 candidate L0 / latency p50/p99 / L0 files /
compaction rewrite bytes / flush-stall proxy / io_write_bytes；变体 schema / budg-b64 / semantic。
- 验证：SF1 短跑（3–5min）冒烟；ETA/abort 内置。
- 产出：bin 或脚本 + `baseline/run_w9_steady_state_<date>.sh`。

### Batch B——引擎冻结后的 SF100 一次性战役

**W6：SF100 全矩阵复测（全计划重心，一次跑完所有需要大 store 的证据）**

- 前置：W1 通过、W2 时间盒到期已冻结、§1 资源检查（当前 /data 692GiB 可用，满足）。
- 变体（9 个，串行）：`naive`（锚，**最先 import，最后删**）、`schema`、`edge-type-only`、`semantic`、
`budg-b64`、`budg-b256`、`budg-b1024`、`**kv-lsm`（真实 RocksDbStyle：`--l0-layout rocksdb-style`，
config.rs:45/76-77 已接好 CLI，graph.rs:1688-1699 flush 逻辑现成——这是第一次真跑，替换模拟行）**、
oracle（C5 已修的 OracleSemantic policy，作剪枝上界列）。
- 每变体流程（扩展 `baseline/run_sf100_maintenance_table_20260612.sh` 的「import→记录→删 store」模式）：
  1. import（`/usr/bin/time -v` 记 maxRSS/wall；同时得维护代价行：store/manifest/L0 files）——**新 RSS 即 Gate 2 数据**；
  2. `storage-bench --samples 5000 --warmup-runs 1 --repeats 3`（C9 协议，含缓存快照）；
  3. 删 store 前跑 **CSR sampled compare vs naive store**（参照 `baseline/sf100_sampled_csr_compare.py` 与
    `run_sf100_correctness_parallel_20260611.sh` 用法，s100 规模）→ 补 G8 naive 锚；
  4. 记录后删除变体 store。
- A/B/A 哨兵：战役末尾把第一个 bench 变体（建议 schema）重新 import+bench 一轮，验证无顺序/环境漂移。
- ETA（§1.4 要求）：9 变体 ×（import ~1.3h + bench 3 轮 ~15-20min + compare ~10min）≈ **15–20h，过夜跑**；
磁盘峰值 = naive store + 当前变体 store ≈ 2×132GiB + 输入，692GiB 足够；写 DONE 标记；
abort 条件：单变体 import 超 3h 或 MemAvailable < 80GiB。
- 产出：`remote-logs/qslsm-sf100-matrix-<date>/` 全原始 JSON +
用 `baseline/summarize_strong_baseline.py` / `extract_funnel_p99.py` 再生全套表：
read-amp 主表（**真实 kv-style 替换模拟行**）、维护代价表（新 RSS）、细桶 p50/90/99、漏斗、
budget sweep、oracle 达成比例（candidate 相对 oracle 的 %）、**各变体 3 轮 mean±stddev 列**；
`baseline/sf100-matrix-<date>-cn.md` 总结 + 与 20260610 旧表逐列差异说明。
- **跑完立即判 Gate 1 / Gate 2（§3）并写进总结。**

### Batch C——证据补齐（W6 后，7 月内）

- **W7**：用 W3 的 runner 跑 SF30 workload-shift 正式实验（feedback-only / 静态 / no-feedback 三组）→ 判 Gate 4。
- **W8**：用 W4 的 runner 跑 SF30 property + 2-hop 正式实验 → 判 Gate 3。
- **W9**：用 W5 的 runner 跑 SF30 稳态 30–60min 正式实验（schema / budg-b64 / semantic）。
- **W10：论文数据卫生与 claims 安全化**（可与 W7–W9 并行，纯文本）：
  - 主文所有表确认由脚本从原始 JSON 再生；
  - 按 Gate 结果改 claims：Gate 1 过 → 可讲 latency；不过 → 主讲 candidate/read-amp 下降 + 避开 full-semantic cliff，延迟写成 observation；
  - read_bytes caveat：若 W6 复测后 semantic/b1024 的 read_bytes 已正常（C7 生效）→ 删 caveat；否则降级为 secondary metric；
  - P4 LiveGraph 章节：SF10 实测 + SF100 不可行性观察（引用 `baseline/livegraph-sf100-attempt-20260612-cn.md`）；
  - P5 端到端：收窄措辞（storage-level 对照 + SF10 server-path smoke 为 server 证据；不声称 end-to-end layout 对照）；
  - 叙事主线落到：「SemL0 = L0 exact-proof pruning + budgeted materialization + feedback-driven（self-tuning）物理设计；
  budgeted 是 schema-style 与 full-semantic 之间的实用工作点」。
- **W11：legacy 清理（保守档，见 `待做.md`）**：删 3 个无人调用的 legacy 验证函数
（queries.rs:300/324/435 validate_ic1_ic14 / validate_ic_batch / validate_mixed_tugraph）+ mod.rs re-export；
`merge_l0_flush_segments_to_cap` 静默塌缩 fully_mixed（graph.rs ~3079）加计数/日志；
`snb-cache` 命令与 `LegacySnbGraph` 整套是否删除先问用户。
- **W12：LSMGraph 外部对比（时间盒 ~1 周，回应「外部 SOTA 基线」）**：
  1. clone `github.com/iDC-NEU/LSMGraph`，先做构建评估 + ETA（能否 2 天内在 SF1/SF10 跑通 load + typed-neighbor scan）；
  2. 可行 → SF10 对比：LSMGraph vs 我方 schema/budg-b64（load 时间、峰值内存、邻居扫描延迟），
    沿用 LiveGraph SF10 的 dense edge list 流程（`baseline/convert_livegraph_edges.py` 产物格式可复用）；
  3. 不可行 → 与 BACH 同款处理：related work 定性对比 + 设计差异表（多级 CSR 布局 / 布局变换 vs 语义 L0 剪枝，
    强调正交性：SemL0 可叠加在任意 LSM-CSR 布局之上）+ 内部消融方法学辩护（贡献是 L0 布局策略而非整引擎）。
- **W13：schema-evolution 安全裁剪实验 + invariant/theorem（新增，无需破冻结）**——见 §0.5 的核查结论：
  机制（`src/schema.rs` SchemaCatalog、段 footer `schema_epoch`/`property_encoding_epoch`、engine add/drop/alias API、
  `tests/engine_tests.rs:1942 schema_epoch_change_keeps_old_segments_readable`）**已实现且已测**，剪枝靠 stable edge_type id
  自动跳过旧 exact 段，**不需要新引擎代码**。本工单只做实验 + 写作：
  1. runner（SF30 级）：epoch 0 import 一批 edge label → 用 engine API `add_edge_label` + `add_property` 推进到 epoch 1 →
     epoch 1 写入新 label / 新 property 的 delta → bench 三类查询：(a) 新 label typed-neighbor、(b) 旧 label topology、
     (c) 新 property equality/absent；记录每类的 candidate_l0_segments 漏斗 + mismatches；
  2. 断言三件事：correctness（vs naive，mismatches=0，**无 false negative**）；pruning（新 label 查询对 epoch 0 exact 段
     candidate≈0，即旧段被正确跳过）；conservative（mixed/legacy 段被保守读，candidate>0 但仍 0 mismatch）；
  3. 可选 +1：lazy compaction 后旧 schema-uncertain 段升级，pruning power 恢复（与 feedback compaction 故事合并）；
  4. 写作：3 个 invariant（Segment Schema / Epoch-Aware Resolution / Conservative Pruning）+ theorem
     「schema evolution 只降剪枝精度、不引入漏读」+ schema-变更分类表（additive 支持、rename=alias/relabel 二分、
     drop=lazy、type/encoding=保守）；与 BACH file-snapshot MVCC 的对照写进 related work。
     **边界（必须显式）**：只声称 additive schema + 定宽 property equality + presence/absence + epoch/encoding-epoch + alias/tombstone/snapshot；
     不声称任意 schema migration、range/string/compound predicate、在线全库 rewrite。

---

## §3 决策门（每门两套预案，判定后写入对应总结 md）


| 门                                                            | 判定时点        | 通过 →                                      | 不过 →                                                                           |
| ------------------------------------------------------------ | ----------- | ----------------------------------------- | ------------------------------------------------------------------------------ |
| **Gate 1** budg-b64 延迟稳定优于 schema（3 轮 mean±stddev 区间不重叠）     | W6 后        | 主 claim 保留「budgeted 延迟最优」，用 W2 插桩分解解释机制   | 主 claim 改「保留 schema 级剪枝、避开 full-semantic fanout cliff，candidate/read-amp 显著下降」 |
| **Gate 2** schema/budgeted 的 import maxRSS 与 naive 同量级（C4 后） | W1 初判、W6 终判 | 维护代价表入主文作加分项                              | RSS 列移注脚 + 写成当前实现限制，主文只保 store/manifest/L0/import wall                         |
| **Gate 3** property/2-hop 工作负载出正向结果                          | W8 后        | 标题保留「property graphs」全称，签名维度覆盖声明成立        | 标题/正文收窄为「L0 design …」，property 写成机制 + microbench                               |
| **Gate 4** feedback-only 在 workload-shift 下 N 个 flush 内重定向预算 | W7 后        | 贡献写「self-tuning semantic materialization」 | feedback 降级为「controlled workload-shift mechanism」，勿称生产级自适应                     |
| **Gate 5** 稳态混合读写出 time-series（schema/budg-b64/semantic）       | W9 后        | LSM 动机坐实，read-amp 随 L0 churn 的曲线入主文       | 降级为附录/observation，主文不强调 dynamic steady-state                                  |
| **Gate 7** schema-evolution 实验：新 label 查询跳过旧 exact 段、0 mismatch、mixed 保守读 | W13 后  | 贡献加「schema/snapshot-safe semantic pruning」+ theorem，标题可含 dynamic property graph 全义 | 退为 correctness 小节 + invariant（机制已测，故几乎必过；真出问题就只写 readability 不写 pruning 收益）  |


---

## §4 时间线（截稿 2026-10-17，比原 GPT 方案提前约一月，余量即缓冲）


| 时段        | 内容                                                              |
| --------- | --------------------------------------------------------------- |
| 6/13–6/20 | W1 冒烟；W2 启动（插桩先行）；W3/W4/W5 并行落代码                                |
| 6/21–7/05 | W2 收口冻结引擎 → **W6 SF100 战役** + 全表再生 → 判 Gate 1/2                 |
| 7 月余下     | W7/W8/W9 跑批判 Gate 3/4；W12 LSMGraph 时间盒；W10 启动；W11               |
| 8 月       | 缓冲：Gate 失败的 fallback 实验与文案；W12 收尾                               |
| 9 月       | 数字冻结（全表脚本再生）、论文重写（叙事按 Gate 定稿）、内审                               |
| 10/10     | SIGMOD R4 摘要/COI；**10/17 全文**；miss → PVLDB Vol.20 的 11/1 或 12/1 |


---

## §5 Codex 会话约定

- **一工单一会话**；会话模板：§1 自检 → 读 §0 相关证据文档 → 执行 → 验证 → 留 dated trace →
把完成状态追加进 `baseline/seml0-gap-analysis-20260612/implementation-status-20260612.md`
（代码项）或 `baseline/seml0-linux-rerun-status-20260612-cn.md`（实验项）。
- 禁止：重做 §0 已完成项；未做 ETA 评估就起 >30min 任务；并发多个 SF100 级任务；覆盖旧 trace；
在 W2 冻结后、W6 结束前改引擎行为（bug fix 除外）。
- 遇到与本文档冲突的仓内新状态（别的会话推进了），以仓内状态文档为准并更新本文档对应小节。

---

## §6 发表完成线（Definition of Done，分三层；这是「做到哪就能投」的明确答案）

> 顶会无「保证录用」。下面给的是**可投线**（所有 desk-reject 风险关闭、贡献诚实成立）与**有竞争力线**。
> 关键事实：W7/W8/W9 的**代码已在 W3/W4/W5 合并完成**，所以 Tier 2 只剩「跑」不剩「写代码」——
> 从 Tier 1 到 Tier 2 的边际成本只是几次 SF30 跑批（小时级，非开发），**因此真实目标定在 Tier 1+2**。

### Tier 1 — 最低可投线（必须全做；做完即可投，claim 按 Gate 收窄）
| 项 | 关掉的风险 | 完成判据 |
|---|---|---|
| **W6** SF100 全矩阵复测 | G1 单次测量 / G3 kv 假标注 / G4 RSS / G8 naive 锚 / read_bytes caveat | 9 变体 JSON 齐全、真实 kv-lsm 入表、各变体 3 轮 mean±stddev、vs-naive 0 mismatch、oracle 上界列；判 Gate 1/2 |
| **W13** schema-evolution 实验 + invariant/theorem | 「schema 变了旧存储是否失效/漏边」必问项 | SF30 实验：新 label 跳旧 exact 段、0 mismatch、mixed 保守读；3 invariant + theorem 写入正确性小节（判 Gate 7） |
| **W10** 数据卫生 + claim 安全化 | 数据完整性 / 过度声明 | 全表从 raw JSON 再生；claim 按 Gate 1/3/4/5/7 结果落档；limitations 显式（external/RSS/kv/tail/property 不藏） |

**到 Tier 1 即可投**：所有一票否决项已关，论文诚实、贡献成立（哪怕标题收窄为「Query-Semantic L0 Design for
LSM-Based Dynamic Property Graphs」、property 写成机制+microbench）。现实预期：borderline，靠 R4 自带 revision 轮有补救空间。

### Tier 2 — 有竞争力线（代码已就绪，只需跑批；**推荐的真实目标**）
| 项 | 升级了什么 | 完成判据 |
|---|---|---|
| **W7** feedback-only workload-shift（代码 W3 已完成） | 杀掉「benchmark 过拟合」(G5)；贡献升为 **self-tuning materialization** | SF30 三组（feedback-only/static/no-feedback）相位切换，预算 N 个 flush 内重定向（判 Gate 4） |
| **W8** property + 2-hop（代码 W4 已完成） | 标题保住 **property graph** 全义 + 多跳相关性 (Gate 3) | SF30 四类 property 谓词 + 2-hop typed expansion 出正向 candidate/latency |
| **W9** 稳态混合读写（代码 W5 已完成） | 坐实 **LSM 动机本身** (Gate 5) | SF30 30–60min time-series：candidate/p99/rewrite/flush-stall 随 L0 churn |

**到 Tier 1+2 = 强投稿**：标题保留全义；三个差异化贡献立住（exact-proof budgeted pruning + feedback self-tuning +
schema/snapshot-safe）；Gate 1/2/3/4/5/7 全绿或诚实有界。

### Tier 3 — 加分项（绝不阻塞投稿；缺了用 limitations/related-work 兜底）
- **W12** LSMGraph 外部对比（可构建则 SF10 实测；否则定性 + 设计差异表）。
- **W11** legacy 死代码清理（artifact 卫生）。
- oracle 上界打磨、更多规模点。

### 一句话
**做到 Tier 1 就能投；做到 Tier 1+2 才值得投**（因为 Tier 2 代码是已付的沉没成本，只差跑）。Tier 3 随缘。
顺序：W6 过夜 → 判 Gate 1/2 → W7/W8/W9/W13 跑批（均 SF30 级，单-SF100 规约只约束 W6，故这批可错峰并行）→
W10 写作冻结 → 投。按此节奏比 10/17 截稿留 ≥1 月缓冲。

