# LSMGraph 测试运行总览

记录时间：2026-05-30  
项目路径：`/data/WorkSpace/lsmgraph-rs`

这份文档把最近几轮测试统一放在一起，方便区分：

```text
1. storage microbench：测底层 LSMGraph/CSR 读放大。
2. Java LDBC benchmark：测 HTTP adapter + SNB adjacency cache + LDBC Driver 流程。
3. validation：测 LDBC 查询结果正确性。
```

当前最重要的判断是：

```text
Graph-aware L0 对底层 typed get_neighbors 读放大有明显收益。
Java LDBC benchmark 当前主要反映 SNB cache 查询层，不直接反映 CSR storage 读性能。
```

---

## 1. 正规执行流程

### Validate

```bash
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs
bash run.sh interactive-validate.properties
```

### Benchmark

```bash
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs
bash run.sh interactive-benchmark-${Scale_Factor}.properties
```

例如：

```bash
bash run.sh interactive-benchmark-sf10.properties
```

### Start Server

```bash
cd /data/WorkSpace/lsmgraph-rs
DATA_DIR=<store> \
HOST=127.0.0.1 \
PORT=9090 \
LSMGRAPH_METRICS_INTERVAL_SECS=30 \
bash deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

---

## 2. 已确认的 Validation 结果

### Mixed IC/IS/IU Validation

使用脚本：

```bash
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph/locate_first_failure.sh
```

结果：

```text
Processed: 20,000 / 20,000
Crashed: 0
Incorrect: 0
Validation Result: PASS
Finished: 2026-05-29T09:52:50+08:00
Total time: 28m54s
```

日志：

```text
/data/WorkSpace/lsmgraph-rs/logs/first-failure-20260529_092331-driver.log
/data/WorkSpace/lsmgraph-rs/logs/first-failure-20260529_092331-server.log
```

结论：

```text
IC/IS/IU 混合验证流程已经跑通，当前没有发现结果错误。
```

---

## 3. Storage Microbench 口径

测试命令：

```bash
cd /data/WorkSpace/lsmgraph-rs
target/release/lsmgraph storage-bench \
  --data-dir <store> \
  --samples 100 \
  --edge-type 1
```

说明：

```text
edge_type=1 表示 SNB Person-Knows。
这个 benchmark 会先 scan_edges 选采样点，再执行 typed get_neighbors。
核心看 get_neighbors_elapsed_ms、QPS、candidate_l0_segments、matched_l0_segments、body_reads。
read_bytes/read_syscalls 包含 scan_edges 阶段，所以不能只看它判断点查性能。
```

---

## 4. SF1 Storage Microbench

### Store 状态

| 状态 | Store | Level layout |
|---|---|---:|
| Naive L0 | `/data/WorkSpace/lsmgraph-rs/store/sf1-bench` | `[17,0,0,0,0]` |
| Graph-aware L0-only | `/data/WorkSpace/lsmgraph-rs/store/sf1-graph-aware-l0-codex-20260529` | `[69,0,0,0,0]` |
| L0->L1 compact | `/data/WorkSpace/lsmgraph-rs/store/sf1-bench-l1-codex-20260529-1050-695431` | `[0,1,0,0,0]` |

### 结果

| 指标 | Naive L0 | Graph-aware L0-only | L0->L1 compact |
|---|---:|---:|---:|
| sampled vertices | 100 | 100 | 100 |
| returned neighbor edges | 15,223 | 15,223 | 15,223 |
| get_neighbors elapsed | 392 ms | 16 ms | 13 ms |
| get_neighbors QPS | 255.10 | 6,250.00 | 7,692.31 |
| candidate L0 segments | 1,700 | 300 | 0 |
| matched L0 segments | 990 | 299 | 0 |
| body reads | 1,007 | 368 | 101 |
| read syscalls | 1,061 | 512 | 105 |

### SF1 结论

```text
Graph-aware L0-only 相比 naive L0：
  QPS 255.10 -> 6250.00
  提升约 24.5x
  candidate L0 segments 下降 82.4%
  matched L0 segments 下降 69.8%

L0->L1 compact 相比 naive L0：
  QPS 255.10 -> 7692.31
  提升约 30.2x

