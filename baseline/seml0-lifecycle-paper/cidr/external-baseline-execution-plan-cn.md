# 外部 Baseline 执行计划

> 2026-06-30 执行状态：LiveGraph、Aster RocksGraph、Neo4j Community、TuGraph、NebulaGraph 均已完成 SF1/SF10 measured baseline 并通过 count/hash digest gate。LSMGraph-style 保持为 internal layout row；Teseo/GraphOne/LLAMA/Aspen 继续作为 qualitative/design comparison，不硬塞进数值表。最新可引用结论见 `external-baseline-status-20260627-cn.md`、`../../external-baselines-20260626/3plus3-baselines/progress-table.md` 和 `../../external-baselines-20260626/3plus3-baselines/effect-table.md`。

## 目标

补一个可复现的外部 baseline，如果做不到，就生成足够清楚的 artifact attempt 记录，支撑 CIDR 版的 qualitative comparison。

## Reproducibility gate

一个外部系统只有满足以下条件，才能进主实验表：

- public source 或可归档 artifact。
- 能在远端机器 clean build。
- 能加载同一 LDBC SNB edge set，或有明确 converter。
- 跑同一 sampled typed-neighbor query workload。
- 有 correctness count / digest check。
- 记录 load time、RSS、disk footprint、query avg/p50/p90/p99。
- 有 raw logs、run config、commit/version、DONE marker。

不满足 gate 的系统只能进入 qualitative comparison。

## 优先级

### P0：LiveGraph

理由：

- 最接近 SemL0 的 read path 对比。
- 目标是 adjacency scan 与 transactional graph storage。
- 可定义 typed-neighbor scan workload。

执行步骤：

1. 确认 `deps/LiveGraph` 当前源码、commit、build 状态。
2. 若缺 TBB，记录依赖；如允许安装，再安装 `libtbb-dev`。
3. 编译 driver。
4. 将 LDBC SNB SF1 转成 LiveGraph loader 输入。
5. 跑 SF1 smoke：load、scan、count correctness。
6. 通过后跑 SF10 main：load time、RSS、disk、scan latency。
7. 生成 `baseline/external-baselines-YYYYMMDD/livegraph/` 结果包。

通过标准：

- SF1 smoke 正确。
- SF10 main 有 DONE marker。
- 至少 3 个 core edge types 的 positive typed-neighbor scans。
- query count 与 ground truth 对齐。

失败时保留：

- build log。
- missing dependency。
- driver gap。
- why not apples-to-apples。

### P1：Teseo 或 GraphOne

选择规则：

- 如果 Teseo build 快速修复，优先 Teseo。
- 如果 GraphOne TBB 修复更容易，优先 GraphOne。
- 只需要一个系统进入 P1 main attempt。

执行步骤：

1. 安装或记录缺失依赖。
2. 跑官方 demo / benchmark smoke。
3. 评估能否加载 LDBC edge list。
4. 若无法表达 typed-neighbor scan，则停止，不硬改 workload。
5. 生成 artifact attempt report。

通过标准：

- clean build。
- 能加载同一 edge set 或可解释的 converted edge list。
- 能跑可比 read/scan 或 update workload。

### P2：LLAMA

用途：

- Artifact sanity 或 analytics comparison。
- 不作为主外部 baseline。

执行步骤：

1. 转换 LDBC SF1/SF10 edge list 为 SNAP。
2. 跑 BFS/PageRank smoke。
3. 记录与 SemL0 workload 不匹配原因。

## 输出文件

每个外部系统生成：

- `artifact-report.md`
- `build.log`
- `run-config.json`
- `metrics.tsv`
- `correctness.tsv`
- `DONE` 或 `FAILED`

汇总生成：

- `external-baseline-status.md`
- `external-baseline-comparison-table.md`

## 主文使用规则

- 只有通过 reproducibility gate 的系统才能进入 numeric table。
- 未通过的系统进入 qualitative design matrix。
- 若只有少量系统通过，CIDR 版可接受，但必须明确 external comparison is partial。
- 若没有任何系统通过，CIDR 版仍可写，但必须把 “why direct comparison is hard” 作为独立段落，并附 artifact attempts。
