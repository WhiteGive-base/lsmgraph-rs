# Path B / C2 进度跟踪表（living doc）

> 日期：2026-06-18 起，长期更新。
> 配套：策略/计划见 `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md`（讲"为什么/做什么"）；本表讲"做到哪、做完没"。
> 来源：用户的 "SemL0/K4 Path B 后续执行交接文档"（C2 semantic merge retention，Stage 0–10）。
>
> **状态图例**：✅ 完成 / 🟡 进行中或部分 / ⬜ 未开始 / ⏸️ 阻塞 / ⏭️ 可选。

---

## Part A — 计划评估摘要

**结论**：计划方向正确、纪律优秀、可执行。采纳。主要修正是把"已完成"和"已存在能力"从工作量里扣掉，让火力落到 **pruning-surface 测量** 这个真正的新增点。

**强项**：GO/PARTIAL/FALLBACK 三门 + "不能写"清单 + 强制同表报告 rewrite/write-amp；C2 主表 before/after 收益与代价同表；SegmentSemanticState 用正交多维 struct；SF1→SF10→SF30 + smoke 先行 + 资源 ETA/abort。

**基于代码的校准**（影响工期与"已完成"标注）：
1. **Stage 0 已可回答**：分支 `codex/sf100-basegraph-bench` @ `c2aa16c`；`src/`+`tests/` 中 `K4` = 0（已全改中性名）。"远端仍有 K4*" 的分支不成立。
2. **Stage 1 已完成**：outline 已升级 Path B + 三贡献 + 3 处小修。
3. **Stage 3 比估计轻**：naive vs semantic_edge **两 policy 已存在**（`graph.rs:3364` 的 `semantic_partition_outputs` 开关：`split_semantic_compaction_segments` vs `split_property_compaction_segments`）；代价计数器已存在（`LevelCompactionDecision{input/output_segments,input/output_bytes}`、`metrics.compaction_input/output_bytes`、`storage_compaction_latency`、L0 `estimated_rewrite_bytes`）。**真正新增 = pruning-surface summary（exact/mixed/unknown… 段计数+ratio，merge 前后）+ logical_update_bytes（write_amp 分母）+ SegmentSemanticState 封装**。degree/property merge 是新的 → 标 ⏭️ 可选。
4. **测量正确性**：`exact_surface_ratio_before/after` 必须在**参与本次 merge 的同一段集合**上算（before=输入段、after=输出段），不能用全局 level 状态。
5. **naive 对照点是"质"不是"量"**：naive 仍按 `segment_target_bytes` 切，段数未必更少；差别在"段是 mixed、exact_surface_ratio 低"。主对照指标用 `exact_surface_ratio`，不是段数。
6. 源交接文档有结构瑕疵（enum 截断、Stage4/5 段落混入 Stage3），按其 §15 总表执行即可。

---

## Part B — 进度表

**当前位置**：核心里程碑 = **Gate-C2 = GO**；单文件 md 论文已组装+润色（801 行）。
**真实 LDBC SF30 ✅ 确认**（`stage5-sf30-real-summary`）：1.09B 边、40 分区→528 exact L1，semantic retention **1.0** vs naive **0.0**，write_amp 1.24 vs 1.07。已折入 table-c2/plot-data/§5/§7/§9。
**SIGMOD 评估**：三大缺口均显著改善——C2 真实 SF30 背书 ✅；**(a) 外部 baseline = LiveGraph SF10 真实系统已整理入 §8 + `external-baseline-md.md`**（SF100-external 不可行=诚实边界）；(d) 动态退化 W9 已入 §7 Table 2。外部 baseline 源：`baseline/seml0-baseline-defense-20260619/`（作者机整理）+ raw 验证 `remote-logs/livegraph-sf10-20260612/`。
**SIGMOD hardening pass（2026-06-19）已做**（用户判定 = SIGMOD submission candidate）：stale wording/path/TODO/cross-ref 清零；C2 表改三层（A 受控 read-amp / B 真实 retention+cost）；related work 具名 LSMGraph/BACH/RocksDB-RUM-Dostoevsky-learned/adaptive-indexing；LiveGraph SF10 强化 + 明确 SF10-only；不卖 uniform latency。可见副本已同步 `baseline/seml0-lifecycle-paper/`。
**Stage 10 剩**：venue + TeX/PDF（本机无 LaTeX）+ bibkeys；SemL0-vs-LiveGraph SF10 head-to-head 仍缺 SemL0 SF10 列；（可选）LSMGraph head-to-head（~1 天）。

