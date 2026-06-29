# SemL0 / K4 主线现状与后续阶段计划（2026-06-17，Path B 已锁定）

> 这份文档回答两件事：**(A) 之前每个阶段做了什么**（现状回顾），**(B) 现在每个阶段需要做什么**（Path B 执行计划）。
>
> 它**接续并合并**下面两份既有计划，后续以本文档为准：
> - `PLAN-SEML0-SIGMOD2027-CODEX-CN.md`（2026-06-12）：L0 论文骨架（W1–W13），**实验已基本跑完**。
> - `SEML0-K4-PAPER-EXPERIMENT-PLAN-20260617-CN.md`（2026-06-17）：升级成 lifecycle 论文的实验大纲（E1–E6），**实验尚未跑**。
>
> **路线决策（已拍板）：走 Path B —— 把论文从"L0 物理设计"升级为"query-semantic LSM 全生命周期管理"。**
> Path A（直接收窄投 L0 论文）**仅作为截稿兜底安全网**，不再作为并列分支。
>
> **命名约定（已拍板）**：`K4` = 论文工作包/实验变体**代号**，**不是内核符号**。内核 API 已在提交 `c2aa16c` 去掉所有 `K4*`，改用中性命名（`LevelMergePolicy / run_maintenance / LifecycleReport / split_semantic_compaction_segments` …）。对照表见 `baseline/mainline-chatgpt-review-20260617-cn.md`。**功能都在，只是名字换了。**

---

## 0. 一页纸现状（TL;DR）

- **L0 论文证据链：基本跑完。** W1–W14 + 三道收敛 Gate 全部有结论。
- **强 claim 全部落到 FALLBACK，必须收窄：**
  - 稳定 latency 优势 → 不成立（Gate 1 FALLBACK：b64 比 schema 快约 11%，但 1σ 区间重叠）。
  - composite semantics 稳定优于 edge-type-only → 未证明（W14 PARTIAL GO）。
  - 能写的是：**read-amplification / candidate-segment 显著下降 + 正确性安全 + RSS 实测不劣**。
- **现有论文已 source-ready**：`paper/` 下 md-section 草稿齐全（`final-paper-outline-md.md` 等），标题为 *Query-Semantic **Physical Design***；剩 template/PDF/page/venue 打包这种**非实验工程**。
- **Lifecycle 升级（K4）：内核已迁移，实验未跑。** `run_lifecycle* / LevelMergePolicy / L1+ semantic split+filter / 事件驱动 maintenance scheduler` 都在主仓库且单测通过（提交 `b56f3f8`），但 **merge retention / 写放大 cost / auto-maintenance 的性能实验一个都没跑**。
- **Path B 的唯一硬核新实验 = C2 语义 merge retention**；其余阶段大多是复用已有证据 + 重新叙述 + 可压缩的防御性章节。**火力集中在 C2。**

---

## A. 之前每个阶段做了什么（现状回顾）

### A.1 代码地基（C1–C13，6/12 前已完成）

L0 exact-proof pruning 的内核地基（预算口径、degree directory sidecar、precise offset read、SourceBloom 持久化、feedback→budget、CSR cache 等）**已全部落地并测过**。权威清单见 `baseline/seml0-gap-analysis-20260612/implementation-status-20260612.md`。唯一当时未做的代码项 C10（CSR 单次调用固定开销）已归入 W2 处理。

### A.2 W 系列工单进度

