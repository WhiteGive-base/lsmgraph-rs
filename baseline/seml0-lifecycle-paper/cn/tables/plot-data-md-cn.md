# Plot Data 中文版

这个文件告诉你应该画哪些图。

## Chart 1：W6 SF100 main results

画 candidate L0 / read bytes / latency p99 / RSS。重点展示 `naive` vs pruned variants。

关键数字：

- `naive` candidate L0 = 49,257,601，read bytes = 3461.6 MiB。
- `schema` candidate L0 = 5,929,197。
- `edge-type-only` candidate L0 = 5,899,015。
- `budg-b64` candidate L0 = 6,022,507。
- `semantic` read bytes = 642.7 MiB，但 RSS = 118.03 GiB outlier；它应写成 unbudgeted/full-materialization stress point，不是推荐配置。

## Chart 2：W6 budget sweep

画 budget 从 schema 到 full semantic 的 cost/benefit interpolation。

## Chart 3：W6 import RSS

展示 budgeted/schema RSS 与 naive 持平，full semantic 是 outlier。图注要说明：这个 outlier 是 SemL0 需要 budgeted lifecycle control 的证据。

## Chart 4：W9 steady-state time series

最强 dynamic latency evidence。SF30 mixed read/write 下：

- schema p99：1,270.6 → 8,740.2us。
- semantic p99：414.6 → 1,274.0us。

同时报告 cost：candidate L0 +86.3%，L0 files +93.4%。

## Chart 5：W8 property predicate

Property predicate 下 budg-b64/semantic 对 candidate/body/read/elapsed 有约 14-24% 改善。2-hop 不能写 universal candidate reduction。

## Chart 6：C2 lifecycle retention

画 grouped bars：retention 和 write_amp side by side；另画 real-SF30 metadata proxy。

重点：

- synthetic：read blow-up 4-6x vs flat。
- real SF30：retention 1.0 vs 0.0，write_amp 1.24 vs 1.07。
- real SF30 metadata proxy：naive weighted candidate-byte read-amp 6.52x，semantic 1.00x；avg candidate segments/query 93.6 vs 13.2。

## Chart 7：External baselines

展示 LiveGraph SF10 的 load/RSS/scan latency。诚实标注：external comparison SF10-only。
