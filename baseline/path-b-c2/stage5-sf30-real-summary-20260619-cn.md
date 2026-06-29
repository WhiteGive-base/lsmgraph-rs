# Stage 5 — 真实 LDBC SF30 retention：DONE / 确认 C2 在真实数据成立

> run: `run_c2_sf30_real_20260618.sh`（v2，build-l1-from-l0），日志 `remote-logs/c2-sf30-real-20260619/`（DONE）。
> 方法：导入 SF30（semantic L0，**不** compact，保留多个 live L0）→ 拷贝 ×2 → 每份 drain L0 的 (src_label, edge_type) 分区为 exact L1 → `compact_levels_with_policy` L1→L2 naive vs semantic。

## 数据
- 真实 SF30 = **1,087,848,423 条有向边**；drain 出 **40** 个 (src_label, edge_type) 分区 → **528 个 exact L1 段**（两 policy 同输入）。

| policy | exact_surface before→after | pruning_retention | mixed_after | input_segs | output_segs | output_bytes | logical_bytes | write_amp |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1.000 → **0.000** | **0.000** | 1.000 | 528 | 503 | 37.20 GB | 32.4 GB | **1.07** |
| semantic | 1.000 → **1.000** | **1.000** | 0.000 | 528 | 528 | 43.25 GB | 32.4 GB | **1.24** |

## 解读
- **C2 主张在真实 1.09B 边数据上成立**：semantic merge 保留 pruning surface（retention 1.0），naive 摧毁它（0.0）→ 与合成 SF1/SF10c、真实 SF1（1.0 vs 0.0）完全一致。
- **代价有界**：semantic write_amp 1.24 vs naive 1.07（仅高 ~16%，<2×），来自 528 段各自的 header/offset/bloom 开销。
- **印证"质不是量"**：naive 在 SF30 没有更少段（503 vs 528），但全是 MIXED（exact_surface=0），写更少字节却毁掉剪枝面。
- **边界**：本次为 open-mode（只测 surface + 写代价）；read-amp 后果由合成 SF1/SF10c（4–6×）展示，merge 无损性由 engine 测试套件覆盖。

## 影响（对论文/SIGMOD）
- **填掉"C2 只是合成 microbenchmark"这条 SIGMOD 缺口的一半**：retention 现在有真实大规模数据背书。
- 已折入：`paper/lifecycle/tables/table-c2-retention.md`（+SF30 行）、`plot-data-md.md`（Chart 6）、`§5/§7/§9`（"in progress/future work"→"confirmed"）。

## 资源/清理
- 已删两份拷贝；保留 base `store/c2-sf30-base`（no-compact，含 live L0，可复跑）。