| W | 目标 | 做了什么 | 结论 / Verdict | 关键产物 |
|---|---|---|---|---|
| **W1** | release 重建 + SF1/SF30 冒烟 | 修复 SF30 sidecar / CSR cache 回归（`5236a1d`），验证 C7/C4/C11/C9 效果 | ✅ 就绪，给 W6 放行 | `remote-logs/w1-*` |
| **W2 / C10** | 消减 ~391μs/次 CSR 固定开销 | 落分阶段计时插桩；**离线**延迟归因（复用 W6/W8/W14 JSON，未起 heavy runner） | ⚠️ **FALLBACK / claim-safety**：能解释延迟矛盾、约束 claim；**不能写 broad latency superiority** | `baseline/c10-latency-attribution-summary-20260617-cn.md/.tsv` |
| **W3** | feedback-only 模式 + workload-shift runner（代码） | 关静态 edge-type 权重、冷启动均匀、打分只用 feedback；runner | ✅ 代码+单测完成 | `2ec4610` |
| **W4** | property 谓词 + 2-hop（代码） | 四类谓词模式 + 2-hop typed expansion runner | ✅ 代码完成 | `93e681b` |
| **W5** | 真实 store 稳态混合读写 runner（代码） | 后台 ingest/delete + 前台 sampled 查询的 runner | ✅ 代码完成 | `2fcf2fe` |
| **W6** | **SF100 全矩阵（9 变体）+ Gate 1/2**（原计划重心） | naive/schema/edge-type-only/semantic/budg-b64/b256/b1024/**kv-lsm 真跑**/oracle 全跑；3 轮 mean±stddev；vs-naive correctness | ✅ **DONE**。所有 compare `mismatches=0`。**Gate 1=FALLBACK**（b64 比 schema 快 11.15% 但 1σ 重叠）；**Gate 2=GO**（RSS schema/naive=0.99x、b64/naive=0.91x，可作实测主结果） | `baseline/sf100-matrix-20260613-cn.md` |
| **W7** | self-tuning / workload-shift（SF30 正式） | 三组 feedback-only / static / no-feedback 相位切换 | ✅ **Gate=GO**（caveat：real-SF30-derived，非 full-store production trace） | `baseline/w7-sf30-workload-shift-summary-20260615-cn.md` |
| **W8** | property + 2-hop（SF30 正式） | 四类 property 谓词 + 2-hop | ✅ DONE。property：candidate/body/read/elapsed 改善；**2-hop：body/read/elapsed 改善，但 candidate L0 高于 schema → 不能写 universal 2-hop candidate reduction** | `baseline/w8-property-2hop-summary-20260615-cn.md` |
| **W9** | 稳态混合读写（SF30 正式） | schema/budg-b64/semantic 各 ~29 万查询、6 checkpoint | ✅ DONE。budg-b64：candidate L0 −4.1%、p99 −1.2%、p50 +1.8%；**semantic：p50 −63.9%、p99 −82.5%，但 candidate L0 +86.3%、L0 files +93.4%**（收益/代价并存，按 delta 表写） | `baseline/w9-steady-state-summary-20260615-cn.md` |
| **W10** | 写作冻结 + claims 安全化 | claims 按 Gate 收窄；`paper/` md-section 草稿 + 表格 + artifact README 装配 | 🟡 **SOURCE-READY**；剩 template/PDF/page/venue archive（**非实验工程**） | `baseline/seml0-w10-submission-freeze-draft-cn-20260615.md` |
| **W13** | schema evolution 实验 + invariant/theorem | 10 个 schema-evolution 测试；stable edge_type id 下旧段可读、剪枝安全 | ✅ DONE。可写 **additive / 定宽 property 边界**内的 schema-evolution correctness；不写 full migration | `baseline/w13-schema-evolution-summary-20260614.md` |
| **W14** | semantic necessity（composite vs edge-type-only） | SF30 smoke + SF100 reuse-store type-only/minimal matrix | ⚠️ **PARTIAL GO / FALLBACK**：correctness parity（`mismatches=0`）成立；**但未证明 composite semantics 稳定优于 edge-type-only** | `baseline/w14-final-verdict-20260617-cn.md` |
| W11 / W12 | legacy 清理 / LSMGraph 外部对比 | 未做（Tier 3，按"停止扩实验"停掉） | ⏸️ 未做（Path B 下 W12 外部对比重新变得相关，见 §B） | — |

### A.3 三道收敛 Gate 的最终结论（2026-06-17 权威）

