# Stage 0 — 仓库状态校准（2026-06-18）

> 目的：确认后续 C2 操作的是正确分支、commit、API 命名，避免在错分支/旧 API 上做实验。

## 事实

| 项 | 值 |
|---|---|
| 分支 | `codex/sf100-basegraph-bench` |
| HEAD | `c2aa16c` (`Rename-K4-kernel-APIs-to-LSM-maintenance`) |
| `src/`+`tests/` 中 `K4` 出现次数 | **0**（已全部改成中性命名） |

## API 命名（中性，K4 已移除）

| 旧（论文代号，不在代码） | 现行内核 API | 位置 |
|---|---|---|
| K4MergePolicy | `LevelMergePolicy{fanout,min_input_segments,max_output_level,semantic_partition_outputs}` | `src/graph.rs:674` |
| K4MaintenanceReport / LifecycleReport | `MaintenanceReport` / `LifecycleReport` | `src/graph.rs` |
| run_k4_* | `run_maintenance` / `run_lifecycle*` / `compact_levels*` | `src/graph.rs` |
| split_k4_semantic_compaction_segments | `split_semantic_compaction_segments` | `src/graph.rs:4114` |

## C2 关键入口（已核）

- 两 merge policy 切换：`compact_level_to_next`（`src/graph.rs:3306`），`if policy.semantic_partition_outputs { split_semantic_compaction_segments } else { split_property_compaction_segments }`（`graph.rs:3364`）。
- per-merge 代价：返回 `LevelCompactionDecision{input_segments,output_segments,input_bytes,output_bytes,semantic_partition_outputs}`（`graph.rs:3414`）。
- 全局代价计数器：`metrics.compaction_count/_input_bytes/_output_bytes`、`storage_compaction_latency`（`src/metrics.rs:58-60,104`）。
- 段语义字段：`CsrSegmentMeta`（`src/csr/format.rs:108`），`SemanticSummaryCompleteness{Exact,Conservative,Unknown}`（`src/schema.rs:576`）。

## 结论

`K4 = 论文代号` 与 `内核中性命名` 一致；当前 outline / status 文档与代码对齐。**允许继续 C2**，基于上述中性 API。Stage 0 = ✅。

## 工作树备注

仓库有大量未跟踪的 `baseline/`、`paper/`、`SEML0-K4-STATUS-AND-PLAN-*.md` 等文档（本 Path B 工作产物），以及若干已跟踪文件的本地修改（W14/runner/doc 类）。这些属预期的工作产物，不影响 C2 内核改动；Stage 3 改 `src/` 前会先跑 correctness gate 作为基线。
