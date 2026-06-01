# Graph-aware L0 与 RA-score Targeted Compaction 说明

本文档解释当前 LSMGraph 中 graph-aware L0、L0 filter metrics、RA-score picker 和 targeted compaction 的实现逻辑。

## 1. 要解决的问题

原始 LSM-CSR 第一版写入路径会不断 flush 出多个 L0 run。对 `get_neighbors(src, edge_type)` 来说，如果 L0 有很多 run，读路径需要逐个判断这些 L0 segment 是否可能包含目标 `src`。

在 SF1 storage microbench 中曾观察到：

```text
levels: [17, 0, 0, 0, 0]
10 个采样 src 需要跨 17 个 L0 文件做 header/offset 探测
```

这类问题分两层：

```text
逻辑读放大：一次邻居查询需要检查多少个 L0 segment
物理 I/O 放大：这些检查实际带来多少 header/offset/body read
```

Direct I/O 或 io_uring 只能优化已经发生的 I/O，不能消除不必要的 L0 segment 探测。所以当前优化顺序是：

```text
先减少候选 L0 segment 数
再减少 metadata I/O
最后再评估 Direct I/O / io_uring
```

## 2. Graph-aware L0 的基本思路

Graph-aware L0 的目标是让 L0 segment 更接近图查询语义，而不是只按 flush 时间组织。

当前实现使用的分区语义是：

```text
source label + edge type + src range
```

例如 SNB 中：

```text
src_label = 1 表示 Person
edge_type = 1 表示 Knows
```

那么一个典型 partition 是：

```text
(Person, Knows, src_range=[72057594037927936, 72057594038976511])
```

其中 `src_range` 不是固定业务 ID 区间，而是按内部 encoded vertex id 的 bucket 划分。默认 bucket size 在配置中是：

```rust
l0_ra_range_bucket_size = 1 << 20
```

## 3. Flush 阶段做了什么

开启 `--graph-aware-l0` 后，MemGraph flush 到 L0 时不再只生成一个大 CSR segment，而是：

```text
1. 从 MemGraph 收集所有 EdgeRecord
2. 按 (source_label_from_vertex_id(src), edge_type) 分组
3. 小分区合并到 Mixed，避免低频 relation 产生大量小文件
4. 每个分区内部按 (src, edge_type, dst, ts) 排序
5. 按 segment_target_bytes 切分
6. 切分时不在同一个 src 的邻接表中间切 segment
7. 每个 segment 写成一个 L0 CSR 文件
```

相关代码：

```text
src/graph.rs
  Engine::build_l0_flush_segments
  split_range_bounded_segments

src/csr/writer.rs
  CsrWriter::write_segment

src/csr/format.rs
  CsrHeader
  CsrSegmentMeta
```

每个 CSR segment metadata 现在包含：

```text
src_label
edge_type_partition
min_src / max_src
min_ts / max_ts
edge_count
unique_src_count
segment_bytes
```

这些 metadata 用于后续查询过滤和 compaction 选择。

## 4. L0 查询过滤链路

`get_neighbors(src, edge_type, snapshot)` 查询 L0 时按下面顺序过滤：

```text
1. schema partition filter
   判断 segment 的 src_label / edge_type_partition 是否可能匹配查询。

2. range filter
   判断 src 是否落在 segment 的 min_src / max_src 中。

3. src membership Bloom filter
   如果 offset table 已在 metadata cache 中，则用 Bloom filter 判断 segment 是否可能包含 src。

4. offset lookup
   在 sparse EdgeOffset 表里二分查找 src。

5. body read
   只有 offset 命中时才读取该 src 的 edge bodies。
```

相关代码：

```text
src/graph.rs
  Engine::get_neighbors_typed_internal

src/csr/reader.rs
  CsrReader::cached_may_contain_src
  CsrReader::get_neighbors

src/csr/cache.rs
  CsrMetadataCache
  SourceBloom
```

## 5. 为什么要按 partition 聚合指标

全局指标只能说明“L0 很慢”，但不能说明“谁导致慢”。

例如：

```text
Person-Knows        查询很热，每次候选 17 个 segment
Post-HasTag         segment 多，但查询很少
Forum-HasMember     查询少，rewrite 成本低
```

如果只按 L0 文件数 compact，会把很多不热的数据也重写，write amplification 会变高。

