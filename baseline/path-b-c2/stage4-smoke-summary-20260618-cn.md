# Stage 4 — C2 merge-retention smoke 总结（2026-06-18）

> runner：`src/bin/c2_merge_retention.rs`（bin `c2-merge-retention`）
> 命令：`cargo run --bin c2-merge-retention -- --sources 300 --store-dir target/c2-smoke --output baseline/path-b-c2/stage4-smoke-sf1-20260618.json`
> 原始结果：`baseline/path-b-c2/stage4-smoke-sf1-20260618.json`
> 规模：SF1-class 合成数据（300 sources × 4 edge types = 1200 edges，全 Person 源标签，4 个单一 edge_type 的 exact L1 段）

## 结果（naive vs semantic L1→L2 merge）

| 指标 | naive_merge | semantic_merge |
|---|---|---|
| exact_surface_ratio before | 1.000 | 1.000 |
| exact_surface_ratio **after** | **0.000** | **1.000** |
| mixed_ratio after | 1.000 | 0.000 |
| **pruning_retention** | **0.000** | **1.000** |
| input_segments | 4 | 4 |
| output_segments | **1**（塌成 mixed） | **4**（保留分区） |
| input_bytes | 71,808 | 71,808 |
| output_bytes / rewrite_bytes | 46,752 | 71,808 |
| logical_update_bytes | 38,400 | 38,400 |
| **write_amp** | **1.22** | **1.87** |
| read_bytes before | 38,400 | 38,400 |
| **read_bytes after** | **153,600（4× 放大）** | **38,400（持平）** |
| correctness_mismatches | **0** | **0** |

## 解读

- **核心 C2 主张成立（smoke 级）**：semantic merge 跨层**保留** pruning surface（retention 1.0），naive merge **退化**它（retention 0.0，输出塌成单个 MIXED 段）。
- **读放大后果可见**：naive merge 后，同一 typed-neighbor workload 的 read_bytes 从 38,400 涨到 153,600（4×）——因为 MIXED 段无法按 edge_type 剪枝，每次 typed 查询都要读整段；semantic merge 后 read_bytes 持平。
- **代价同表呈现**（不藏）：semantic 的 write_amp 1.87 > naive 1.22，output_segments 4 > 1。这正是设计 §6 要求的"收益与代价同表"。
- **正确性**：两 policy 的 before/after neighbor 结果完全一致，0 mismatch。

## 结论

Stage 4 完成判据"runner + 表链路跑通、0 mismatch"**达成**，且已给出 GO 方向的强信号（retention 1.0 vs 0.0 + 读放大 4×，代价有界）。链路（surface 测量 → 表 → 正确性）端到端验证通过。

下一步 Stage 5：在更大规模 + 混合源标签 + 多 repeats 上跑正式结果，判 Gate-C2。是否接入真实 LDBC SF30 数据（vs 放大合成）见 Stage 5 决策。
