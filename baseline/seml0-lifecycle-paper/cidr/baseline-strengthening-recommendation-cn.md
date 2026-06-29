# Baseline 加强建议与执行结果

更新日期：2026-06-30

导师要求的方向是：至少有经验总结、方法动机和实现描述；收集足够部署和实验结果；和类似系统比较。如果没有完全合适的 apples-to-apples 对象，CIDR 可以接受，但必须说明为什么不可比，并给出 artifact attempt 和 design matrix。

## 建议的 3+3 Baseline 分组

### 问题相关系统

| 系统 | 角色 | 当前口径 |
|---|---|---|
| LiveGraph | transactional graph storage / adjacency scan baseline | 已完成 SF1/SF10 measured baseline，digest PASS，scope-limited numeric |
| LSMGraph-style | LSM/layout-style internal baseline | 已提取 SF30/SF100 internal layout rows，不写成官方 external artifact |
| Aster RocksGraph | LSM-adjacent graph storage bridge baseline | 已完成 SF1/SF10 typed-neighbor bridge，digest PASS；不写成完整 AsterDB/Gremlin benchmark |

BACH 可作为 related-work motivation，说明 adjacency list/CSR 物理布局转换；没有 artifact/workload bridge 前不进入 baseline row。

### 开源图数据库系统

| 系统 | 角色 | 当前口径 |
|---|---|---|
| Neo4j Community | mainstream property-graph database baseline | 已完成 SF1/SF10 measured baseline，digest PASS |
| TuGraph | embedded graph database / LDBC-friendly baseline | 已完成 SF1/SF10 measured baseline，digest PASS |
| NebulaGraph | distributed open-source graph database representative | 已完成 SF1/SF10 nGQL edge-type model baseline，digest PASS；不写成生产部署 |

## 当前可进入数值表的系统

| 系统 | Scale | Correctness | 说明 |
|---|---|---|---|
| LiveGraph | SF1/SF10 | PASS | typed-neighbor scope-limited dynamic graph storage baseline |
| Aster RocksGraph | SF1/SF10 | PASS | LSM-adjacent typed-neighbor bridge baseline；不是完整 AsterDB/Gremlin benchmark |
| Neo4j Community | SF1/SF10 | PASS | property-graph database baseline；所有 dense edge types 导入为独立 outgoing relationship types |
| TuGraph | SF1/SF10 | PASS | embedded C++ API typed-neighbor baseline；不是完整 TuGraph LDBC Interactive benchmark |
| NebulaGraph | SF1/SF10 | PASS | distributed graph DB baseline；每个 dense edge type 建一个 nGQL edge type |

## 不能写成数值 baseline 的系统

| 系统 | 原因 | 写法 |
|---|---|---|
| Teseo/GraphOne/LLAMA/Aspen | workload/API 与 LDBC property-graph typed-neighbor 不直接匹配 | related work/design comparison |
| LSMGraph-style | 内部 layout row，不是官方 external artifact | internal/layout-style row |

## 建议正文呈现方式

Evaluation：

- 主表或 external-baseline sidebar 放 LiveGraph、Aster RocksGraph、Neo4j、TuGraph、NebulaGraph 的 measured rows。
- 表注写清楚：同一 LDBC dense edge set、同一 fixed-seed sampled typed-neighbor workload、count/hash digest PASS。
- LSMGraph-style 放 internal/layout-style rows。
- Aster/NebulaGraph 放数值时必须写清楚 scope：Aster 是 RocksGraph typed-neighbor bridge，NebulaGraph 是 nGQL edge-type model，不是生产部署或完整 LDBC Interactive benchmark。

Comparison / Related Work：

- Problem-adjacent systems：LiveGraph、LSMGraph、Aster、BACH、Teseo、GraphOne。
- Open-source graph DBMS：Neo4j、TuGraph、NebulaGraph。
- 比较维度：storage layout、update model、query semantics 是否进入 storage、compaction/rewrite 是否保持 semantic surface、schema/snapshot correctness。

## 给导师的短答

可以这样回：

> 我们把 baseline 分成两组：问题相关系统和开源图数据库。问题相关组里，LiveGraph 已完成 SF1/SF10 typed-neighbor measured baseline；LSMGraph-style 作为内部 layout baseline；Aster RocksGraph 已完成 SF1/SF10 typed-neighbor bridge baseline。开源图数据库组里，Neo4j、TuGraph、NebulaGraph 都已完成 SF1/SF10 并通过同一 count/hash digest gate。主文不写“全面打败图数据库”，而是写在同一 typed-neighbor workload 和 correctness gate 下的 scope-limited measured comparison，并对 Aster/NebulaGraph 保留接口桥接边界。