> ⚠️ 执行中发现并修复：`src/error.rs` 工作副本被一段 stray `ee` 损坏（line 2，非法 Rust），导致 `Result` 全 crate 无法解析、74 个编译错误连锁。与 C2 无关、且早于本次会话（`error.rs` 启动即 `M`）。已 `git checkout HEAD -- src/error.rs` 恢复（HEAD 内容 = 单行 `pub type Result<T> = anyhow::Result<T>;`，仅丢弃 `ee` 垃圾，无真实改动损失）。

| # | 阶段 | 预计 | 累计估时(低–高) | 状态 | 完成判据(简) | 现状/校准 | 实际(run-id/日期) |
|---|---|---|---|---|---|---|---|
| 0 | 仓库状态校准 | 0.5d | 0.5d | ✅ | 分支/HEAD/API 命名确认+留档 | `stage0-repo-state-20260618-cn.md`：`codex/sf100-basegraph-bench`@`c2aa16c`，K4=0，入口已核 | 2026-06-18 |
| 1 | S0 outline 冻结 | 0.5–1d | 1.0–1.5d | ✅ | 三贡献一致、C2=唯一承重、latency=supporting | 已完成；§5 措辞已对齐计划 | 本会话 |
| 2 | C2 设计规格 | 0.5–1d | 1.5–2.5d | ✅ | 问题/指标/表列写死、判据明确 | 已定稿：`c2-merge-retention-design-20260618-cn.md` + `paper/tables/table-c2-retention-schema.md`（§6 阈值为提案，Stage 5 数据落定后确认，不阻塞） | 2026-06-18 |
| 3 | C2 metrics/state | 1–3d | 2.5–5.5d | ✅ | cost counters + pruning-surface summary + state struct，测试绿 | 全done：`SegmentSemanticState`+`PruningSurfaceSummary`+`logical_update_bytes`+`LevelCompactionDecision.surface_before/after`(接 `compact_level_to_next`)。测试：lib 69 + engine 61（新增端到端 `semantic_merge_retains_pruning_surface_while_naive_merge_degrades_it`）。另修复 error.rs 损坏 | 2026-06-18 |
| 4 | C2 SF1/SF10 smoke | 1–2d | 3.5–7.5d | ✅ | runner+表链路跑通、0 mismatch | `c2_merge_retention` bin + `stage4-smoke-*`：semantic retention 1.0 vs naive 0.0、read_bytes naive 4×放大/semantic 持平、write_amp 1.87 vs 1.22、0 mismatch（GO 信号） | 2026-06-18 |
| 5 | C2 SF30 正式 | 2–4d | 5.5–11.5d | ✅ | C2 主表完整、收益与代价同表 | 合成 SF1+SF10-class 一致（`stage5-scale-summary`）：retention 1.0 vs 0.0、read naive 4–6×放大、write_amp 1.85 vs 1.14、0 mismatch。**真实 LDBC SF30 ✅ 确认**（`stage5-sf30-real-summary-20260619-cn.md`）：1.09B 边、40 分区→528 exact L1，semantic retention **1.0** vs naive **0.0**，write_amp 1.24 vs 1.07（v1 FullCompact 失败→v2 build-l1 路径解决） | 2026-06-19 |
| 6 | **C2 判门** | 0.5d | 6.0–12.0d | ✅ | GO/PARTIAL/FALLBACK 定档 | **GO**（`stage6-gate-verdict`）：retention + 读放大消除压倒，写代价有界可解释。Path B 成立，保留 Lifecycle 标题 | 2026-06-18 |
| 7 | C1/C3 重述 | 1–2d | 7.0–14.0d | ✅ | 既有 W6/W8/W13/W14 证据纳入 lifecycle 叙事 | `paper/sections/04-*.md`(C1)+`06-*.md`(C3) 草稿完成；3 处数字 TODO 已填（W6 naive≈49.3M、W8≈22%、W13=10 tests） | 2026-06-18 |
| 8 | §5/C2 主章节 | 1–3d | 8.0–17.0d | ✅ | retention 与 rewrite cost 同表、不夸大 | `paper/sections/05-lifecycle-retention.md`（GO 结构，SF1/SF10c 结果表 inline，代价同表，claim 有界） | 2026-06-18 |
| 9 | Related work/limitations | 1–2d | 9.0–19.0d | ✅ | 覆盖 compaction/adaptive/learned-LSM + latency so-what | `08-related-work.md` + `09-limitations.md` + `reviewer-question-bank.md` 草稿完成 | 2026-06-18 |
| 10 | 投稿装配 | 3–5d | 12.0–24.0d | 🟡 | venue/TeX/PDF/页数/证据表 | md sections 已齐（04/05/06/08/09 + Q-bank）+ RQ6 结果表已渲染 + 3 处数字 TODO 已填；**剩：venue 选择 + TeX/PDF 编译（本机无 TeX 引擎）+ 整本组装 + bib 引用**（需人工/换环境） | 进行中 |

