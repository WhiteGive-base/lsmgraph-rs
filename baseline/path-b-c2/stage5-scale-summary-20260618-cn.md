# Stage 5 — C2 merge-retention 放大结果（2026-06-18）

> runner：`c2-merge-retention`（`src/bin/c2_merge_retention.rs`）
> 数据：合成、受控（controlled microbenchmark），单一源标签 Person，每个 edge_type 一个 exact L1 段。
> 原始：SF1-class `stage4-smoke-sf1-20260618.json`（300×4=1200 edges）；SF10-class `stage5-scale-sf10class-20260618.json`（40000×6=240k edges，bg `b6fcjwlu7`）。

## 跨规模对照（naive vs semantic L1→L2 merge）

| 指标 | SF1 naive | SF1 semantic | SF10c naive | SF10c semantic |
|---|---|---|---|---|
| exact_surface_ratio after | 0.000 | 1.000 | 0.000 | 1.000 |
| pruning_retention | 0.000 | 1.000 | 0.000 | 1.000 |
| output_segments | 1 | 4 | 1 | 6 |
| read_bytes before→after | 38.4k→**153.6k (4×)** | 38.4k→38.4k | 7.68M→**46.08M (6×)** | 7.68M→7.68M |
| write_amp (output/logical) | 1.22 | 1.87 | 1.14 | 1.85 |
| correctness_mismatches | 0 | 0 | 0 | 0 |

## 关键观察

1. **保留 vs 退化稳定**：semantic merge 在两个规模都 retention=1.0（保留/重建 exact surface）；naive merge 都 retention=0.0（塌成单一 MIXED 段）。
2. **读放大随分区数线性**：naive merge 后 read_bytes 放大 ≈ #edge_types（SF1=4 类→4×，SF10c=6 类→6×），因为 MIXED 段无法按 edge_type 剪枝，每个 typed 查询读整段；semantic merge 后 read_bytes 持平。
3. **代价有界、可解释**：semantic 的 output_segments = **活跃语义分区数**（4 / 6），不是无界 file explosion；write_amp 1.85–1.87（vs naive 1.14–1.22），高出部分来自 N 个段各自的 header/offset/bloom 开销。绝对值 < 2×。
4. **正确性**：所有规模、两 policy 的 before/after neighbor 结果一致，0 mismatch。

## 边界（claim 纪律）

- 这是**受控合成 microbenchmark**，刻意控制分区结构以隔离 merge policy 的影响；不是 LDBC 真实数据端到端。真实端到端读放大/内存等由既有 W6 SF100 覆盖。
- **真实 LDBC SF30** 为可选的额外外部效度步骤（需 import + 双副本 store 跑 naive/semantic），不阻塞 C2 gate。
- 不写 "low overhead"（write_amp 确有上升）；不写 "production-grade scheduler"。

## 结论

C2 retention 主张在 SF1→SF10-class 上**一致成立且强**：semantic merge 保留 pruning surface 并消除 naive merge 引入的读放大，代价为有界的段数（=分区数）与 modest write-amp 上升。→ 进入 Stage 6 判门。