| Gate | 名称 | 状态 | 结论 |
|---|---|---|---|
| **G1** | W14 必要性 | **PARTIAL GO** | correctness parity + schema-vs-pruned 证据可用；composite 必要性未证明 |
| **G2** | C10 延迟归因 | **COMPLETED / FALLBACK-CLAIM-SAFETY** | 能解释 latency 矛盾、约束 claim；不写 broad latency 优势 |
| **G3** | S0 semantic dilution 诊断 | **COMPLETED / FALLBACK-PROXY-SIGNAL** | W14 degree-class 强 proxy、W8 中等、W6 core 弱 |

**一句话总结现状**：L0 论文的**实验已做完、强 claim 已收窄到安全 claim、论文 source-ready**。Path B 在此基础上**升级故事**，把已有证据重新组织进"生命周期"框架，并补一块关键拼图（C2）。

---

## B. 现在每个阶段需要做什么（Path B 执行计划）

> Path B 的论文升级动作落到现有 `paper/` 草稿上很具体：
> - **标题**：`Physical Design` → **`Lifecycle Management`**（`SemL0: Query-Semantic Lifecycle Management for LSM-Based Dynamic Property Graphs`）。
> - **§4 Query-Semantic Physical Design** → 贡献 **C1（pruning surface 生成与利用）**，基本只改叙述。
> - **§5 Feedback-Driven Semantic Compaction** → **升级为贡献 C2（跨 flush + compaction 的 lifecycle *retention*）**：把现有 L1+ semantic split/filter + 新的 C2 实验吸收进来，这是 Path B 的实质内容增量。
> - **§6 Schema Evolution And Snapshot-Correct Deltas** → 贡献 **C3（snapshot/schema 安全）**，已强，只改叙述 + 补 invariant/theorem。

### 阶段顺序与内容

| 阶段 | 论文角色 | 已有基础（复用） | 现在要做（增量） | 优先级 |
|---|---|---|---|---|
| **S0 主线冻结**（不写代码） | 全篇约束 | 本文档 + 现有 outline | 锁定：标题升级、主 claim=read-amp+retention+safety（latency 为 supporting）、K4=代号、prototype 定位、三贡献 C1/C2/C3。产物：更新论文 outline 的 Positioning/Abstract/Contributions | **先做（阻塞后续）** |
| **C2 cost 计数器**（C2 前置，纯内核增量） | C2 代价侧 | 现有 maintenance/lifecycle report | Engine 补：per-level rewrite bytes、logical update bytes、write amplification、maintenance foreground blocking time、`LevelMergePolicy` reason 字段。过 correctness gate | **必做** |
| **C2 语义 merge retention 实验** 🔑 | **贡献 2 = 唯一硬核** | `LevelMergePolicy{semantic_partition_outputs}`、`split_semantic_compaction_segments`（**当前仅按 (src_label, edge_type) 分组**）、L1+ filter | **(a)** 量化"普通 merge 退化 pruning surface vs 语义 merge 保留"：exact-segment ratio / mixed-ratio / candidate before-after / read-bytes before-after / pruning retention；**(b)** 评估把 degree/property 也纳入分区键的**收益 vs 写放大·段数代价**（真实 tradeoff，必须同表）。先 SF1→SF10 验证 runner，SF30 出主结果。判**新 Gate**「语义 merge 是否在可控写放大下保住 pruning surface」 | **必做（设硬时间盒）** |
| **S2 segment 状态机** | C1/C2 表达支撑 | **已 ~90% 存在**（topology/property/degree/tombstone/schema 各维字段都在） | 封装成 `SegmentSemanticState` struct（**正交多维，不是扁平 enum**）+ 派生对外标签；论文用它讲 retention 退化/恢复 | 中（随 C2 一起做） |
| **C1 / C3 重新叙述** | 贡献 1 / 3 | W6/W8/W14（C1）、W13+lifecycle 单测（C3） | 把已有数据归到"pruning surface 生命周期"框架；补 pruning reason taxonomy 表；写 3 invariant + theorem（"schema evolution 只降剪枝精度、不漏读"） | 必做（写作，可并行） |
| **related work 定位** | 必备 | L0 计划已记 LSMGraph(VLDB'24)/BACH(PVLDB'25) 为正交锚点；现有 §8 已有骨架 | 显式对 workload-aware layout / semantic compaction / learned LSM tuning 立靶子（ChatGPT 漏了，reviewer 必问） | 必做（写作） |
| **latency "so what" 预案** | 必备 | G2 归因结论 | 准备"降 read bytes 但延迟不稳"被追问的答案（尾延迟 / CPU·IO 节省 / 选择性 workload 的确定性增益） | 必做（写作） |
| **E2 auto-maintenance under mixed workload** | lifecycle 闭环演示 | W9 稳态、`delta_graph_normal_api_triggers_auto_maintenance` 单测 | SNB mixed read/write 下 segment-count-over-time + tail latency + maintenance/feedback/cascade count（与 W9 部分重叠，增量做） | 防御性（可压缩） |
| **W17 写生命周期 / W18 schema cost model** | 审稿人防御 | auto_compaction 入口、schema catalog | flush latency / 写放大可控、open/recovery time 可解释；**做不完降级成 discussion + 有限实验** | 防御性（可压缩） |
| **W12 LSMGraph 外部对比** | 加分 | L0 计划已有方案 | Path B 下"外部 SOTA 基线"追问更突出；可构建则 SF10 实测，否则定性 + 设计差异表 | 加分（不阻塞） |

