# Stage 5 — 真实 LDBC SF30 retention run：FAILED（已诊断）

> run: `baseline/path-b-c2/run_c2_sf30_real_20260618.sh`，日志 `remote-logs/c2-sf30-real-20260618/`
> 结论：导入成功，**merge 阶段失败**——属于"实验设计 vs 引擎布局"不匹配，**非代码 bug**，**不影响 C2 gate=GO**（合成证据已决定性）。

## 发生了什么
- ✅ release 编译（25.86s）。
- ✅ SF30 导入成功：**1,087,848,423 条有向边**（608,041,914 input rows，322,396,940 src 组），base store `store/c2-sf30-base` = **147G**，总 import ~3532s + FullCompact 666s。
- ❌ merge：`run_open` 调 `compact_levels_with_policy`（L1→L2，fanout=2，min_input_segments=2）返回空 → `Error: no L1->L2 compaction triggered (need >=2 L1 segments)`。

## 根因
`--compact-after-import` 触发 `engine.compact_l0_to_l1()`，把全部 L0 **合并成单个 L1 段**。base store live 布局 = **L0:0, L1:1, L2:0**（磁盘上 857 个 .edge 是已 DeleteFile 但未 GC 的残留）。

`compact_levels_with_policy` 只处理 source level ∈ [L1, max_output_level) 且要求 `source_count >= min_input_segments && source_count > fanout`。L1 只有 1 段 → 跳过 → 无 L1→L2 可比。**所有数据塌进 1 个段，没有东西可 merge。**

## 这说明什么（对 C2 claim 是中性/正面）
- C2 是 **compaction policy 的性质**，最干净的证明方式就是**受控实验**（固定分区结构，只变 merge policy）——这正是合成 SF1/SF10c 做的，且 Gate=GO。真实数据端到端由 W6 SF100 覆盖。
- 真实 SF30 retention 是"额外外部效度"，**可选、不阻塞**，且本次撞到的是引擎布局现实（bulk import + FullCompact 产出单段），不是 C2 机制问题。

## 正确的重试路径（如要做真实 SF30）
要让 L1→L2 naive-vs-semantic 可比，需要一个**多 L1 段**状态：
1. 导入时**不要** `--compact-after-import`（保留多个 live L0 段；`--l0-layout semantic` 让 L0 按分区切）。
2. 新增 runner 模式：打开 store，枚举 live L0 的 (src_label, edge_type) 分区，对每个分区调 `compact_l0_partition_to_l1` 建一个 exact L1 段（= 合成实验的真实数据版），再 `compact_levels_with_policy` L1→L2 naive vs semantic。
3. 成本：另一次 ~1h 导入 + 新 runner 模式代码。收益：真实数据外部效度（gate 已 GO，非必需）。

## 现状/资源
- 保留 `store/c2-sf30-base`（147G）备用；/data 492G free（已删两份失败拷贝）。
- 若不重试：删 `store/c2-sf30-base` 可回收 147G。
