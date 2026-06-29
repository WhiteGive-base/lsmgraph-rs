# Table C2 — Lifecycle Retention 主表列定义（schema）

> 配套设计：`baseline/c2-merge-retention-design-20260618-cn.md`。
> 用途：固定 C2 主表的列、类型、来源，保证实验输出 JSON/TSV 与论文表一一对应、可由脚本再生。
> 每行 = 一个 (dataset, workload, policy) 组合（正式跑取 ≥3 repeats 的 mean±stddev）。

## 维度列（行键）

| column | type | 取值 / 来源 |
|---|---|---|
| `dataset` | str | SF1 / SF10 / SF30 |
| `workload` | str | edge-selective / mixed /（可选）degree-selective / property-selective |
| `policy` | str | naive_merge / semantic_edge_merge /（可选）semantic_degree_merge / semantic_property_merge |
| `input_level` | int | merge 源层（如 L1） |
| `output_level` | int | merge 目标层（如 L2） |

## 收益列（pruning surface + workload）

| column | type | 定义 / 来源 |
|---|---|---|
| `exact_surface_ratio_before` | f64 | Σ edges(topology_exact)/Σ edges，**在参与 merge 的输入段集合上**（两 policy 共用） |
| `exact_surface_ratio_after` | f64 | 同上，但在该 policy 的 output 段集合上 |
| `mixed_ratio_before` | f64 | Σ edges(topology_mixed)/Σ edges（输入段） |
| `mixed_ratio_after` | f64 | 同上（output 段，per policy） |
| `pruning_retention` | f64 | `exact_surface_ratio_after / exact_surface_ratio_before`（≥1 = 保留/重建） |
| `exact_surface_seg_before/after` | int | 段计数版本（辅助） |
| `candidate_before` | f64 | merge 前布局上该 workload 的 candidate segments（漏斗计数） |
| `candidate_after` | f64 | merge 后布局上同一 workload 的 candidate segments |
| `read_bytes_before/after` | u64 | header+offset+body read bytes（merge 前/后布局） |
| `read_amp_proxy.kind` | str | real-SF30 open-mode 中的 proxy 类型；当前为 `metadata_typed_neighbor_partition_replay` |
| `read_amp_proxy.weighted_read_amp_vs_exact_bytes` | f64 | Σ candidate bytes after / Σ exact partition bytes before；metadata replay，不含 body decode |
| `read_amp_proxy.avg_candidate_segments_per_query` | f64 | 每个 typed-neighbor partition 的平均 post-merge candidate segment 数 |
| `read_amp_proxy.candidate_bytes_total` | u64 | 40 个 typed partitions replay 后的 candidate bytes 总和 |

## 代价列（write/compaction）

| column | type | 定义 / 来源 |
|---|---|---|
| `input_segments` | int | `LevelCompactionDecision.input_segments`（已存在） |
| `output_segments` | int | `LevelCompactionDecision.output_segments`（已存在） |
| `input_bytes` | u64 | `LevelCompactionDecision.input_bytes`（已存在） |
| `output_bytes` | u64 | `LevelCompactionDecision.output_bytes`（已存在） |
| `rewrite_bytes` | u64 | = output_bytes（level merge 全重写） |
| `logical_update_bytes` | u64 | 该次 merge 覆盖的逻辑边数据大小（**Stage 3 新增计数器**） |
| `write_amp` | f64 | `rewrite_bytes / logical_update_bytes` |
| `compaction_latency_ms` | f64 | `storage_compaction_latency`（已存在） |

## 正确性 / 判定列

| column | type | 取值 |
|---|---|---|
| `correctness_mismatches` | int | 与 naive anchor 的 digest compare，必须 = 0 |
| `verdict` | str | GO / PARTIAL / FALLBACK（按设计 §6） |

## 主对照口径（提醒）
- **headline = `exact_surface_ratio` / `pruning_retention`（边加权）**，不是 `output_segments`。
- naive 的退化体现在 surface ratio 低 + mixed 段；semantic 体现在保留（甚至 after>before 的重建）。
- 收益列与代价列必须**同表呈现**，禁止只报收益不报 write_amp / output_segments。
