# Aster-Morris8 SF30 b64 对比结果

- 系统级结论：**NO-GO**
- Morris class 直接作为查询 metadata：**NO-GO**
- bench digest：checked=1800，mismatches=0
- neighbor compare：checked=1800，mismatches=0
- 未运行 b256/b1024；初轮未达到 GO，按计划停止。

## 实验配置

- Before：原始提交 `3d09ecb`，`--semantic-degree-estimator exact`。
- After：分支 `try_Aster`，`--semantic-degree-estimator morris8`；Morris 只控制 budget admission，持久化 metadata 仍为 exact/Mixed。
- 输入：`/data/WorkSpace/ldbc-sf30/social_network`；blocking I/O；`SNB_SKIP_ADJ_CACHE=1`；MemGraph=64 MiB；`B=64`。
- 查询：固定 `sf30-core-s200.plan.json`，9 个核心 edge types，1,800 个查询，预热 1 次，measured repeats=3。
- 执行顺序：Before import/bench -> After import/bench -> neighbor compare，全程串行。

## 核心指标

| 指标 | exact | Morris8 | 变化 |
|---|---:|---:|---:|
| peak import RSS GiB | 2.324 | 2.367 | +1.86% |
| import wall time s | 599.24 | 588.23 | -1.84% |
| import throughput edges/s | 1815380 | 1849359 | +1.87% |
| store bytes | 43769548665 | 43769413756 | -0.0003% |
| candidate L0 / round | 77919 | 77919 | +0.00% |
| read bytes / round | 8967528 | 8972040 | +0.05% |
| body reads / round | 11332 | 11332 | +0.00% |
| body bytes / round | 4696320 | 4696320 | +0.00% |
| mean avg latency us | 1083.6 | 1115.1 | +2.91% |
| mean p50 latency us | 1207.4 | 1211.1 | +0.31% |
| mean p99 latency us | 2801.9 | 2898.1 | +3.44% |
| L0 segments | 1143 | 1152 | +9 |
| exact-degree L0 segments | 63 | 72 | +9 |
| Mixed L0 segments | 1080 | 1080 | +0 |
| Mixed L0 bytes | 42223449960 | 42254824208 | +0.07% |
| degree-directory entries | 407307 | 397561 | -2.39% |

绝对回退量：candidate L0 `+0`/round，read bytes `+4512`/round，body reads `+0`/round，body bytes `+0`/round。

## Degree Class 边界

- tested sources=`4384723`，tested edges=`65011712`。
- boundary_misclass_rate=`1.080958%`；boundary_overestimate_rate=`0.004265%`；boundary_underestimate_rate=`1.076693%`。
- misclassified=`47397`；危险高估=`187`；保守低估=`47210`。
- 相对误差上界：p50=`0%`，p95=`20%`，p99=`50%`，max=`98.87%`。

### Class Confusion Matrix

行是 exact class，列是 Morris estimated class。

| exact \ estimated | Low | Medium | High |
|---|---:|---:|---:|
| Low | 3515989 | 0 | 0 |
| Medium | 47154 | 821180 | 187 |
| High | 0 | 56 | 157 |

危险高估为 Low->Medium/High 与 Medium->High；本次只有 Medium->High=`187`。低估共 `47,210`，会造成更弱物化或更多读取，但不会直接造成 false skip。

### Boundary Windows

| 窗口 | tested | misclassified | misclass rate | overestimated | overestimate rate | underestimated | underestimate rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| low_medium_14_18 | 285929 | 37011 | 12.944122% | 0 | 0.000000% | 37011 | 12.944122% |
| medium_high_922_1126 | 421 | 128 | 30.403800% | 91 | 21.615202% | 37 | 8.788599% |

| 精确点 | tested | misclassified | misclass rate | overestimated | underestimated |
|---|---:|---:|---:|---:|---:|
| degree_15 | 62209 | 0 | 0.000000% | 0 | 0 |
| degree_16 | 56613 | 0 | 0.000000% | 0 | 0 |
| degree_17 | 51225 | 25576 | 49.928746% | 0 | 25576 |
| degree_1023 | 0 | 0 | 0.000000% | 0 | 0 |
| degree_1024 | 0 | 0 | 0.000000% | 0 | 0 |
| degree_1025 | 0 | 0 | 0.000000% | 0 | 0 |

SF30 样本在 exact degree 1023/1024/1025 三个点均为 0；该边界的结论来自 922-1126 窗口，而非这三个离散点。

## Budget Materialization Skip

- exact_keep_sources=`865784`；Morris keep sources=`818761`。
- exact_keep && morris_skip：sources=`47079`，bytes=`32262048`。
- materialization_false_skip_rate：sources=`5.437730%`，bytes=`2.127473%`。
- exact_skip && morris_keep（false include）：sources=`56`，bytes=`2016352`，false_include_rate=`0.001277%`。
- 安全实现回退到 Mixed：sources=`3565962` (`81.326962%`)，bytes=`594171040` (`28.560769%`)。

