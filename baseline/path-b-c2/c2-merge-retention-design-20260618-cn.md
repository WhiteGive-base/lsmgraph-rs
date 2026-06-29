# C2 — Semantic Merge Retention 实验设计规格书

> 日期：2026-06-18  阶段：Path B / Stage 2（设计规格，纯文档，无代码）
> 配套：进度表 `baseline/path-b-c2-progress-tracker-cn.md`；策略 `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md`。
> 目的：在写代码（Stage 3）和跑实验（Stage 4/5）之前，把 C2 要证明什么、怎么测、怎么判**写死**。
> 表列定义另见 `paper/tables/table-c2-retention-schema.md`。

---

## 1. C2 要证明 / 不证明的问题

**要证明（论文承重）**：
1. 普通 merge（不带语义分区键）会让 segment 的 **semantic pruning surface 退化**（exact 段变 mixed）。
2. semantic-aware merge 能在 merge 后**保留或重建**该 surface（按 `(src_label, edge_type)` 重新分区 + 重新推导 exact metadata）。
3. 保留 surface 的**代价可量化且可接受**：rewrite/output bytes、output segments、write amplification、compaction latency。

**不证明（claim 边界，违反即收窄）**：
- 不证明 uniform latency speedup。
- 不证明 fully optimal / production-grade compaction scheduler。
- 不证明任意 schema migration。
- 不证明 degree/property 维度的 retention 是默认能力（那是可选增强，见 §4）。

---

## 2. 复用现状（避免 Stage 3 重复造轮子）

| 需要的能力 | 现状 | 位置 |
|---|---|---|
| naive_merge policy | **已存在**（`semantic_partition_outputs=false` → `split_property_compaction_segments`，仅按大小切，不带语义键） | `src/graph.rs:3364-3368` |
| semantic_edge_merge policy | **已存在**（`semantic_partition_outputs=true` → `split_semantic_compaction_segments`，按 `(src_label, edge_type)` 分区） | `src/graph.rs:3364-3365`, `4114` |
| 每次 merge 的 input/output bytes、段数 | **已存在** | `LevelCompactionDecision{input_segments,output_segments,input_bytes,output_bytes}` `graph.rs:3414` |
| 全局 compaction 计数/字节/延迟 | **已存在** | `metrics.compaction_count/_input_bytes/_output_bytes`、`storage_compaction_latency` `metrics.rs:58-60,104` |
| L0 侧 rewrite 估计 | **已存在** | `L0CompactionDecision.estimated_rewrite_bytes` |
| 段级语义维度字段 | **已存在**（~90%） | `CsrSegmentMeta.{summary_completeness, property_summary_completeness, degree_class_exact, may_contain_tombstones, src_label, edge_type_partition, schema_epoch}` `csr/format.rs:108` |

**→ Stage 3 真正要新增的只有 3 项**（见 §7）：
1. **pruning-surface summary**（exact/mixed/unknown… 段计数与 ratio，merge 前后）。
2. **logical_update_bytes**（write_amp 的分母，当前缺）。
3. **`SegmentSemanticState`** 正交多维 struct（封装已有字段，供测量与论文表达）。

**结论**：naive vs semantic_edge 的**核心对照不需要新 merge 代码**，Stage 3 工作量集中在"测量"。

---

## 3. 对照 policy

| policy | 含义 | 代码 | 本期 |
|---|---|---|---|
| `naive_merge` | `semantic_partition_outputs=false`，输出按大小切、(label,edge_type) mixed | 已存在 | **必做（对照基线）** |
| `semantic_edge_merge` | `semantic_partition_outputs=true`，按 `(src_label, edge_type)` 分区 | 已存在 | **必做（主系统）** |
| `no_compaction` | merge 前布局，作 surface/workload 的 before 基准 | 用 merge 前快照 | 必做（作 before） |
| `semantic_degree_merge` | 分区键再加 degree_class | 需扩 `split_semantic_compaction_segments` | ⏭️ 可选（仅追强 GO） |
| `semantic_property_merge` | 分区键再加 property presence/summary | 同上 | ⏭️ 可选 |

---

## 4. 实验设计（A/B on policy，同输入）