Graph-aware L0-only 达到 L1 compact baseline 的约 81.3% QPS。
```

日志：

```text
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf1-naive-l0-edge1-samples100-20260529.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf1-graphaware-l0-edge1-samples100-20260529.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf1-l1-edge1-samples100-20260529.json
```

---

## 5. SF10 Storage Microbench

### Store 状态

| 状态 | Store | Level layout |
|---|---|---:|
| Naive L0 | `/data/WorkSpace/lsmgraph-rs/store/sf10-bench` | `[170,0,0,0,0]` |
| Graph-aware L0-only | `/data/WorkSpace/lsmgraph-rs/store/sf10-graph-aware-l0-codex-20260530b` | `[376,0,0,0,0]` |
| L0->L1 compact | `/data/WorkSpace/lsmgraph-rs/store/sf10-bench-cp` | `[0,1,0,0,0]` |

准备阶段：

```text
SF10 L0->L1 compact: 约 6m37s
SF10 graph-aware L0-only import: 约 18m34s
SF10 directed_edges: 355,185,382
```

### 结果

| 指标 | Naive L0 | Graph-aware L0-only | L0->L1 compact |
|---|---:|---:|---:|
| sampled vertices | 100 | 100 | 100 |
| returned neighbor edges | 20,765 | 20,765 | 20,765 |
| scan_edges | 355,185,382 | 355,185,382 | 355,185,382 |
| get_neighbors elapsed | 3,496 ms | 45 ms | 19 ms |
| get_neighbors QPS | 28.60 | 2,222.22 | 5,263.16 |
| candidate L0 segments | 17,000 | 500 | 0 |
| matched L0 segments | 7,075 | 480 | 0 |
| body reads | 7,245 | 856 | 101 |
| read syscalls | 7,745 | 1,618 | 105 |

### SF10 结论

```text
Graph-aware L0-only 相比 naive L0：
  QPS 28.60 -> 2222.22
  提升约 77.7x
  candidate L0 segments 下降 97.1%
  matched L0 segments 下降 93.2%
  body reads 下降 88.2%

L0->L1 compact 相比 naive L0：
  QPS 28.60 -> 5263.16
  提升约 184.0x

Graph-aware L0-only 达到 L1 compact baseline 的约 42.2% QPS。
```

日志：

```text
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf10-naive-l0-edge1-samples100-20260530.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf10-graphaware-l0-edge1-samples100-20260530.json
/data/WorkSpace/lsmgraph-rs/logs/storage-bench-sf10-l1-edge1-samples100-20260530.json
```

---

## 6. Java LDBC SF10 Benchmark

Server store：

```text
/data/WorkSpace/lsmgraph-rs/store/sf10-graph-aware-l0-codex-20260530b
```

配置：

```text
scale_factor: 10
thread_count: 1
warmup: 100
operation_count: 250
IC1-IC14: enabled
IS1-IS7: enabled
IU1-IU8: enabled
```

Server 启动：

```text
engine open: 0.0s
SNB cache load: 285.2s
HTTP bind: 127.0.0.1:9090
```

Warmup：

```text
operations: 100
duration: 1.149s
throughput: 87.03 op/s
```

Run：

```text
operations: 265
duration: 5.387s
throughput: 49.19 op/s
```

Schedule audit：

```text
Maven: BUILD SUCCESS
Driver: FAILED SCHEDULE AUDIT
Late Count: 17
Tolerated Late Count: 13
```

慢查询摘要：

| Operation | Count | Mean | Max |
|---|---:|---:|---:|
| IC5 | 1 | 1177 ms | 1177 ms |
| IC12 | 2 | 1001 ms | 1210 ms |
| IC14 | 1 | 717 ms | 717 ms |
| IC10 | 1 | 282 ms | 282 ms |
| IC13 | 3 | 85 ms | 200 ms |
| IC2 | 2 | 56.5 ms | 68 ms |

短查询与更新摘要：

```text
IS1 mean: 1.25ms
IS2 mean: 8.64ms
IS3 mean: 3.46ms
IS4 mean: 1.70ms
IS5 mean: 2.57ms
IS6 mean: 1.52ms
IS7 mean: 1.30ms

IU2 mean: 30.67ms
IU3 mean: 38.30ms
IU5 mean: 34.71ms
IU7 mean: 29.58ms
IU8 mean: 26.00ms
```

Server metrics：

```text
HTTP 2xx: 366
HTTP 4xx: 0
HTTP 5xx: 0

