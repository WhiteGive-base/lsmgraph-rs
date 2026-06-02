# LSMGraph L0 Read Amplification 对比记录

记录时间：2026-05-29  
项目路径：`/data/WorkSpace/lsmgraph-rs`  
数据集：LDBC SNB SF1  
测试目标：比较 naive L0、graph-aware L0-only、L0->L1 compact 后三种状态下 `get_neighbors(src, edge_type)` 的读放大与 QPS。

## 1. 测试口径

本轮是 storage microbench，不是 Java LDBC benchmark。

```bash
cd /data/WorkSpace/lsmgraph-rs
target/release/lsmgraph storage-bench \
  --data-dir <store> \
  --samples 100 \
  --edge-type 1
```

说明：

- `edge_type=1` 对应 SNB `Person-Knows`。
- `storage-bench` 已新增 typed 模式：采样只从 `edge_type=1` 的边选 source，并调用 `get_neighbors_typed(src, 1, snapshot)`。
- 每轮都会先 `scan_edges` 选采样点，所以 `read_syscalls/read_bytes` 包含全量 scan 阶段；判断 L0 设计收益时，核心看 `get_neighbors_elapsed_ms`、QPS、`candidate_l0_segments`、`matched_l0_segments`。

## 2. 三种 Store 状态

| 状态 | Store | Level layout | 设计含义 |
|---|---|---:|---|
| 原始 naive L0 | `/data/WorkSpace/lsmgraph-rs/store/sf1-bench` | `[17,0,0,0,0]` | flush run 全留在 L0，不按 schema 分区 |
| Graph-aware L0-only | `/data/WorkSpace/lsmgraph-rs/store/sf1-graph-aware-l0-codex-20260529` | `[69,0,0,0,0]` | 按 `(src_label, edge_type)` 分区并切 segment，但不 compact 到 L1 |
| L0->L1 compact | `/data/WorkSpace/lsmgraph-rs/store/sf1-bench-l1-codex-20260529-1050-695431` | `[0,1,0,0,0]` | 全量 L0 compact 成一个 L1 CSR |

Graph-aware L0-only 虽然 L0 segment 数从 17 增加到 69，但 typed 查询可以先按 schema partition 剪枝，所以它是检验“L0 层研究点是否有效”的关键状态。

## 3. Typed `Knows` 查询结果

| 指标 | Naive L0 | Graph-aware L0-only | L0->L1 compact |
|---|---:|---:|---:|
| sampled vertices | 100 | 100 | 100 |
| returned neighbor edges | 15,223 | 15,223 | 15,223 |
| get_neighbors elapsed | 392 ms | 16 ms | 13 ms |
| get_neighbors QPS | 255.10 | 6,250.00 | 7,692.31 |
| candidate L0 segments | 1,700 | 300 | 0 |
| range filtered segments | 700 | 0 | 0 |
| bloom filtered segments | 10 | 1 | 0 |
| filter passed segments | 990 | 299 | 0 |
| matched L0 segments | 990 | 299 | 0 |
| offset cache hits / misses | 980 / 10 | 296 / 3 | 100 / 1 |
| body reads | 1,007 | 368 | 101 |
| read syscalls | 1,061 | 512 | 105 |
| read bytes | 1.51 GB | 1.39 GB | 1.27 GB |

日志文件：

```text
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf1-naive-l0-edge1-samples100-20260529.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf1-graphaware-l0-edge1-samples100-20260529.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf1-l1-edge1-samples100-20260529.json
```

## 4. 提升幅度

Graph-aware L0-only 相比 naive L0：

```text
get_neighbors: 392ms -> 16ms
QPS: 255.10 -> 6250.00
提升倍数: 24.5x
candidate L0 segments: 1700 -> 300，下降 82.4%
matched L0 segments: 990 -> 299，下降 69.8%
body reads: 1007 -> 368，下降 63.5%
```

L0->L1 compact 相比 naive L0：

```text
get_neighbors: 392ms -> 13ms
QPS: 255.10 -> 7692.31
提升倍数: 30.2x
candidate L0 segments: 1700 -> 0
matched L0 segments: 990 -> 0
body reads: 1007 -> 101
```

Graph-aware L0-only 相比 L1 compact：

```text
get_neighbors: 16ms vs 13ms
QPS: 6250.00 vs 7692.31
Graph-aware L0-only 已达到 L1 compact baseline 的约 81.3% QPS。
```

## 5. 结论

这轮补充实验说明：L0 层本身确实有优化空间，而且收益不是只靠“压到 L1”得到的。

Naive L0 的问题是每个 typed neighbor 查询仍然要跨多个 flush run 做探测。100 个 `Person-Knows` 点查产生了 1,700 个 L0 candidate segment，最后 990 个 segment 进入 offset/body 阶段。

Graph-aware L0-only 没有 compact 到 L1，仍然保留 `[69,0,0,0,0]`，但通过 `(src_label, edge_type)` schema partition，把 candidate L0 segment 从 1,700 降到 300。也就是说，即使 L0 segment 数更多，只要查询带 typed hint，逻辑读放大反而显著下降。

L0->L1 compact 仍然是更强的 baseline，因为它直接消除 L0 探测。但 graph-aware L0-only 已经接近 L1 compact 的点查 QPS，说明后续 research 可以聚焦在：

```text
schema-aware L0 partition
+ src range bounded segment
+ src membership Bloom filter
+ RA-score targeted compaction
```

目标不是完全替代 compaction，而是在热点 relation/range 尚未 compact 前，把 L0 的读放大控制住，并用实际查询代价决定哪些 partition/range 最值得 compact。