### Path A 兜底（仅一行，不展开）

若 C2 时间盒到期仍无正向结果：**标题退回 `Query-Semantic L0 Design …`，把 lifecycle 写成 mechanism + 有限实验，用现有 W6/W7/W8/W9/W13 证据直接走 W10 打包投稿**。所有一票否决项已关，仍可投。

---

## §5 硬性规约（跑实验时必须遵守）

1. **资源门限**：`/data` 可用 < 200GiB 或 MemAvailable < 80GiB → 硬停。构建/测试 `nice -n 10 cargo … -j 16`。
2. **单 SF100 任务原则**：同时最多 1 个 SF100 级 import/compact/bench。C2 实验**优先在 SF1→SF10→SF30 验证**，SF100 仅在确有必要且资源充足时跑。
3. **长任务事前 ETA + 进度信号 + abort 条件**（LiveGraph SF100 的 17–21 天尾部是教训）。
4. **留痕**：每次跑批 `remote-logs/<run-id>/`（含 DONE/FAILED 标记）+ `baseline/` dated 总结；不覆盖旧结果；表格可由脚本从原始 JSON 再生。
5. **改 `src/` 后** `nice -n 10 cargo test -j 16` 全绿再继续；先过 correctness gate（`cargo test --lib / --bin lsmgraph / --test engine_tests` + `snb-validate`）。
6. **遇到与本文档冲突的仓内新状态**，以仓内 dated 状态文档为准并回写本文档。

---

## §6 下一步最小动作（Path B）

1. **S0 冻结**：把标题升级、主 claim、K4=代号、prototype 定位、三贡献写进 `paper/final-paper-outline-md.md`（Positioning / Abstract / Contributions / §4·§5·§6 reframe）。**这一步做完才动代码。**
2. **补 C2 cost 计数器**（纯内核增量，过 correctness gate）。
3. **C2 merge retention 实验**：SF1/SF10 验证 runner 与指标链路 → SF30 出主结果；**收益与写放大/段数代价同表**；判新 Gate。
4. **C1/C3 重新叙述** + invariant/theorem + related work + latency so-what（写作，可与 2/3 并行）。
5. **S2 状态机封装**（随 C2 一起）。
6. C2 出结果后再决定 E2/W17/W18/W12 做到什么深度（按截稿余量裁）。

> 一句话：**Path B 现在唯一真正"要新做"的硬核是 C2（语义 merge retention）+ 它的 cost 计数器**；论文层面就是把标题改成 *Lifecycle Management*、把 §5 从"L0 feedback 自适应"升级成"跨层生命周期保持"，其余是复用已有证据重新叙述 + 可压缩的防御性章节。
