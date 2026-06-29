# SemL0 CIDR 材料包

生成日期：2026-06-27

本目录用于把 SemL0 从 SIGMOD/research-track 写法调整为 CIDR 写法：强调系统设计原则、原型实现经验、资源化实验和边界，而不是强行写成完整图数据库 SOTA 竞赛。

## 先读哪些文件

1. `en/seml0-cidr-draft.md`  
   英文主稿草案。主题是 **Query Signatures as a Storage Control Plane for Dynamic Property Graphs**。

2. `cn/seml0-cidr-reading-cn.md`  
   中文阅读版，给自己和导师快速看故事、贡献、证据和边界。

3. `external-baseline-status-20260627-cn.md`  
   最新外部 baseline 状态页。当前可进数值表的是 LiveGraph、Neo4j Community、TuGraph；Aster/NebulaGraph 仍只能写 qualitative/artifact attempt。

4. `../../external-baselines-20260626/3plus3-baselines/progress-table.md`  
   3+3 baseline 当前进度表。

5. `../../external-baselines-20260626/3plus3-baselines/effect-table.md`  
   只包含通过 correctness gate 的数值结果。

6. `deployment-evidence-map-cn.md`  
   远端原型部署证据地图：内部 W6/W9/C2/W13、外部 LiveGraph/Neo4j/TuGraph、以及不能过度书写的边界。

7. `comparison-matrix-cn.md`  
   相关系统设计矩阵。用于解释为什么不是所有系统都适合 apples-to-apples 数值比较。

## 当前外部 baseline 结论

已通过 same-workload count/hash digest gate：

- LiveGraph：SF1/SF10 typed-neighbor measured baseline，scope-limited dynamic graph storage baseline。
- Neo4j Community：SF1/SF10 general property-graph database baseline。
- TuGraph：SF1/SF10 embedded graph database baseline，使用 embedded C++ API typed-neighbor driver。

未进入数值表：

- Aster：远端无本地 source checkout 或 Docker image，当前只能 qualitative/artifact-needed。
- NebulaGraph：server images 已通过 `docker.1ms.run` mirror 拉取并 retag；当前仍是 SF1 smoke pending，不能进数值表。
- LSMGraph-style：可作为内部 layout baseline，不是官方 external LSMGraph artifact。

## 写作口径

可以写：

- SemL0 把 query signatures 下沉为 storage control plane，影响 pruning、compaction、schema/snapshot fallback。
- 外部系统比较采用 reproducibility gate：同一 LDBC edge set、同一 sampled typed-neighbor workload、count/hash digest、load/RSS/disk/latency/raw logs。
- LiveGraph/Neo4j/TuGraph 通过 gate，因此进入数值表；Aster/NebulaGraph 没有通过 gate，因此只做 design comparison 或 artifact attempt。

不要写：

- SemL0 全面打败所有图数据库。
- Aster/NebulaGraph 已完成数值 baseline。
- LSMGraph-style 是官方外部 LSMGraph 实验。
- 当前远端原型是 production deployment。