所以当前实现把 L0 filter metrics 按以下 key 聚合：

```rust
L0PartitionKey {
    src_label,
    edge_type,
    range_start,
    range_end,
}
```

对应代码：

```text
src/metrics.rs
  L0PartitionKey
  L0PartitionProbe
  L0PartitionSnapshot
  Metrics::record_l0_partition_query
  Metrics::record_l0_partition_probe
  Metrics::l0_partition_snapshots

src/graph.rs
  Engine::l0_partition_key
```

## 6. 每个 partition 记录哪些指标

每个 partition 会记录：

```text
query_count
candidate_segments
range_filtered_segments
bloom_filtered_segments
filter_passed_segments
matched_segments
offset_cache_hits
offset_cache_misses
body_reads
bloom_false_positive_probes
avg_candidate_segments
offset_cache_miss_rate
```

含义如下：

```text
query_count
  有多少次查询触达这个 partition。

candidate_segments
  schema filter 之后，这个 partition 下被认为可能相关的 L0 segment 数。

range_filtered_segments
  被 min_src / max_src 过滤掉的 segment 数。

bloom_filtered_segments
  metadata cache 中 Bloom filter 判断“不可能包含 src”的 segment 数。

filter_passed_segments
  通过 schema/range/Bloom 后，真正进入 offset lookup 的 segment 数。

matched_segments
  offset lookup 命中并读取 body 的 segment 数。

offset_cache_hits / misses
  CSR metadata cache 是否命中。miss 通常意味着要读 header + offset table。

body_reads
  读取 edge body 的次数。

bloom_false_positive_probes
  Bloom 认为可能包含 src，但 offset lookup 最终没有找到 src 的次数。
```

这些指标会出现在 JSON metrics 中：

```text
metrics.csr.l0_partitions
```

## 7. Score 怎么计算

当前 score 公式是：

```text
score =
  query_count
  * avg_candidate_segments
  * (1 + offset_cache_miss_rate)
  / max(estimated_rewrite_MiB, 1)
```

直觉：

```text
query_count 越高，说明越热，越值得 compact。

avg_candidate_segments 越高，说明每次查询要检查的 L0 segment 越多，读放大越严重。

offset_cache_miss_rate 越高，说明 metadata cache 没兜住，实际 I/O 成本更高。

estimated_rewrite_MiB 越大，说明 compact 成本越高，应降低优先级。
```

相关代码：

```text
src/graph.rs
  Engine::pick_l0_partition_by_score
  Engine::score_l0_partition
```

默认阈值：

```rust
l0_ra_min_queries = 10
l0_ra_min_l0_segments = 2
l0_ra_min_score = 10.0
```

如果 query 太少，或者只涉及一个 L0 segment，就不触发 targeted compaction。

## 8. Picker 选中后怎么 compact

picker 选中一个 partition 后，会调用 targeted compaction。

执行流程：

```text
1. 从 metrics 中读取所有 L0PartitionSnapshot
2. 对每个 partition 估算 score
3. 选择 score 最大的 partition
4. 根据 key 得到：
   src_label
   edge_type
   range_start
   range_end
5. 选择所有匹配该范围的 L0 segment
6. 选择 L1 中同 partition 且 src range 重叠的 segment
7. 读取这些 segment 的所有 edge records
8. 按 (src, edge_type, dst, ts) 合并
9. 保留 snapshot 下最新版本
10. 输出新的 L1 segment
11. manifest 记录 create/delete
12. version 切换
13. rebuild index
```

相关代码：

```text
src/graph.rs
  Engine::compact_best_l0_partition_by_score
  Engine::compact_l0_range_to_l1
  Engine::compact_l0_target_to_l1
  target_matches_meta
```

注意：targeted compaction 会把重叠 L1 segment 一起读进来重写，避免 L0 和 L1 中同一范围数据重复可见。

## 9. 命令怎么使用

### 9.1 导入 graph-aware L0 store

```bash
cd /data/WorkSpace/lsmgraph-rs

target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-graph-aware \
  --relation person_knows \
  --memgraph-bytes 1048576 \
  --graph-aware-l0
```

### 9.2 手动 compact 某个 partition

```bash
target/release/lsmgraph compact \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-graph-aware \
  --src-label 1 \
  --edge-type 1
```