## 6. 与 Java LDBC Benchmark 的关系

当前标准 Java LDBC benchmark 主要测 HTTP adapter + SNB adjacency cache，不代表底层 CSR `get_neighbors` 性能。

已记录的 compacted L1 store 标准流程结果：

```text
命令：
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs
bash run.sh interactive-benchmark-sf1.properties

结果：
throughput: 196.91 op/s
benchmark duration: 1.295s
server startup: 25.1s
SNB cache load: 22.4s
```

正规流程保留为：

```bash
# validate
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs
bash run.sh interactive-validate.properties

# benchmark
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs
bash run.sh interactive-benchmark-${Scale_Factor}.properties
```

## 7. 后续建议

下一步应该把本轮 typed storage-bench 扩展成批量 relation 测试：

```text
edge_type=1  Person-Knows
edge_type=2  HasCreator
edge_type=3  HasTag
edge_type=6  LikesPost
edge_type=7  LikesComment
```

这样可以判断 graph-aware L0 对高频 relation、低频 relation、单向/双向 relation 的收益是否稳定。之后再接 RA-score 自动 compaction，让系统在 L0-only 阶段根据实际查询代价自动挑选 hot partition/range compact。

## 8. SF10 复测结果

记录时间：2026-05-30  
数据集：LDBC SNB SF10  
测试口径同 SF1：`storage-bench --samples 100 --edge-type 1`，即 typed `Person-Knows` neighbor lookup。

### 8.1 Store 状态

| 状态 | Store | Level layout | 说明 |
|---|---|---:|---|
| Naive L0 | `/data/WorkSpace/lsmgraph-rs/store/sf10-bench` | `[170,0,0,0,0]` | 原始 SF10 store，全部停留在 L0 |
| Graph-aware L0-only | `/data/WorkSpace/lsmgraph-rs/store/sf10-graph-aware-l0-codex-20260530b` | `[376,0,0,0,0]` | 用 `/data/WorkSpace/ldbc-sf10/social_network` 重新导入，启用 `--graph-aware-l0`，不 compact |
| L0->L1 compact | `/data/WorkSpace/lsmgraph-rs/store/sf10-bench-cp` | `[0,1,0,0,0]` | 从 `[170,0,0,0,0]` 全量 compact 到 1 个 L1 CSR |

准备阶段耗时：

```text
SF10 L0->L1 compact: 2026-05-30T00:14:35 -> 00:21:12，约 6m37s
SF10 graph-aware L0-only import: 2026-05-30T00:23:12 -> 00:41:46，约 18m34s
Graph-aware import directed_edges: 355,185,382
Graph-aware adjacency cache write elapsed: 379.0s
```

### 8.2 Typed `Knows` 查询结果

| 指标 | Naive L0 | Graph-aware L0-only | L0->L1 compact |
|---|---:|---:|---:|
| sampled vertices | 100 | 100 | 100 |
| candidate edges for sampling | 3,877,032 | 3,877,032 | 3,877,032 |
| returned neighbor edges | 20,765 | 20,765 | 20,765 |
| scan_edges | 355,185,382 | 355,185,382 | 355,185,382 |
| scan elapsed | 308,914 ms | 290,513 ms | 247,431 ms |
| get_neighbors elapsed | 3,496 ms | 45 ms | 19 ms |
| get_neighbors QPS | 28.60 | 2,222.22 | 5,263.16 |
| candidate L0 segments | 17,000 | 500 | 0 |
| range filtered segments | 9,021 | 2 | 0 |
| bloom filtered segments | 904 | 18 | 0 |
| filter passed segments | 7,081 | 480 | 0 |
| matched L0 segments | 7,075 | 480 | 0 |
| offset cache hits / misses | 7,001 / 80 | 475 / 5 | 100 / 1 |
| body reads | 7,245 | 856 | 101 |
| read syscalls | 7,745 | 1,618 | 105 |
| read bytes | 14.90 GB | 14.06 GB | 12.82 GB |

日志文件：

```text
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf10-naive-l0-edge1-samples100-20260530.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf10-graphaware-l0-edge1-samples100-20260530.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf10-l1-edge1-samples100-20260530.json
```

### 8.3 SF10 结论

Graph-aware L0-only 相比 naive L0：

```text
get_neighbors: 3496ms -> 45ms
QPS: 28.60 -> 2222.22
提升倍数: 77.7x
candidate L0 segments: 17000 -> 500，下降 97.1%
matched L0 segments: 7075 -> 480，下降 93.2%
body reads: 7245 -> 856，下降 88.2%
```

L0->L1 compact 相比 naive L0：

```text
get_neighbors: 3496ms -> 19ms
QPS: 28.60 -> 5263.16
提升倍数: 184.0x
candidate L0 segments: 17000 -> 0
matched L0 segments: 7075 -> 0
body reads: 7245 -> 101
```

Graph-aware L0-only 相比 L1 compact：

```text
get_neighbors: 45ms vs 19ms
QPS: 2222.22 vs 5263.16
Graph-aware L0-only 达到 L1 compact baseline 的约 42.2% QPS。
```

SF10 比 SF1 更能说明 L0 读放大问题：naive L0 从 17 个 run 增长到 170 个 run 后，100 个 `Person-Knows` 点查产生 17,000 个 L0 candidate segment。Graph-aware L0-only 虽然有 376 个 L0 segment，但 typed partition pruning 把 candidate 降到 500，所以在不 compact 到 L1 的情况下仍然得到 77.7x 点查提升。
