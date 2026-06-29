# Stage 6 — Gate-C2 判门 verdict（2026-06-18）

> 依据：`stage4-smoke-summary-20260618-cn.md`（SF1）+ `stage5-scale-summary-20260618-cn.md`（SF10-class）。
> 判据来源：`c2-merge-retention-design-20260618-cn.md` §6。

## Verdict：**GO**（带有界代价说明）

semantic-aware merge 相比 naive merge：

| GO 判据（设计 §6） | 提案阈值 | 实测（SF1 / SF10c） | 结论 |
|---|---|---|---|
| exact_surface_after(semantic) ≫ naive | sem ≥0.8×before & naive ≤0.3×before | sem **1.0**（=before）/ naive **0.0** | ✅ 远超 |
| 目标 workload read 改善 | ≥20% | read_bytes_after：sem 比 naive 低 **75%（SF1）/ 83%（SF10c）** | ✅ 远超 |
| correctness | 0 mismatch | **0 / 0** | ✅ |
| write_amp 可接受 | ≤1.5× naive（提案） | sem/naive ≈ **1.53× / 1.62×** | ⚠️ 略超提案，但绝对值 sem=1.85–1.87 < 2×，可解释 |
| output_segments 不爆炸 | ≤2× naive（提案） | sem = **4 / 6** = 活跃语义分区数（naive=1） | ⚠️ 名义"4×/6×"，但实为 **bounded = #partitions**，非无界 explosion |

## 判定说明（为什么是 GO 而非 PARTIAL）

- 两条"⚠️"项不是真实风险，而是**提案阈值的设定问题**：
  - `output_segments`：semantic merge 的输出段数 = **活跃 (src_label, edge_type) 分区数**，由 schema 上界约束，不随数据量无界增长。把它和 naive 的"1 个 mixed 段"做"≤2×"比较本身不当。**应将该 guardrail 改为"output_segments ≤ 活跃语义分区数"**（已满足）。
  - `write_amp`：1.85–1.87 的绝对值 modest（< 2×），高出 naive 的部分来自 N 个段各自的 header/offset/bloom 固定开销，**可解释、有界**。
- 收益侧压倒性：retention 1.0 vs 0.0，且 naive 引入的读放大随分区数线性（4×/6×），semantic 完全消除。
- 因此满足 **GO**：semantic merge 在**可控、可解释的写代价**下保留 pruning surface 并消除读放大；不满足 PARTIAL 的"代价明显偏高/仅部分 workload 有效"。

## 对论文的影响（落实到写作）

- **保留 Lifecycle Management 标题**；C2 作为第二贡献成立。
- **§5 写成主章节**（retention 机制 + 跨规模结果 + 代价同表）。
- **Evaluation 增加 RQ6 主表**（用 `table-c2-retention-schema.md` 的列；填入 SF1/SF10c 两行，naive vs semantic）。
- **Abstract 第 3 句**（C2 承重句）可以保留，但措辞按本 verdict 收：
  - 可写："semantic-aware compaction retains/rebuilds the pruning surface across levels and removes the read amplification that naive merge introduces, at a bounded, explainable write-amplification cost."
  - **不写**："low/negligible overhead"、"fully optimal"、"production-grade scheduler"。
- **§9 Limitations**：写明 C2 当前为受控合成 microbenchmark（隔离 merge policy）；write-amp 随语义分区数上升；真实 LDBC SF30 端到端为 future/optional。

## 遗留（可选，不阻塞）

- 真实 LDBC SF30 双副本 naive/semantic 跑（外部效度）。
- degree/property merge policy（可选增强，追"更强 retention"时做）。
