# 外部 Baseline 调研与执行结论

更新日期：2026-06-27

本文件给 CIDR draft 使用。原则是：只有通过同一 workload 的可复现 correctness gate，才进入数值表；其他系统只写 design comparison、artifact attempt 或 related work。

## Reproducibility Gate

外部系统进入主数值表必须同时满足：

- public source、可归档 artifact，或明确可复现 Docker/image 版本。
- 能加载同一 LDBC dense edge set。
- 能执行同一 fixed-seed sampled typed-neighbor workload。
- 每个 sampled query 有 count/hash digest correctness。
- 记录 load time、RSS、disk footprint、avg/p50/p90/p99 latency。
- 保留 raw logs、run config、版本、DONE/result marker。

## 已完成数值 Baseline

### LiveGraph

定位：scope-limited dynamic graph storage baseline。

结果：

- SF1：34,692,699 edges，load 21.155 s，peak RSS 4,850,512 KB，disk 5,368,709,120 bytes。
- SF10：355,185,382 edges，load 1,457.36 s，peak RSS 47,908,544 KB，disk 38,654,705,664 bytes。
- SF10 all-types avg 391.063 us；positive-only avg 2.089 us；negative/high-fanout avg 780.036 us。
- Digest：SF1 checked 16,140 / mismatches 0；SF10 checked 32,140 / mismatches 0。

写法边界：可以写 typed-neighbor workload 下的 scope-limited measured external baseline；不要写成完整图数据库 head-to-head。

### Neo4j Community

定位：主流 property-graph database baseline。

导入模型：所有 SemL0 dense edge types 都作为 Neo4j 中独立 outgoing relationship types 导入，例如 `E_Px` / `E_Nx`。这样避免把负向 edge type 混成普通 incoming traversal。

结果：

- SF1：checked 1,700 / mismatches 0；all-types avg 180,922.681 us，p50 2,899.281 us，p90 43,831.280 us，p99 3,458,485.918 us；load 34.628 s；disk 2,174,091,281 bytes。
- SF10：checked 1,700 / mismatches 0；all-types avg 1,974,806.086 us，p50 3,442.775 us，p90 70,662.558 us，p99 34,522,340.128 us；load 216.845 s；disk 16,664,449,041 bytes。
- SF10 negative/high-fanout avg 3,943,167.684 us，returned negative neighbors 84,017,646。

写法边界：Neo4j 用于说明通用图数据库在 typed-neighbor/high-fanout workload 上的代价，不是同类 LSM 存储引擎竞赛。

### TuGraph

定位：embedded graph database baseline。

实现口径：使用 `tugraph/tugraph-runtime-ubuntu18.04:4.0.0-finbench` 镜像中的 embedded C++ API driver，加载同一 dense edge set，并用 shared fixed-seed truth 做 count/hash digest。

结果：

- SF1：loaded 3,181,724 vertices / 34,692,699 edges；checked 1,700 / mismatches 0；avg 420.925 us，p50 6.358 us，p90 151.398 us，p99 17,602.600 us；load 269.804 s；peak RSS 1,618,668 KB；disk 1,595,001,088 bytes。
- SF10：loaded 29,987,835 vertices / 355,185,382 edges；checked 1,700 / mismatches 0；avg 3,775.450 us，p50 12.630 us，p90 633.490 us，p99 158,870 us；load 3,939.95 s；peak RSS 14,697,508 KB；disk 14,960,539,904 bytes。

写法边界：这是 embedded C++ API typed-neighbor baseline，不是完整 TuGraph LDBC Interactive benchmark。

## 内部/布局 Baseline

### LSMGraph-style

定位：internal layout-style baseline，不是官方 external LSMGraph artifact。

可写用途：

- 用于说明 LSM/level-style layout 在 SemL0 workload 下的代价。
- 放在 internal/layout-style rows，不与 external systems 混写。

## Qualitative / Artifact Attempt

### Aster

远端当前无本地 source checkout 或 Docker image。2026-06-29 继续公开检索后，能确认 Aster 论文存在，但没有定位到可直接复现的官方 GitHub/source/artifact 入口。当前判断更接近 artifact 未公开、未归档或入口不明显，而不是远端下载权限问题。可作为 LSM-structured graph database 的 qualitative comparison；拿到 artifact 并通过 gate 前不能给数值。

### NebulaGraph

NebulaGraph 是官方开源图数据库，远端 GitHub 能访问 `vesoft-inc/nebula` 和 `vesoft-inc/nebula-docker-compose`。2026-06-29 换源后，`docker.1ms.run` 可获取 `vesoft/nebula-{graphd,metad,storaged}:v3.8.0`，三项服务镜像已拉取并 retag 为官方名。当前 blocker 已从 image availability 降级为 service/schema/loader/query/digest。可作为 popular distributed graph database 的 design comparison；完成 SF1/SF10 typed-neighbor digest 前不能给数值。

### Teseo、GraphOne、LLAMA、Aspen

这些系统适合 related work 或 design-space comparison，但公开 artifact/API/workload 与 SemL0 的 LDBC property-graph typed-neighbor workload 不直接匹配。不要硬塞进主 latency table。

### ByteGraph、BG3、Galaxybase、GES

这些更适合作 industry/system paper 写法参考，例如 motivation、deployment framing、lessons learned。SemL0 不应暗示自己有 production deployment 或能与这些生产系统做 production-scale apples-to-apples。

## 论文可用表述

可以写：

> We evaluate SemL0 against measured external baselines that pass the same-workload digest gate: LiveGraph as a scope-limited dynamic graph storage baseline, Neo4j Community as a general property-graph database baseline, and TuGraph as an embedded graph database baseline. We also report artifact attempts for Aster and NebulaGraph, and keep them out of the numeric table until they satisfy the same loader, workload, and count/hash digest gate.

不要写：

- We beat all graph databases.
- Aster/NebulaGraph are measured baselines.
- LSMGraph-style is an official external LSMGraph artifact.