这里的 materialization false skip 表示 Morris 没有物化 exact 本会物化的 degree partition，不是查询结果漏读；实际 metadata 回退为 Mixed。

## Query False Skip

- shadow audited query executions=`9000`；unique sampled queries=`1800`。
- raw：queries=`0` / `9000` (`0.000000%`)；segments=`0` / `3765` (`0.000000%`)；edge records=`0` / `155405` (`0.000000%`)。
- safe：queries=`0`，segments=`0`，edge records=`0`，rate=`0.000000%`。
- result digest：bench mismatches=`0`；neighbor compare mismatches=`0`。

直接使用 Morris class 仍判定 **NO-GO**：固定查询 shadow 未实际命中漏读，但全量 degree-class shadow 出现 `187` 个危险高估，因此不能把 Morris class 升格为 correctness-critical metadata。

## Morris Counter Update Skip

- attempted=`65011712`，applied=`41560542`，skipped=`23451170`，write avoidance=`36.072223%`。
- saturation_count=`0`（SF30 要求为 0）。

| E | attempted | applied | skipped | observed skip | expected skip | probability check |
|---:|---:|---:|---:|---:|---:|---|
| 0 | 31167615 | 31167615 | 0 | 0.000000% | 0.000000% | pass |
| 1 | 14262180 | 7132643 | 7129537 | 49.989111% | 50.000000% | pass |
| 2 | 8966571 | 2239429 | 6727142 | 75.024689% | 75.000000% | pass |
| 3 | 6379610 | 796129 | 5583481 | 87.520726% | 87.500000% | pass |
| 4 | 3011704 | 187740 | 2823964 | 93.766320% | 93.750000% | pass |
| 5 | 1141215 | 35671 | 1105544 | 96.874296% | 96.875000% | pass |
| 6 | 82041 | 1309 | 80732 | 98.404456% | 98.437500% | pass |
| 7 | 776 | 6 | 770 | 99.226804% | 99.218750% | n/a |
| 8 | 0 | 0 | 0 | n/a | 99.609375% | n/a |
| 9 | 0 | 0 | 0 | n/a | 99.804688% | n/a |
| 10 | 0 | 0 | 0 | n/a | 99.902344% | n/a |
| 11 | 0 | 0 | 0 | n/a | 99.951172% | n/a |
| 12 | 0 | 0 | 0 | n/a | 99.975586% | n/a |
| 13 | 0 | 0 | 0 | n/a | 99.987793% | n/a |
| 14 | 0 | 0 | 0 | n/a | 99.993896% | n/a |
| 15 | 0 | 0 | 0 | n/a | 99.996948% | n/a |

## 内存解释

- 当前 exact 路径的 flush exact-degree table=`0` bytes；degree 来自已排序 `(src, edge_type)` run-length，并没有一个可由 Morris8 替换的大型 exact-degree HashMap。
- 现有 degree-directory value 本来就是 `1` byte：Before entries=`407307`、u8 payload=`407307` bytes；After entries=`397561`、u8 payload=`397561` bytes。
- degree-directory 的主要下界来自 `(VertexId, EdgeType)` key 和 bucket capacity：Before bucket payload lower bound=`11010048` bytes，sidecar=`5295519` bytes。`hypothetical_exact_u64_payload_bytes`=`3258456` 只是反事实，不是当前 baseline 的实际占用。
- Morris import 的 peak live estimates=`120968`，1-byte payload=`120968` bytes；它是新增临时状态，不是替换 baseline 的 u64 table。

因此 Aster 的“8-bit degree counter”优势在当前 import 数据路径上没有对应的 64-bit baseline 对象可替换；本次结果不能支持其降低 2.2-2.5 GiB 总 RSS 的假设。峰值反而增加 1.86%，且只做了门槛规定的首轮 A/B，不把该差异解释为稳定回退幅度。

## Gate

- [ ] rss_reduction_at_least_3pct
- [ ] materialization_false_skip_at_most_5pct
- [x] candidate_l0_regression_at_most_5pct
- [x] read_bytes_regression_at_most_5pct
- [x] throughput_regression_at_most_10pct
- [x] avg_latency_regression_at_most_10pct
- [x] p50_latency_regression_at_most_10pct
- [x] digest_mismatches_zero
- [x] safe_false_skip_zero
- [x] saturation_zero

注：raw false-skip 是反事实 shadow 指标；实际路径继续写入 exact/Mixed metadata，安全性由 safe 指标和 digest 共同验证。
本轮因 RSS 与 materialization false-skip 两项失败而停止；未进行反向顺序复测，也未运行 b256/b1024。