这个会 compact 所有 `Person-Knows` L0 segment。

### 9.3 手动 compact 某个 src range

```bash
target/release/lsmgraph compact \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-graph-aware \
  --src-label 1 \
  --edge-type 1 \
  --min-src 72057594037927936 \
  --max-src 72057594038976511
```

这个只 compact 指定 range 内重叠的 L0 segment。

### 9.4 同进程采样并自动 compact

```bash
target/release/lsmgraph storage-bench \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-graph-aware \
  --samples 20 \
  --auto-compact \
  --ra-min-queries 1 \
  --ra-min-score 0 \
  > logs/ra-auto-pick-smoke.json
```

这里使用 `--ra-min-queries 1 --ra-min-score 0` 是为了 smoke test 更容易触发。正式实验建议使用默认值或更保守的阈值。

### 9.5 查看结果

```bash
python3 - <<'PY'
import json
p='logs/ra-auto-pick-smoke.json'
with open(p) as f:
    d=json.load(f)
print('levels:', d['levels'])
print('auto_compaction:', json.dumps(d['auto_compaction'], indent=2)[:2000])
print('partition_count:', len(d['metrics']['csr']['l0_partitions']))
PY
```

## 10. 当前实现的限制

### 10.1 Metrics 是进程内状态

`compact --auto-pick` 依赖当前进程内的 metrics。

如果这样跑：

```bash
target/release/lsmgraph neighbors ...
target/release/lsmgraph compact --auto-pick ...
```

第二个命令是新进程，看不到第一个命令积累的 metrics，所以可能选不到 partition。

当前建议：

```text
用 storage-bench --auto-compact 做实验
或后续把 picker 接到长驻 snb-server 的 HTTP endpoint
```

### 10.2 当前 picker 是同步执行

现在 picker 被调用后会直接执行 compaction，不是后台 scheduler。

后续可以扩展为：

```text
server 周期性检查 l0_partitions
选 score 高的 partition
放入 compaction queue
加 cooldown / hysteresis / rate limit
```

### 10.3 src_range bucket 还比较粗

默认 `1 << 20` 是工程初值。后续需要在 SF1/SF10/SF30 上比较：

```text
bucket 太小：partition 太碎，metrics 和 compaction 元数据变多
bucket 太大：targeted compact 范围太大，rewrite bytes 增加
```

### 10.4 Mixed partition 会降低 edge_type 精度

低频 relation 会合并到 Mixed，查询时 Mixed 作为 wildcard 参与过滤。

好处是控制小文件数量。

代价是某些低频 edge type 查询会多检查 Mixed segment。

## 11. 当前验证结果

真实 SF1 `person_knows` smoke 中：

```text
input_rows: 180,623
directed_edges: 361,246
storage-bench samples: 20
l0_partitions: 16
candidate_l0_segments: 240
range_filtered_segments: 19
bloom_filtered_segments: 29
filter_passed_segments: 193
matched_l0_segments: 192
offset_cache_misses: 13
```

picker 选中：

```text
src_label: 1
edge_type: 1
range_start: 72057594037927936
range_end: 72057594038976511
query_count: 2
avg_candidate_segments: 12.0
offset_cache_miss_rate: 0.9167
selected_l0_segments: 11
estimated_rewrite_bytes: 12,950,088
score: 3.7246
output L1 segments: 1
```

compact 后：

```text
levels: [1, 1, 0, 0, 0]
validate-knows 1000 sampled vertices: passed
```

`[1,1,0,0,0]` 的含义是：hot range 覆盖的 11 个 L0 segment 被压到了 L1，剩余 1 个 L0 segment 不覆盖该 hot range，所以没有被重写。

## 12. 后续可以怎么发展成 research 点

当前实现已经具备实验基础，可以继续做以下对比：

```text
Baseline A: naive L0, no cache, no compaction
Baseline B: header/offset cache only
Baseline C: full L0->L1 compaction
Design 1: graph-aware flush
Design 2: graph-aware flush + Bloom filter
Design 3: RA-score targeted compaction
Design 4: RA-score targeted compaction + cooldown/rate limit
```

核心研究问题可以表述为：

```text
在动态图 LSM-CSR 中，是否可以利用图 schema、source range 和真实查询反馈，
以更低 write amplification 换取接近全量 compaction 的 L0 read amplification 降低？
```

