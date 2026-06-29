# 外部 Baseline 执行状态更新（2026-06-27）

更新：2026-06-30，Aster RocksGraph 与 NebulaGraph 已完成 SF1/SF10 typed-neighbor digest gate。

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

### Aster RocksGraph

状态：SF1/SF10 typed-neighbor bridge baseline 已完成，count/hash digest PASS。

实现口径：使用 `NTU-Siqiang-Group/Aster` 轻量 RocksGraph 实现；远端 checkout 记录 commit `6abb258e577c479325092a8ac0e7691fdfd154c2`。typed-neighbor bridge 将每个 `(edge_type, src)` 映射到 compact logical vertex id，避免 Aster MorrisCounter 对稀疏大 vertex id 的内存放大。这个结果可作为 Aster/RocksGraph storage bridge baseline，不能写成完整 AsterDB/Gremlin benchmark。

SF1 smoke：

- checked=1700，mismatches=0。
- all-types avg 150.985 us，p50 10.680 us，p90 381.904 us，p99 3,411.36 us。
- positive-only avg 8.648 us。
- negative/high-fanout avg 293.322 us。
- load 74.399 s，peak RSS 1,851,684 KB，disk 1,054,936,788 bytes。

SF10 main：

- checked=1700，mismatches=0。
- all-types avg 1,408.270 us，p50 580.529 us，p90 1,061.240 us，p99 15,809.60 us。
- positive-only avg 564.505 us。
- negative/high-fanout avg 2,252.040 us。
- load 794.262 s，peak RSS 15,621,088 KB，disk 10,401,409,041 bytes。

可写口径：Aster 可以进入数值表，但必须写成 Aster/RocksGraph typed-neighbor bridge baseline；不要写成完整 AsterDB 图数据库系统评测。

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

### NebulaGraph

状态：SF1/SF10 typed-neighbor baseline 已完成，count/hash digest PASS。

实现口径：使用官方 NebulaGraph v3.8.0 server images（`vesoft/nebula-graphd`、`vesoft/nebula-metad`、`vesoft/nebula-storaged`）。由于远端 Docker Hub 直连受限，镜像通过 `docker.1ms.run` 拉取后 retag 为官方名。space 使用 `vid_type=INT64`，每个 dense edge type 建一个 nGQL edge type（`E_Px` / `E_Nx`），用 Python client 批量写入和查询。

SF1 smoke：

- checked=1700，mismatches=0。
- all-types avg 61,553.966 us，p50 581.010 us，p90 10,736.324 us，p99 1,189,334.146 us。
- positive-only avg 1,012.955 us。
- negative/high-fanout avg 122,094.978 us。
- load 194.644 s，peak RSS 1,673,516 KB，disk 4,552,617,537 bytes。

SF10 main：

- checked=1700，mismatches=0。
- all-types avg 744,146.618 us，p50 741.524 us，p90 20,218.339 us，p99 14,228,169.276 us。
- positive-only avg 1,753.354 us。
- negative/high-fanout avg 1,486,539.881 us。
- load 2,493.471 s，peak RSS 3,665,396 KB，disk 46,185,803,519 bytes。

可写口径：NebulaGraph 可以作为 open-source distributed graph DB baseline 写入数值表，但必须说明这是同一 typed-neighbor workload 下的 nGQL edge-type model，不是生产部署、集群调优或完整 LDBC Interactive benchmark。

## 内部/布局 baseline

### LSMGraph-style

状态：已从 SemL0 ablation trace 提取 SF30/SF100 layout-style rows。

可写口径：

- internal layout baseline，不是官方外部 LSMGraph artifact。
- 可以帮助说明 LSM/level-style layout 在 typed-neighbor pruning workload 下的代价。
- 不要写成 external LSMGraph system result。

## 不能进入数值主表的系统

Teseo、GraphOne、LLAMA、Aspen 仍不进入数值主表：它们的公开 artifact/API/workload 与 SemL0 的 LDBC property-graph typed-neighbor workload 不直接匹配，适合 related work 或 design-space comparison。

## CIDR draft 写法建议

可以写：

> We evaluate SemL0 against measured external baselines that pass the same-workload digest gate: LiveGraph as a scope-limited dynamic graph storage baseline, Aster RocksGraph as an LSM-adjacent typed-neighbor bridge baseline, Neo4j Community as a general property-graph database baseline, TuGraph as an embedded graph database baseline, and NebulaGraph as a distributed open-source graph DB baseline. Each numeric row uses the same LDBC dense edge set, the same fixed-seed sampled typed-neighbor workload, and a per-query count/hash digest check.

不要写：

- “SemL0 beats all graph databases.”
- “Aster RocksGraph results are full AsterDB/Gremlin benchmark results.”
- “NebulaGraph results are production deployment or full LDBC Interactive benchmark results.”
- “LSMGraph-style is an official external LSMGraph artifact.”

## 下一步

优先级：

1. CIDR draft：主表或 external-baseline sidebar 放 LiveGraph + Aster RocksGraph + Neo4j + TuGraph + NebulaGraph，并保留各自 scope 注释。
2. 若投稿前要进一步加强 artifact story，可对 Aster/NebulaGraph 做 clean checkout / clean image reproduction，把 binary hash、image digest、run config 再固化一次。
3. Teseo/GraphOne/LLAMA/Aspen 继续放 design comparison，不硬塞进 latency 表。
