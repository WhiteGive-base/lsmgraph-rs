# SemL0 CIDR 实验现状与执行结论

> **历史快照**：本文记录 2026-07-21 的审计与执行判断，保留用于 provenance 回溯，不再作为当前状态真源。2026-08-05 的封板代码索引和验证入口见 [`FINAL-HANDOFF-CN.md`](FINAL-HANDOFF-CN.md)。

更新时间：2026-07-21

## 结论

现有材料足够生成六组**带边界的 provisional 诊断图**，但不足以直接冻结六组正式论文图。20 个证据来源按论文 claim 审计后的分布为：

- `REUSE = 5`：可在限定口径下直接复用；
- `FIX = 6`：原始数据可用，但需修 parser、provenance 或表述；
- `RERUN = 9`：正式 claim 必须重跑。

当前已经完成三项确定性修复：

1. W6 SF10 的 `/usr/bin/time -v` elapsed 不再把 `3:19.49` 截成 `19.49`，正确值为 `199.49 s`；九个变体均从原始 stderr 重建。
2. RQ3 三个 arm 均有 `done` event 和目录 `DONE`，已新增非覆盖式重新解析汇总；旧 `partial-running` 是 summarizer 执行时机问题。
3. W13 规范化数据改指向实际的 `w13-schema-evolution-20260615-143649`，10/10 有界测试通过；旧 `20260614-0004` 只保留为历史来源。

## 六组图的当前判定

| 图组 | 当前可用材料 | 当前最高等级 | 正式缺口 |
|---|---|---|---|
| Fig.1 end-to-end | W6 + 旧外部系统历史结果 | context only | 查询列表、接口、API 边界、硬件和 repeats 不一致；全部重跑 |
| Fig.2 component ablation | W6 SF10/SF100 layout matrix | layout diagnostic | 不是 A0–A6 单开关阶梯；需实现正交开关后重跑 |
| Fig.3 resource tradeoff | W6 import wall/CPU/RSS/disk/L0 | import-only | 缺 steady PSS、CPU/op、磁盘分项、mixed/compaction telemetry |
| Fig.4 dynamic compaction | C2 三层证据 + RQ3 + W9 | mechanism/proxy | RQ3 n=1 且不同 RNG；W9 compaction=0；固定 trace 后重跑 |
| Fig.5 workload coverage | W8、W7、W13 | coverage sanity | W8 无 digest，只覆盖 edge type 1；完整 selectivity×degree×RW 矩阵缺失 |
| Fig.6 scalability | W6 SF10→SF100 | two-point diagnostic | 采样数不同、版本/硬件未绑定；SF1/10/30/100 与并发曲线重跑 |

## 为什么当前没有启动正式 timing 重跑

远端当前为 128-vCPU Xeon 6982P-C、NVMe `/data`；旧外部实验明确来自 64-vCPU Xeon 8163、`/dev/vdb1`。审计时另一个用户的长期数据生成任务持续占用约 60 GiB RSS，并有多 worker。虽然 load average 不高，但 page cache、内存带宽和 I/O 干扰无法排除。

因此现在启动 latency/QPS、CPU/RSS、load、compaction 或 concurrency 正式实验，只会再产出一套无法进入论文的噪声数据。当前窗口只执行了不依赖 timing 的 parser、hash、manifest、数据规范化与图表渲染；正式重跑顺序和耗时/磁盘预算已写入 [`work/remote-audit/RERUN-CANDIDATES.tsv`](work/remote-audit/RERUN-CANDIDATES.tsv)。服务器释放后从 P10/P20/P31/P40/P60 顺序开始。

## 已生成的六组 provisional 图

每组均由 Python/Matplotlib 从规范化 TSV 读取，未在脚本中手抄实验数值；输出同时包含 PNG、SVG、PDF 和 layout JSON。

| 图 | 用途 | 不能用于的 claim |
|---|---|---|
| `fig1_context_not_matched` | 显示旧系统数据为什么不可直接横比 | 端到端系统排名 |
| `fig2_layout_ablation_provisional` | W6 layout/budget 诊断 | A0–A6 因果消融 |
| `fig3_import_resource_provisional` | import wall/CPU/RSS/disk/L0 | steady-state query resource Pareto |
| `fig4_compaction_timeline_provisional` | RQ3 n=1 机制时间线 | 有 CI 的固定 trace compaction 结论 |
| `fig5_workload_coverage_provisional` | W8 property/two-hop 局部覆盖 | 完整 workload generality |
| `fig6_two_point_scale_diagnostic` | SF10→SF100 两点趋势 | scaling curve 或复杂度斜率 |

图目录：[`figures/output/provisional/`](figures/output/provisional/)；视觉验收见 [`validation/VISUAL-QA-CN.md`](validation/VISUAL-QA-CN.md)。这些图用于决定补跑重点，不能直接替换正式论文图。

## 正式图的数据门槛

- 所有正式点先做 3 个独立进程 run；QPS CV >3% 或 P99 CV >5% 时补到 5 个；长时 import/dynamic/compaction 至少 3 个独立 run。
- 同一比较必须绑定同一 input/query-trace SHA、cache state、concurrency 和 host fingerprint；Git SHA、binary SHA-256、system version 必须在各底层 engine 的重复运行内固定，SemL0 各配置还必须使用同一构建。
- 性能点必须同时满足 `digest_pass=true` 与 `mismatch_count=0`。
- W9 no-compaction、C2 metadata proxy、W8 prototype equality、unsupported direction 均不得被自动提升为完整系统性能结果。
- 正式绘图脚本缺字段、缺 digest、缺 repeats 或 protocol 不 matched 时必须失败，而不是画空值或占位值。

六个正式 Python 绘图入口已放在 [`figures/scripts/formal/`](figures/scripts/formal/)，并统一读取冻结契约 [`plan/FIGURE-DATA-REQUIREMENTS.tsv`](plan/FIGURE-DATA-REQUIREMENTS.tsv)。编译和 `--help` 全部通过；将六份旧 candidate TSV 分别送入后，六个入口均以退出码 2 拒绝且不生成 formal 图，验收记录见 [`validation/FORMAL-PLOT-GATES-CN.md`](validation/FORMAL-PLOT-GATES-CN.md)。

数据入口已收口为 [`data/source-registry.tsv`](data/source-registry.tsv)（20 个来源）和 [`data/normalized/candidates/`](data/normalized/candidates/)（1,082 条逐 metric 候选）；它们保留证据与缺口，但不会被自动升级为正式结果。
