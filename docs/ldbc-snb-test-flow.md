# LDBC SNB Test Flow

本文档记录完整测试顺序：先用 SF1 做 validate，再跑 SF10 和 SF30 benchmark，最后做 metric 与 I/O 测试采集。

命令默认在 Linux 测试机运行，工程目录为：

```bash
cd /data/WorkSpace/lsmgraph-rs
```

当前 Windows 工作区 `E:\文档\DGS项目复现\lsmgraph-rs` 是代码镜像；正式 benchmark 使用下面的 Linux `/data/WorkSpace/...` 数据路径。

## 1. 环境与路径

公共路径：

| 项目 | 路径 |
| --- | --- |
| LSMGraph 工程 | `/data/WorkSpace/lsmgraph-rs` |
| LSMGraph wrapper 脚本 | `/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph` |
| Java LDBC/DGS driver | `/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs` |
| HTTP adapter | `127.0.0.1:9090` |
| benchmark 输出目录 | `/data/WorkSpace/lsmgraph-rs/logs/<run-id>` |

数据与 store 路径：

| Scale | 原始图数据 | 参数/验证数据 | Store |
| --- | --- | --- | --- |
| SF1 validate | `/data/WorkSpace/ldbc-sf1/social_network` | `/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv` | `/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation` |
| SF1 benchmark 参数 | `/data/WorkSpace/ldbc-sf1/social_network` | `/data/WorkSpace/dgs/data/substitution_parameters_tugraph` | 按需要复用 SF1 validate store |
| SF10 benchmark | `/data/WorkSpace/ldbc-sf10/social_network` | `/data/WorkSpace/ldbc-sf10/substitution_parameters` | `/data/WorkSpace/lsmgraph-rs/store/sf10-base-graph` |
| SF10 storage I/O | `/data/WorkSpace/ldbc-sf10/social_network` | 同上 | `/data/WorkSpace/lsmgraph-rs/store/sf10-base-dynamic` |
| SF30 benchmark | `/data/WorkSpace/ldbc-sf30/social_network` | `/data/WorkSpace/ldbc-sf30/substitution_parameters` | `/data/WorkSpace/lsmgraph-rs/store/sf30-base-graph` |
| SF30 storage I/O | `/data/WorkSpace/ldbc-sf30/social_network` | 同上 | `/data/WorkSpace/lsmgraph-rs/store/sf30-base-dynamic` |

预检查：

```bash
cd /data/WorkSpace/lsmgraph-rs

test -x deps/ldbc_snb_interactive_impls/dgs/run.sh
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-validate.properties
test -f deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-benchmark-sf10.properties
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-benchmark-sf30.properties
```

如果 `interactive-validate.properties`、`validation_params_tugraph.csv` 或 `run.sh` 缺失，先从原始 DGS/LDBC driver 目录补齐；benchmark properties 已在当前仓库的 `dgs` 目录下。

构建 release binary：

```bash
cargo build --release --features direct-io,uring
```

## 2. SF1 Validate

SF1 validate 必须使用干净 store。IU 更新会改变 store 状态，因此每次正式 validate 前都要重建。
`prepare_validation_store.sh` 构建当前正式查询路径使用的 BaseGraph validation store。

```bash
cd /data/WorkSpace/lsmgraph-rs

FORCE=1 \
INPUT=/data/WorkSpace/ldbc-sf1/social_network \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

先跑一个 Rust 侧 mixed validate 烟雾测试：

```bash
target/release/lsmgraph snb-validate-mixed \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  --validation-params /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv \
  --max-lines 500
```

正式 SF1 validate 走 Java LDBC driver。终端 A 启动 LSMGraph HTTP adapter：

```bash
cd /data/WorkSpace/lsmgraph-rs

IO_BACKEND=blocking \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
HOST=127.0.0.1 \
PORT=9090 \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

终端 B 运行 driver validate：

```bash
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs

bash run.sh interactive-validate.properties
```

验收标准：driver 输出 validation 通过，无 incorrect operation；服务端日志不出现 query/update 处理错误。

## 3. SF10 Benchmark

构建 SF10 BaseGraph store：

