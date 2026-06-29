# 远端原型部署证据地图

更新日期：2026-06-27

## 部署口径

SemL0 当前只能写成 remote prototype deployment：

- 远端代码和实验路径：`/data/WorkSpace/lsmgraph-rs`
- 本地整理路径：`E:\文档\DGS项目复现\baseline\seml0-lifecycle-paper`
- 不能写 production deployment。
- 不能写真实用户 workload。
- 不能写 production write-stall characterization。

## 内部证据

### W6 SF100 main matrix

用途：证明 query-semantic control plane 能降低 candidate segments 和 read bytes。

证据路径：

- `baseline/sf100-matrix-20260613-cn.md`
- `baseline/seml0-lifecycle-paper/tables/plot-data-md.md`

关键数字：

- naive candidate L0：19,257,601。
- naive read bytes：461.6 MiB。
- semantic read bytes：42.7 MiB。
- full semantic RSS：118.03 GiB，是 memory-cliff stress point，不是推荐配置。

### W9 SF30 dynamic mixed read/write

用途：作为动态 workload 下 tail latency 的 supporting evidence。

证据路径：

- `baseline/w9-steady-state-summary-20260615-cn.md`
- `baseline/seml0-lifecycle-paper/tables/plot-data-md.md`

关键数字：

- 30 min，6 checkpoints，约 163 q/s，0 writer errors。
- 1800s p99：schema 8,740.2 us，semantic 1,274.0 us。

边界：latency 是 workload-dependent supporting evidence，不是 universal speedup。

### C2 controlled + real SF30

用途：证明 physical rewrite 会破坏或保持 semantic surface。

证据路径：

- `baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`
- `baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md`
- `remote-logs/c2-sf30-real-20260619/`
- `remote-logs/c2-sf30-readamp-proxy-20260621/`
- `baseline/seml0-lifecycle-paper/tables/table-c2-retention.md`

关键数字：

- real SF30：1.09B directed edges，40 `(src_label, edge_type)` partitions，528 exact L1 segments。
- semantic retention 1.0 vs naive 0.0。
- write_amp 1.24 vs 1.07。
- metadata proxy：semantic 1.00x vs naive 6.52x。

边界：

- controlled rows 测完整 read workload + correctness。
- real SF30 测 retention/write cost。
- real SF30 metadata replay 测 candidate segments/bytes proxy。
- 不写 full SF30 body-read workload 已完成。

### W13 schema/snapshot correctness

用途：证明 conservative fallback 不漏读。

证据路径：

- `baseline/w13-schema-evolution-summary-20260614.md`

覆盖点：

- old segment readability。
- mixed delta across compaction/reopen。
- alias/drop。
- encoding epoch。
- new label exact-vs-mixed pruning。

## 外部 Baseline 证据

结果包：

- 远端：`/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/`
- 本地：`baseline/external-baselines-20260626/3plus3-baselines/`

核心文件：

- `progress-table.md`
- `effect-table.md`
- `metrics.tsv`
- `correctness.tsv`
- `run-config.json`

已通过 gate 的外部系统：

| System | Scale | Correctness | 可写强度 |
|---|---|---|---|
| LiveGraph | SF1/SF10 | PASS | scope-limited dynamic graph storage baseline |
| Aster RocksGraph | SF1/SF10 | PASS | LSM-adjacent typed-neighbor bridge baseline |
| Neo4j Community | SF1/SF10 | PASS | general property-graph database baseline |
| TuGraph | SF1/SF10 | PASS | embedded graph database baseline |
| NebulaGraph | SF1/SF10 | PASS | distributed open-source graph DB baseline；nGQL edge-type model |

不能进数值表：

| System | 当前状态 | 口径 |
|---|---|---|
| Teseo/GraphOne/LLAMA/Aspen | workload/API 不匹配 | related work/design comparison |
| LSMGraph-style | internal layout row | 不写成 official external artifact |

## CIDR Evidence Table

| 问题 | 证据 | 当前可写强度 |
|---|---|---|
| Query signatures 能否降低 read amplification | W6 SF100 | 强 |
| Semantic control plane 是否需要 budget | W6 full semantic RSS outlier | 强 |
| Compaction 是否会破坏 semantic surface | C2 synthetic + real SF30 | 强 |
| Schema/snapshot 下能否不漏读 | W13 | 中强 |
| 动态 workload 中 latency 是否改善 | W9 SF30 | 中，supporting |
| 是否有外部 baseline | LiveGraph/Aster RocksGraph/Neo4j/TuGraph/NebulaGraph SF1/SF10 measured baselines，digest PASS | 中强，需保留 scope 边界 |
| 是否有生产部署 | 无 | 不能写 |
