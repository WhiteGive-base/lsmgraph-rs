# Aster-Morris8 SF30 b64 实验总结

## 实验范围

- Before：提交 `3d09ecb`，使用 exact degree admission。
- After：分支 `try_Aster`，使用 Aster 风格的 Morris8 counter 做 budget admission。
- 数据集：LDBC SNB SF30。
- 配置：64 MiB MemGraph、`B=64`、blocking I/O、`SNB_SKIP_ADJ_CACHE=1`。
- 查询：固定 1,800 个 sampled queries，预热后执行 3 次 measured repeats。
- 安全策略：Morris8 只决定是否物化 degree partition；最终 metadata 仍使用精确 run-length class，拒绝物化时写为 `Mixed`。

## 核心对比

| 指标 | Exact | Morris8 | 变化 |
|---|---:|---:|---:|
| Peak import RSS | 2.324 GiB | 2.367 GiB | +1.86% |
| Import wall time | 599.24 s | 588.23 s | -1.84% |
| Import throughput | 1,815,380 edges/s | 1,849,359 edges/s | +1.87% |
| Store size | 43,769,548,665 B | 43,769,413,756 B | -0.0003% |
| Candidate L0 / round | 77,919 | 77,919 | 0.00% |
| Read bytes / round | 8,967,528 | 8,972,040 | +0.05% |
| Mean latency | 1,083.6 us | 1,115.1 us | +2.91% |
| P50 latency | 1,207.4 us | 1,211.1 us | +0.31% |
| P99 latency | 2,801.9 us | 2,898.1 us | +3.44% |

其他结果：

- Morris counter write avoidance：36.0722%。
- Degree class misclassification：1.080958%。
- 危险高估：187 个 source，比例 0.004265%。
- Materialization false skip：source 口径 5.4377%，byte 口径 2.1275%。
- Raw sampled query/segment/edge false skip：全部为 0。
- Safe query/segment/edge false skip：全部为 0。
- 两组 1,800-query digest comparison：`mismatches=0`。
- Morris8 saturation count：0。

## Run-length Degree 说明

当前导入路径先按 `(source_label, edge_type, src, dst, ts)` 对边排序。排序后，同一个 `(src, edge_type)` 的边会连续出现，因此只需要顺序扫描并统计这段连续区间的长度：

```text
(src=10, type=1) -> 5 条连续边 -> exact degree = 5
(src=11, type=1) -> 20 条连续边 -> exact degree = 20
```

这个连续区间长度就是 run length，也是当前 flush 中该 `(src, edge_type)` 的精确 degree。随后代码按 `16` 和 `1024` 两个边界将其映射为 Low、Medium 或 High class。

这种方式不需要维护 `HashMap<(src, edge_type), u64>`。当前 degree directory 保存的 class value 本身也已经是 1 byte，所以 Morris8 没有可以替换的大型 64-bit degree table。导入峰值内存主要来自边数组、排序、partition buffer 和 CSR segment 构建，而这些对象不会因为 Morris8 counter 变小。

为了保证查询正确性，实验实现仍然使用 run-length 得到最终精确 class。Morris8 只参与 budget admission；如果 Morris8 拒绝物化，就回退到 `Mixed` 并保守扫描，因此实际 false skip 和 digest mismatch 都为 0。

## 结论

本轮为 **NO-GO**。Morris8 没有使 peak import RSS 下降 3%，实际增加了 1.86%；materialization false skip 也略高于 5% 门槛。根本原因是当前实现已经通过 run-length 避免了大型精确 degree table，Aster 的 8-bit counter 压缩优势在这里没有对应的内存对象可优化。因此没有继续运行 b256/b1024。