```bash
cd /data/WorkSpace/lsmgraph-rs

target/release/lsmgraph base-build \
  --input /data/WorkSpace/ldbc-sf10/social_network \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf10-base-graph

target/release/lsmgraph dynamic-stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf10-base-graph
```

终端 A 启动 SF10 server：

```bash
cd /data/WorkSpace/lsmgraph-rs

IO_BACKEND=blocking \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf10-base-graph \
HOST=127.0.0.1 \
PORT=9090 \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

终端 B 跑 SF10 benchmark：

```bash
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs

bash run.sh interactive-benchmark-sf10.properties
```

`interactive-benchmark-sf10.properties` 里关键路径：

```text
ldbc.snb.interactive.scale_factor=10
ldbc.snb.interactive.updates_dir=/data/WorkSpace/ldbc-sf10/social_network
ldbc.snb.interactive.parameters_dir=/data/WorkSpace/ldbc-sf10/substitution_parameters
warmup=100
operation_count=250
thread_count=1
```

## 4. SF30 Benchmark

构建 SF30 BaseGraph store：

```bash
cd /data/WorkSpace/lsmgraph-rs

target/release/lsmgraph base-build \
  --input /data/WorkSpace/ldbc-sf30/social_network \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf30-base-graph

target/release/lsmgraph dynamic-stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf30-base-graph
```

终端 A 启动 SF30 server：

```bash
cd /data/WorkSpace/lsmgraph-rs

IO_BACKEND=blocking \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf30-base-graph \
HOST=127.0.0.1 \
PORT=9090 \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

终端 B 跑 SF30 benchmark：

```bash
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs

bash run.sh interactive-benchmark-sf30.properties
```

`interactive-benchmark-sf30.properties` 里关键路径：

```text
ldbc.snb.interactive.scale_factor=30
ldbc.snb.interactive.updates_dir=/data/WorkSpace/ldbc-sf30/social_network
ldbc.snb.interactive.parameters_dir=/data/WorkSpace/ldbc-sf30/substitution_parameters
warmup=100
operation_count=250
thread_count=1
```

## 5. 一键 Benchmark 脚本

如果要把 SF10、SF30、storage I/O 和 driver benchmark 串起来跑，用：

```bash
cd /data/WorkSpace/lsmgraph-rs

OUT_DIR=/data/WorkSpace/lsmgraph-rs/logs/benchmarks-sf10-sf30-$(date +%Y%m%d_%H%M%S) \
RUN_SF10=true \
RUN_SF30=true \
RUN_STORAGE_BENCH=true \
RUN_DRIVER=true \
STORAGE_SAMPLES=5000 \
SF10_INPUT=/data/WorkSpace/ldbc-sf10/social_network \
SF10_BASE_STORE=/data/WorkSpace/lsmgraph-rs/store/sf10-base-graph \
SF10_LEGACY_STORE=/data/WorkSpace/lsmgraph-rs/store/sf10-base-dynamic \
SF30_INPUT=/data/WorkSpace/ldbc-sf30/social_network \
SF30_BASE_STORE=/data/WorkSpace/lsmgraph-rs/store/sf30-base-graph \
SF30_LEGACY_STORE=/data/WorkSpace/lsmgraph-rs/store/sf30-base-dynamic \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/run_linux_benchmarks.sh
```

主要输出：

| 文件 | 含义 |
| --- | --- |
| `$OUT_DIR/progress.log` | 全流程进度 |
| `$OUT_DIR/sf10-driver.log` | SF10 Java driver benchmark 日志 |
| `$OUT_DIR/sf30-driver.log` | SF30 Java driver benchmark 日志 |
| `$OUT_DIR/sf10-server.log` | SF10 server 日志 |
| `$OUT_DIR/sf30-server.log` | SF30 server 日志 |
| `$OUT_DIR/sf10-io-blocking.json` | SF10 blocking storage I/O metric |
| `$OUT_DIR/sf10-io-direct.json` | SF10 direct storage I/O metric |
| `$OUT_DIR/sf30-io-blocking.json` | SF30 blocking storage I/O metric |
| `$OUT_DIR/sf30-io-direct.json` | SF30 direct storage I/O metric |
| `$OUT_DIR/sf10-dynamic-stats.json` | SF10 BaseGraph/DynamicGraphView 统计 |
| `$OUT_DIR/sf30-dynamic-stats.json` | SF30 BaseGraph/DynamicGraphView 统计 |
| `$OUT_DIR/benchmark-sf100-estimate.md` | 由 SF10/SF30 结果生成的 SF100 粗估 |