**里程碑**：最小 Path B 验证 = 到 Stage 6 = **6–12 天**；完整 Path B = **12–24 天**。

**可选增强（⏭️，仅在追强 GO/PARTIAL 时做，不阻塞主线）**：
- `semantic_degree_merge` / `semantic_property_merge`（需给 `split_semantic_compaction_segments` 加 degree/property 分区键）+ 对应 degree-selective / property-selective workload。

---

## 文件清单（所有 C2 阶段文档集中处）

C2 工作/进度文档统一放在 **`baseline/path-b-c2/`**；论文成品（section / tex 表）按惯例留在 `paper/`。

| 文档 | 位置 | 阶段 |
|---|---|---|
| 进度跟踪表（本文件，索引） | `baseline/path-b-c2/progress-tracker-cn.md` | 全程 |
| 仓库状态校准 | `baseline/path-b-c2/stage0-repo-state-20260618-cn.md` | 0 |
| C2 设计规格 | `baseline/path-b-c2/c2-merge-retention-design-20260618-cn.md` | 2 |
| C2 表（schema + 已填结果） | `paper/lifecycle/tables/table-c2-retention-schema.md` + `table-c2-retention.md` | 2/5 |
| C2 runner | `src/bin/c2_merge_retention.rs`（bin `c2-merge-retention`） | 4/5 |
| SF1/SF10 smoke 总结 + 数据 | `baseline/path-b-c2/stage4-smoke-summary-20260618-cn.md` + `stage4-smoke-sf1-20260618.json` | 4 |
| SF10-class 放大总结 + 数据 | `baseline/path-b-c2/stage5-scale-summary-20260618-cn.md` + `stage5-scale-sf10class-20260618.json` | 5 |
| C2 判门 verdict | `baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md` | 6 |
| **所有 paper 成品（独立文件夹）** | **`paper/lifecycle/`**：`seml0-lifecycle-paper-draft-md.md`(单文件 824 行) + `sections/`(§1–§10) + `tables/` + `reviewer-question-bank.md` + outline(md/cn) | 7/8/9/10 |
| **plot-data（画图用真实数据表）** | `paper/lifecycle/tables/plot-data-md.md`（W6 SF100 / W9 时序 / W8 / C2 / 外部 baseline 全部实测数字 + 图类型） | 10 |
| 外部 baseline（整理入 paper） | `paper/lifecycle/tables/external-baseline-md.md`（LiveGraph SF10 真实系统 + RocksDB-style + KV-sim，诚实标注）；源记录 `baseline/seml0-baseline-defense-20260619/`（作者机） | 8 |
| 真实 SF30 runner + 日志 | `baseline/path-b-c2/run_c2_sf30_real_20260618.sh` + `remote-logs/c2-sf30-real-20260618/` | 5 |
| SF30 正式总结 | `baseline/path-b-c2/stage5-sf30-summary-*-cn.md`（待产） | 5 |
| C2 判门 verdict | `baseline/path-b-c2/stage6-gate-verdict-*-cn.md`（待产） | 6 |

## 更新规约
- 每完成一阶段：翻状态格（⬜→🟡→✅）、填"实际(run-id/日期)"、在该行"现状/校准"补一句产出。
- 跑实验阶段（4/5）：必须有 `remote-logs/<run-id>/`（含 DONE/FAILED）+ `baseline/path-b-c2/` dated summary；表格可由脚本从原始 JSON 再生。
- 看下一步：`grep -n "🟡\|⬜" baseline/path-b-c2/progress-tracker-cn.md`。
- 与 `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md` 冲突时，以仓内 dated 文档为准并回写本表。