为满足"apples-to-apples"（校准#4），采用**同一 pre-merge 输入、分叉两 policy**的设计，而不是单 store 自比：

```text
对每个 (dataset, workload):
  1. 构造一个会触发 level compaction 的 pre-merge 状态
     （某 L1 层有 > fanout 个、跨多种 (src_label, edge_type) 的 segment）。
  2. 在"将被 compaction 的段集合"（selected_source ∪ selected_target）上算 surface_BEFORE。
  3. 复制该状态，分别用 policy = naive_merge / semantic_edge_merge 各跑一次 compaction。
  4. 在各自 output 段集合上算 surface_AFTER。
  5. 在各自 merge 后布局上跑 workload 的 signatures，记 candidate / read_bytes（= *_after）；
     pre-merge 布局上同一 workload 记 *_before。
  6. 记代价：input/output bytes、rewrite、write_amp、output_segments、compaction latency。
  7. correctness：两 policy 结果 digest 与 naive anchor compare，mismatches 必须 = 0。
```

**关键测量规则（写死，防错）**：
- **surface_before 只算一次**，在"参与本次 merge 的同一段集合"上（不是全局 level、不是全 store）。两 policy 共用同一 before。
- **surface_after 各 policy 各算**，只在该次 merge 的 output 段上。
- compaction 对边集是无损的（除 tombstone GC），故 before/after 的**边加权**比值可直接比较。
- **naive 的对照点是"质"不是"量"**（校准#5）：naive 仍按 `segment_target_bytes` 切，output_segments 数量未必更少；差异体现在 `exact_surface_ratio` 低、段是 mixed。主对照指标 = `exact_surface_ratio` / `pruning_retention`，**不是段数**。

**加分角度（retain → rebuild）**：若 pre-merge 输入里含 mixed 段（例如先做一次 naive merge 制造 mixed L1，再做 semantic merge L1→L2），semantic merge 会按 (label,edge_type) 重切并重推导 exact metadata → **`exact_surface_ratio_after` 可能高于 before**（retention > 1.0）。这能把 thesis 里的 "retain **or rebuild**" 坐实，建议作为一个子场景。

---

## 5. 指标定义（边加权为 headline，段计数为辅）

段级布尔（基于 `CsrSegmentMeta`，与读路径 `signature_pruning_decision` 对齐）：
- `topology_exact` = `summary_completeness == Exact` ∧ `src_label != UNKNOWN_SOURCE_LABEL` ∧ `edge_type_partition != MIXED_EDGE_TYPE`
- `topology_mixed` = `src_label == UNKNOWN_SOURCE_LABEL` ∨ `edge_type_partition == MIXED_EDGE_TYPE` ∨ `summary_completeness == Unknown`
- `degree_exact` = `degree_class_exact`
- `property_exact` = `property_summary_completeness == Exact`
- `tombstone_clean` = `!may_contain_tombstones`
- `schema_current` = `schema_epoch == current_epoch`（否则 OlderEpoch；编码需重解释则 SchemaUncertain）

聚合（分母 = 测量段集合的总边数 `edge_count` 之和）：
- `exact_surface_ratio` = Σ edges(topology_exact) / Σ edges  ← **headline**
- `mixed_ratio` = Σ edges(topology_mixed) / Σ edges
- `pruning_retention` = `exact_surface_ratio_after` / `exact_surface_ratio_before`（per policy）
- 段计数版本（`*_seg`）作为辅助列同时输出。

workload 侧（在 merge 前后布局上各跑一遍 signatures）：
- `candidate_before/after` = candidate_l0+l1 segments（沿用现有 metrics 漏斗计数）
- `read_bytes_before/after` = header+offset+body read bytes

代价侧：
- `input_bytes / output_bytes`（已存在）；`rewrite_bytes` = output_bytes（level merge 即全重写输出）
- `logical_update_bytes` = 该次 merge 覆盖的逻辑边数据大小（**新增计数器**，见 §7）
- `write_amp` = `rewrite_bytes / logical_update_bytes`
- `output_segments`（已存在）；`compaction_latency`（已存在）

---