## 6. Metric 与 I/O 测试

### 6.1 Server Metric

LSMGraph server 提供 `/metrics` 和 `/metrics/reset`。建议在 benchmark 前 reset，结束后抓取结果。

```bash
curl -s -X POST http://127.0.0.1:9090/metrics/reset

# 运行 SF10 或 SF30 benchmark 后：
curl -s http://127.0.0.1:9090/metrics \
  > /data/WorkSpace/lsmgraph-rs/logs/<run-id>-server-metrics.json
```

重点看这些字段：

```text
queries
updates
errors
read_lock_wait_us_avg
read_lock_wait_us_max
write_lock_wait_us_avg
write_lock_wait_us_max
query_exec_us_avg
query_exec_us_max
update_exec_us_avg
update_exec_us_max
```

### 6.2 Storage I/O Metric

`storage-bench.sh` 调用 `target/release/lsmgraph storage-bench`，适合测 LSM/delta store 的 scan 和 sampled neighbor read。这里使用 `*-base-dynamic` store，不使用纯 BaseGraph store。

SF10：

```bash
cd /data/WorkSpace/lsmgraph-rs

IO_BACKEND=blocking \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf10-base-dynamic \
SAMPLES=5000 \
OUT=/data/WorkSpace/lsmgraph-rs/logs/sf10-io-blocking.json \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/storage_bench.sh

IO_BACKEND=direct \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf10-base-dynamic \
SAMPLES=5000 \
OUT=/data/WorkSpace/lsmgraph-rs/logs/sf10-io-direct.json \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/storage_bench.sh
```

SF30：

```bash
cd /data/WorkSpace/lsmgraph-rs

IO_BACKEND=blocking \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf30-base-dynamic \
SAMPLES=5000 \
OUT=/data/WorkSpace/lsmgraph-rs/logs/sf30-io-blocking.json \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/storage_bench.sh

IO_BACKEND=direct \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf30-base-dynamic \
SAMPLES=5000 \
OUT=/data/WorkSpace/lsmgraph-rs/logs/sf30-io-direct.json \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/storage_bench.sh
```

输出 JSON 重点看：

```text
scan_edges
scan_elapsed_ms
sampled_vertices
neighbor_edges
get_neighbors_elapsed_ms
levels
metrics
```

### 6.3 系统级 I/O 观察

benchmark 阶段同步开系统监控：

```bash
iostat -dx 5
pidstat -d -r -u -p <server-pid> 5
```

记录项：

```text
iostat: r/s, rkB/s, await, %util
pidstat: RSS, minflt/s, majflt/s, kB_rd/s, kB_wr/s, CPU%
```

如果 repeated warm run 中 `rkB/s` 和 `kB_rd/s` 长时间为 0，说明本轮 driver benchmark 主要是 warm-cache / memory-resident，不代表 cold O_DIRECT 场景。

## 7. 执行顺序清单

1. `cargo build --release --features direct-io,uring`
2. `FORCE=1 ... prepare_validation_store.sh`
3. `target/release/lsmgraph snb-validate-mixed ... --max-lines 500`
4. 启动 SF1 server，执行 `bash run.sh interactive-validate.properties`
5. 构建 SF10 store，启动 SF10 server，执行 `bash run.sh interactive-benchmark-sf10.properties`
6. 抓取 SF10 `/metrics`，保存 driver/server 日志
7. 构建 SF30 store，启动 SF30 server，执行 `bash run.sh interactive-benchmark-sf30.properties`
8. 抓取 SF30 `/metrics`，保存 driver/server 日志
9. 对 SF10/SF30 的 `*-base-dynamic` store 分别跑 blocking/direct `storage_bench.sh`
10. 汇总 QPS、latency、server metrics、storage I/O JSON、`iostat`/`pidstat` 观察结果