vertex_lookups: 2,085,997
edge_prop_lookups: 727,512
adjacency_lookups: 1,662,585
neighbor_clone_items: 14,277,768
```

日志：

```text
/data/WorkSpace/lsmgraph-rs/logs/sf10-java-benchmark-20260530-graphaware-l0-driver.log
/data/WorkSpace/lsmgraph-rs/logs/sf10-java-benchmark-20260530-graphaware-l0-server.log
/data/WorkSpace/lsmgraph-rs/logs/sf10-java-benchmark-20260530-graphaware-l0-server-metrics.json
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/results/LDBC-SNB-DGS-SF10-results.json
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/results/LDBC-SNB-DGS-SF10-validation.json
```

---

## 7. Java LDBC SF30 Benchmark

Run date:
```text
2026-06-01
```

Server store:
```text
/data/WorkSpace/lsmgraph-rs/store/sf30-bench
```

Run mode:
```text
SF30 driver-only rerun.
Skipped BaseGraph build and storage-bench because both had already completed.
RUN_SF10=false
RUN_SF30=true
RUN_BASE_BUILD=false
RUN_STORAGE_BENCH=false
RUN_DRIVER=true
```

Progress/logging output:
```text
/data/WorkSpace/lsmgraph-rs/logs/benchmarks-codex-20260601_sf30_driver_progress2/progress.log
/data/WorkSpace/lsmgraph-rs/logs/benchmarks-codex-20260601_sf30_driver_progress2/phase.current
/data/WorkSpace/lsmgraph-rs/logs/benchmarks-codex-20260601_sf30_driver_progress2/sf30-server.log
/data/WorkSpace/lsmgraph-rs/logs/benchmarks-codex-20260601_sf30_driver_progress2/sf30-driver.log
/data/WorkSpace/lsmgraph-rs/logs/benchmarks-codex-20260601_sf30_driver_progress2/sf30-driver.time.txt
```

Server startup:
```text
engine open: 0.0s
SNB vertices/properties/adjacency cache load: 910.0s
HTTP bind: 127.0.0.1:9090
```

Driver:
```text
properties: interactive-benchmark-sf30.properties
elapsed wall time: 54.54s
exit status: 0
Maven: BUILD SUCCESS
```

Throughput:
```text
warmup throughput: 86.96 op/s
benchmark throughput: 17.57 op/s
```

Resource notes:
```text
driver max RSS: 1,043,204 KB
driver major page faults: 127
driver filesystem inputs: 66,336
server process reached about 100GB RSS while loading the SF30 SNB adjacency cache
```

Related SF30 BaseGraph / IO results from the earlier interrupted full run:
```text
BaseGraph build output:
  /data/WorkSpace/lsmgraph-rs/store/sf30-base-graph/base_graph

BaseGraph build elapsed:
  6m25.06s

BaseGraph size:
  5.6G

Legacy server store size:
  /data/WorkSpace/lsmgraph-rs/store/sf30-bench = 112G

SF30 IO blocking:
  sampled_vertices: 5000
  scan_edges: 1,087,848,423
  scan_elapsed_ms: 1,055,754
  get_neighbors_elapsed_ms: 136,879

SF30 IO direct:
  sampled_vertices: 5000
  scan_edges: 1,087,848,423
  scan_elapsed_ms: 1,143,893
  get_neighbors_elapsed_ms: 191,743
```

SF30 conclusion:
```text
SF30 can run end-to-end through the Java LDBC driver, but performance is not good yet.
The dominant observed cost is the legacy SNB adjacency cache load path:
  SF30 server cold start: about 15m10s
  SF30 driver benchmark itself: about 54.5s

The measured benchmark throughput is 17.57 op/s, lower than SF10.
This should not be treated as acceptable SF30 performance.
The next optimization target should be server startup/cache loading and query-path work
that still goes through the legacy SNB adjacency cache rather than the new BaseGraph path.
```

---

## 8. 总体结论

### 8.1 Graph-aware L0 是有效的

SF1 和 SF10 都说明：

```text
schema-aware partition + typed lookup 可以显著降低 L0 candidate segment 数。
即使 graph-aware L0-only 没有 compact 到 L1，也能显著提升 typed get_neighbors。
```

特别是 SF10：

```text
naive L0: 170 个 L0 run
graph-aware L0-only: 376 个 L0 segment

虽然 graph-aware segment 更多，但 typed 查询只看相关 partition，
candidate L0 segments 从 17,000 降到 500。
```

这说明研究点不是“文件越少越好”这么简单，而是：

```text
查询语义相关的候选 segment 越少越好。
```

### 8.2 L0->L1 compact 仍然是强 baseline

L0->L1 直接消除 L0 探测，所以点查性能最好：

```text
SF1 L1 compact QPS: 7692.31
SF10 L1 compact QPS: 5263.16
```

但 compact 有后台写放大和资源消耗，因此 graph-aware L0 的价值在于：

```text
在数据尚未 compact 前，先把热点 typed 查询的 L0 读放大控制住。
```

### 8.3 Java LDBC benchmark 和 storage microbench 不能混为一谈

当前 Java LDBC benchmark：

```text
主要走 SNB adjacency cache。
CSR/storage metrics 基本为 0。
```

所以：

```text
Java LDBC benchmark 用来看端到端流程、HTTP adapter、SNB 查询实现、IC/IS/IU 调度压力。
storage-bench 用来看 LSMGraph/CSR 的真实读放大和 compaction/index 效果。
```

---

## 9. 下一步建议

1. 扩大 storage-bench relation 覆盖：

```text
edge_type=1  Person-Knows
edge_type=2  HasCreator
edge_type=3  HasTag
edge_type=6  LikesPost
edge_type=7  LikesComment
```

2. 做 RA-score targeted compaction 的 SF10 实验：

```text
让系统根据 query_count、candidate segments、offset miss rate 自动选择 hot partition/range compact。
```

3. 优化 Java LDBC benchmark 的复杂查询：

```text
重点看 IC5、IC12、IC14。
这些是导致 SF10 schedule audit fail 的主要原因。
```

4. 把 Java LDBC benchmark 拆成三组：

```text
IC only
IS only
IU only
```

这样可以更清楚地区分复杂查询、短查询和更新路径的瓶颈。
