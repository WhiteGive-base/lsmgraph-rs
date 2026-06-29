# LiveGraph 外部 baseline 执行报告（2026-06-26 更新）

## 结论

LiveGraph 已补上 query-count digest correctness。SF1 smoke、SF10 typed-neighbor scan、edge-count gate、per-query sampled count/hash digest 都通过；external drivers 也已在远端当前 worktree 中执行 `make clean && make all` clean rebuild，并记录 binary/source hashes。

因此 LiveGraph 可以从 “measured attempt, digest pending” 升级为 **measured external baseline with digest PASS**。仍然不要写成 “we beat LiveGraph” 或全面 apples-to-apples：LiveGraph 是 in-memory transactional graph store，SemL0 是 LSM-based prototype；比较范围是同一 LDBC edge set 上的 typed-neighbor scan 和 artifact-level reproducibility gate。

## 结果目录

- 远端：`/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260624/livegraph/`
- 本地：`E:\文档\DGS项目复现\baseline\external-baselines-20260624\livegraph`
- SF1 smoke：`sf1-smoke/`
- SF10 typed-neighbor scan：`sf10-typed-neighbor/`

## SF1 smoke

- edge count：34,692,699
- vertex count：3,181,724
- LiveGraph load：21.155 s
- peak RSS：4,850,512 KB
- LiveGraph footprint：5,368,709,120 bytes
- edge-count gate：`scan_edges = dense_edges = LiveGraph edge_count = 34,692,699`
- digest gate：checked 16,140 sampled queries，unique 13,898 query keys，mismatches 0，seed 42

## SF10 typed-neighbor scan

- edge count：355,185,382
- vertex count：29,987,835
- LiveGraph load：1,457.36 s
- benchmark peak RSS：47,908,544 KB
- LiveGraph footprint：38,654,705,664 bytes
- positive edge types：16,070 sampled scans，ops-weighted avg 2.089 us，total neighbors 1,645,266
- all edge types：32,140 sampled scans，ops-weighted avg 391.063 us，total neighbors 1,510,614,465
- edge_type=1：avg 3.53884 us，p50 2.587 us，p90 7.334 us，p99 15.091 us
- digest gate：checked 32,140 sampled queries，unique 27,579 query keys，mismatches 0，seed 42

## Gate 状态

| Gate | 状态 | 说明 |
|---|---:|---|
| public source / artifact | PASS | `deps/LiveGraph`、driver sources、binary hashes 均已记录。 |
| remote build/executable | PASS | 2026-06-26 在远端当前 worktree 执行 `make clean && make all`，重建 external drivers。 |
| 同一 LDBC edge set / converter | PASS | SemL0 scan dump、dense converter、LiveGraph loaded edge count 一致。 |
| sampled typed-neighbor workload | PASS | SF10 跑 34 个 edge types，positive edge types 覆盖数 >= 3。 |
| edge-count correctness | PASS | `scan_edges == dense_edges == livegraph.edge_count`。 |
| per-query count / digest correctness | PASS | fixed-seed sampled `(edge_type, src)`；dense edge-list count/hash vs LiveGraph `get_edges(src,label)` count/hash；SF1 16,140 checked / 0 mismatches，SF10 32,140 checked / 0 mismatches。 |
| load time / RSS / footprint / latency | PASS | 见 `metrics.tsv`、`livegraph.stderr`、`livegraph-footprint.tsv`。 |
| raw logs / config / DONE | PASS | JSON、stderr、progress.log、DONE、`run-config.json`、`build.log` 均保留。 |

## 主文使用建议

CIDR 版本现在可以把 LiveGraph 写成一个通过 artifact-level correctness gate 的 measured external baseline：同一 LDBC SF10 edge set、typed-neighbor sampled workload、load/RSS/footprint/latency、edge-count correctness、per-query digest correctness 都有记录。

仍需保留 scope boundary：这是 SF10 typed-neighbor 外部系统证据，不是生产部署，不是全面 SOTA 胜出，也不是 SF100 外部系统比较。
