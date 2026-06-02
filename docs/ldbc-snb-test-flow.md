# LDBC SNB Test Flow

This document records the Linux test sequence for the BaseGraph/DynamicGraphView
path: SF1 validation first, then SF10/SF30/SF100 benchmarks, then server metrics
and BaseGraph storage I/O measurements.

All commands assume:

```bash
cd /data/WorkSpace/lsmgraph-rs
```

## 1. Paths

| Scale | Input | Parameters or validation data | Store |
| --- | --- | --- | --- |
| SF1 validate | `/data/WorkSpace/ldbc-sf1/social_network` | `deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv` | `/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation` |
| SF10 benchmark | `/data/WorkSpace/ldbc-sf10/social_network` | `/data/WorkSpace/ldbc-sf10/substitution_parameters` | `/data/WorkSpace/lsmgraph-rs/store/sf10-base-graph` |
| SF30 benchmark | `/data/WorkSpace/ldbc-sf30/social_network` | `/data/WorkSpace/ldbc-sf30/substitution_parameters` | `/data/WorkSpace/lsmgraph-rs/store/sf30-base-graph` |
| SF100 benchmark | `/data/WorkSpace/ldbc-sf100/social_network` | `/data/WorkSpace/ldbc-sf100/substitution_parameters` | `/data/WorkSpace/lsmgraph-rs/store/sf100-base-graph` |

Preflight:

```bash
test -x deps/ldbc_snb_interactive_impls/dgs/run.sh
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-validate.properties
test -f deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-benchmark-sf10.properties
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-benchmark-sf30.properties
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-benchmark-sf100.properties
test -f deps/ldbc_snb_interactive_impls/dgs/interactive-benchmark-sf100-smoke.properties
```

Build:

```bash
cargo build --release --features direct-io,uring
```

## 2. SF1 Validation

Prepare a clean BaseGraph validation store:

```bash
FORCE=1 \
INPUT=/data/WorkSpace/ldbc-sf1/social_network \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

Run a Rust mixed validation smoke:

```bash
target/release/lsmgraph snb-validate-mixed \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  --validation-params /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv \
  --max-lines 500
```

Run formal Java driver validation:

```bash
IO_BACKEND=blocking \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
HOST=127.0.0.1 \
PORT=9090 \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

In another terminal:

```bash
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs
bash run.sh interactive-validate.properties
```

Acceptance: driver completes with zero incorrect operations, and the server log has
no query/update handling errors.

## 3. BaseGraph Benchmarks

Manual build and stats example:

```bash
target/release/lsmgraph base-build \
  --input /data/WorkSpace/ldbc-sf30/social_network \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf30-base-graph

target/release/lsmgraph dynamic-stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf30-base-graph
```

Manual driver run example:

```bash
IO_BACKEND=direct \
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf30-base-graph \
HOST=127.0.0.1 \
PORT=9090 \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

In another terminal:

```bash
cd /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs
bash run.sh interactive-benchmark-sf30.properties
```

The SF100 properties use:

```text
ldbc.snb.interactive.scale_factor=100
ldbc.snb.interactive.updates_dir=/data/WorkSpace/ldbc-sf100/social_network
ldbc.snb.interactive.parameters_dir=/data/WorkSpace/ldbc-sf100/substitution_parameters
warmup=100
operation_count=250
thread_count=1
```

## 4. One-Shot Benchmark Script

The script now uses BaseGraph stores for SF10/SF30/SF100. It no longer creates or
reads legacy `snb-full` stores.

SF10 + SF30:

```bash
OUT_DIR=/data/WorkSpace/lsmgraph-rs/logs/benchmarks-sf10-sf30-$(date +%Y%m%d_%H%M%S) \
RUN_SF10=true \
RUN_SF30=true \
RUN_SF100=false \
RUN_STORAGE_BENCH=true \
RUN_DRIVER=true \
STORAGE_SAMPLES=5000 \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/run_linux_benchmarks.sh
```

SF100 after SF1 validation and SF10/SF30 smoke are clean:

```bash
OUT_DIR=/data/WorkSpace/lsmgraph-rs/logs/benchmarks-sf100-$(date +%Y%m%d_%H%M%S) \
RUN_SF10=false \
RUN_SF30=false \
RUN_SF100=true \
RUN_SF100_SMOKE=true \
RUN_STORAGE_BENCH=true \
RUN_DRIVER=true \
STORAGE_SAMPLES=5000 \
SF100_INPUT=/data/WorkSpace/ldbc-sf100/social_network \
SF100_BASE_STORE=/data/WorkSpace/lsmgraph-rs/store/sf100-base-graph \
  bash deps/ldbc_snb_interactive_impls/lsmgraph/run_linux_benchmarks.sh
```

Main artifacts:

| File | Meaning |
| --- | --- |
| `$OUT_DIR/progress.log` | Phase and process progress |
| `$OUT_DIR/sf*-base-build.json` | BaseGraph build result |
| `$OUT_DIR/sf*-dynamic-stats.json` | DynamicGraphView/BaseGraph stats |
| `$OUT_DIR/sf*-driver.log` | Java driver benchmark log |
| `$OUT_DIR/sf*-server.log` | HTTP adapter log |
| `$OUT_DIR/sf*-io-blocking.json` | BaseGraph blocking storage I/O bench |
| `$OUT_DIR/sf*-io-direct.json` | BaseGraph direct storage I/O bench |

## 5. Metrics And I/O

Server metrics:

```bash
curl -s -X POST http://127.0.0.1:9090/metrics/reset
curl -s http://127.0.0.1:9090/metrics \
  > /data/WorkSpace/lsmgraph-rs/logs/<run-id>-server-metrics.json
```

BaseGraph I/O bench:

```bash
target/release/lsmgraph --io-backend blocking base-storage-bench \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf30-base-graph \
  --samples 5000 \
  --prop-reads true

target/release/lsmgraph --io-backend direct base-storage-bench \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf30-base-graph \
  --samples 5000 \
  --prop-reads true
```

Key fields: `total_sampled_sources`, `total_neighbor_edges`, `total_prop_edges`,
per-CSR `elapsed_ms`, `read_chunks`, `read_items`, `cache_hits`, and
`cache_misses`.

System-level observation during long runs:

```bash
iostat -dx 5
pidstat -d -r -u -p <server-pid> 5
```

## 6. Execution Checklist

1. Build release binary with direct I/O support.
2. Rebuild SF1 validation store and run Rust mixed validation.
3. Run formal SF1 Java driver validation.
4. Run SF10 benchmark and BaseGraph I/O bench.
5. Run SF30 benchmark and BaseGraph I/O bench.
6. Build SF100 BaseGraph.
7. Run SF100 smoke benchmark.
8. Run SF100 formal benchmark.
9. Summarize QPS, latency, server metrics, BaseGraph I/O JSON, and system I/O observations.
