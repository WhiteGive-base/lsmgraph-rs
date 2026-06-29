# 外部 Baseline 执行状态更新（2026-06-27）

本文是 `external-baseline-survey-cn.md` 的执行状态补充页，用于写 CIDR draft 时引用。原始结果包在远端：

- `/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/`
- 进度表：`progress-table.md`
- 效果表：`effect-table.md`
- 机器可读表：`metrics.tsv`、`correctness.tsv`

## 当前可以进入数值表的外部 baseline

### LiveGraph

状态：SF1/SF10 typed-neighbor baseline 已完成，count/hash digest PASS。

可写口径：

- scope-limited measured external baseline。
- 同一 LDBC dense edge set。
- 固定 seed 采样 typed-neighbor workload。
- dense edge list 与 LiveGraph 查询结果做 count/hash digest 校验。

关键结果：

- SF1 all-types avg 41.293 us，load 21.155 s，RSS 4,850,512 KB，disk 5,368,709,120 bytes。
- SF10 all-types avg 391.063 us，load 1,457.36 s，RSS 47,908,544 KB，disk 38,654,705,664 bytes。
- SF10 positive-only avg 2.089 us，negative/high-fanout avg 780.036 us。

论文里不要写成“完整图数据库 head-to-head”，也不要写“全面打败 LiveGraph”。它是相同 edge set 和 typed-neighbor workload 下的范围受限外部 baseline。

### Neo4j Community

状态：SF1 smoke 和 SF10 main 均已完成，count/hash digest PASS。

导入模型：把 SemL0 dense edge list 的所有 edge type 都作为 Neo4j 中独立的 outgoing relationship type 导入，例如 `E_Px` / `E_Nx`。这避免把负向 edge type 错写成普通 incoming traversal。

SF1 smoke：

- checked=1700，mismatches=0。
- all-types avg 180,922.681 us，p50 2,899.281 us，p90 43,831.280 us，p99 3,458,485.918 us。
- positive-only avg 4,296.644 us。
- negative/high-fanout avg 357,548.718 us。
- load 34.628 s，disk 2,174,091,281 bytes。

SF10 main：

- checked=1700，mismatches=0。
- all-types avg 1,974,806.086 us，p50 3,442.775 us，p90 70,662.558 us，p99 34,522,340.128 us。
- positive-only avg 6,444.488 us。
- negative/high-fanout avg 3,943,167.684 us。
- load 216.845 s，disk 16,664,449,041 bytes。
- returned neighbors total 84,104,814，其中 negative edge types 占 84,017,646。

可写口径：Neo4j 是一个通用图数据库 baseline，用于说明 property graph DB 在该 typed-neighbor/high-fanout workload 上的代价，不是同类存储引擎的一对一系统竞赛。

## 内部/布局 baseline

### LSMGraph-style

状态：已从 SemL0 ablation trace 提取 SF30/SF100 layout-style rows。

可写口径：

- internal layout baseline，不是官方外部 LSMGraph artifact。
- 可以帮助说明 LSM/level-style layout 在 typed-neighbor pruning workload 下的代价。
- 不要写成 external LSMGraph system result。

### TuGraph

状态：SF1/SF10 full typed-neighbor baseline 均已完成，count/hash digest PASS。

实现口径：使用 TuGraph runtime image 中的 embedded C++ API driver，加载同一 SF1 dense edge list，并用 Neo4j/密集边表共享的 fixed-seed truth 做 count/hash digest。

SF1 关键结果：

- loaded_vertices=3,181,724。
- loaded_edges=34,692,699。
- checked=1700，mismatches=0。
- all-types avg 420.925 us，p50 6.358 us，p90 151.398 us，p99 17,602.600 us。
- load 269.804 s，peak RSS 1,618,668 KB，disk 1,595,001,088 bytes。

SF10 关键结果：

- loaded_vertices=29,987,835。
- loaded_edges=355,185,382。
- checked=1700，mismatches=0。
- all-types avg 3,775.450 us，p50 12.630 us，p90 633.490 us，p99 158,870 us。
- load 3,939.95 s，peak RSS 14,697,508 KB，disk 14,960,539,904 bytes。

可写口径：TuGraph 可以作为 SF1/SF10 measured graph DB baseline 写入数值表。需要说明这是 embedded C++ API typed-neighbor driver，不是完整 TuGraph LDBC Interactive benchmark。

## 不能进入数值主表的系统

### Aster

状态：远端当前没有本地 source checkout 或 Docker image；2026-06-29 继续公开检索后，能确认 Aster 论文存在，但没有定位到可直接复现的官方 GitHub/source/artifact 入口。

这更像 artifact 未公开、未归档或入口不明显，不是远端下载权限问题。当前只能写 qualitative/artifact-needed row。拿到 source/artifact 并接入同一 typed-neighbor workload 前，不能给数值。

### NebulaGraph

状态：NebulaGraph 是官方开源系统；远端能访问 `vesoft-inc/nebula` 和 `vesoft-inc/nebula-docker-compose` 的 GitHub 仓库。2026-06-29 已经通过 `docker.1ms.run` mirror 拉取并 retag 三个服务镜像：`vesoft/nebula-graphd:v3.8.0`、`vesoft/nebula-metad:v3.8.0`、`vesoft/nebula-storaged:v3.8.0`。

远端排查结论：原 blocker 是网络/代理问题，不是系统不存在。远端 shell 的 `HTTP_PROXY/HTTPS_PROXY` 指向 `127.0.0.1:7897`，该代理端口未启动；取消代理后直连 Docker Hub 仍被拒绝；`dockerproxy.com` 超时，`docker.m.daocloud.io` 对该探测返回 read-only denial；`docker.1ms.run` 可用。

当前状态从 image/source setup pending 升级为 server images ready / SF1 smoke pending。服务启动、schema、loader、query driver、digest gate 都完成前，仍不能给数值。

## CIDR draft 写法建议

可以写：

> We evaluate SemL0 against measured external baselines that pass the same-workload digest gate: LiveGraph as a scope-limited dynamic graph storage baseline, Neo4j Community as a general property-graph database baseline, and TuGraph as an embedded graph database baseline. We report artifact attempts for Aster and NebulaGraph, and keep them out of the numeric table until they satisfy the same loader, workload, and count/hash digest gate.

不要写：

- “SemL0 beats all graph databases.”
- “Aster/NebulaGraph results are measured baselines.”
- “LSMGraph-style is an official external LSMGraph artifact.”

## 下一步

优先级：

1. NebulaGraph：镜像获取 blocker 已通过 `docker.1ms.run` 解决；下一步完成 service/schema/loader smoke。
2. Aster：继续找官方 source/artifact；如果没有公开入口，写成 artifact-unavailable qualitative comparison。
3. CIDR draft：主表放 LiveGraph + Neo4j + TuGraph + 内部 LSMGraph-style；Aster/NebulaGraph 放 artifact attempt 和 design comparison。