## 6. 成功判据（Gate-C2，阈值为提案，Stage 2 签收时定稿）

主决策规则看 **semantic_edge 相对 naive 的关系**，不是绝对魔数。

**GO**（保留 Lifecycle Management 标题，C2 为第二贡献，§5 主章节，新增 RQ6 主表）：
- `exact_surface_ratio_after(semantic) ≫ exact_surface_ratio_after(naive)`，提案：semantic 保留 ≥ 0.8×before，naive 跌到 ≤ 0.3×before；
- 目标 workload 的 `candidate_after` 或 `read_bytes_after`：semantic 明显优于 naive（提案：≥ 20% 改善）；
- `write_amp(semantic)` 相对 naive 可接受（提案：≤ 1.5×），`output_segments` 不爆炸（提案：≤ 2×）；
- `correctness_mismatches = 0`。

**PARTIAL**（保留标题，但 C2 写成 prototype + tradeoff，§9 明示代价；不写 low-overhead）：
- surface 有保留，但 `write_amp` / `output_segments` 偏高；或只在部分 workload 有效。

**FALLBACK**（退 Path A，标题改 L0 Design，C2 写成 mechanism/future work）：
- surface 无明显保留；或 candidate/read_bytes 无改善；或代价不可接受；或出现 mismatch。

---

## 7. Stage 3 实现清单（本规格驱动）

**新增 counters / report 字段**（不污染内核语义，runner 只收集）：
- `logical_update_bytes`（per compaction + 累计）。
- pruning-surface summary：在 `LevelCompactionDecision` 增 `surface_before/after`（exact/mixed/unknown 段计数 + 边加权 ratio），或单独 `MergeRetentionReport`。
- 可选：foreground blocking time（若现有 `io_write_blocking_latency` 不足以代表 compaction 阻塞前台）。

**新增类型**：
```rust
pub struct SegmentSemanticState {   // 正交多维，封装已有字段，非扁平 enum
    pub topology: TopologySummaryState,   // Exact / Mixed / Unknown
    pub degree:   DegreeSummaryState,     // Exact / Mixed / Unknown
    pub property: PropertySummaryState,   // Exact / Conservative / Unknown
    pub tombstone: TombstoneState,        // NoTombstone / TombstoneSensitive
    pub schema:   SchemaState,            // Current / OlderEpoch / SchemaUncertain
}
```
- 由 `CsrSegmentMeta` + `current_epoch` 派生（`fn semantic_state(&self, current_epoch) -> SegmentSemanticState`）。
- 复用 `SemanticSummaryCompleteness{Exact,Conservative,Unknown}`（`schema.rs:576`）。

**测试（correctness gate 必绿）**：
- 单测：naive merge 后 `exact_surface_ratio` 下降；semantic merge 后保留/重建；mixed 输入下 semantic 重建使 after≥before；surface 计数与手工构造一致；两 policy 结果与 anchor 0 mismatch。

---

## 8. workload 与 dataset

| 轴 | 值 | 本期 |
|---|---|---|
| dataset | SF1（smoke/正确性）、SF10（调参）、SF30（主结果） | 必做 |
| workload | edge-selective（typed neighbor）、mixed | 必做 |
| workload | degree-selective、property-selective | ⏭️ 仅配 degree/property merge |
| repeats | smoke 1；正式 ≥ 3（取 mean±stddev） | 必做 |

---

## 9. 资源规约 / abort（沿用仓库制度）

启动前记 `df -h /data`、`free -h`、`ps aux | grep -E 'lsmgraph|run_|cargo'`。长任务需 ETA + 进度日志 + DONE/FAILED 标记。abort：单 policy 超预计 2.5×、MemAvailable < 80GiB、`/data` < 200GiB、30min 无进度、mismatch > 0。**SF30 可跑**；单 SF100 规约只约束更大规模。

---

## 10. 本阶段产出
- 本文件 `baseline/c2-merge-retention-design-20260618-cn.md`
- `paper/tables/table-c2-retention-schema.md`（表列定义）

**Stage 2 完成判据**：问题/指标/表列写死、判据明确、测量正确性规则与 naive 对照点已写入 → 待你签收后进入 Stage 3。
