# P02A：共享负载下的 correctness-only smoke

本目录只允许生成 PASS/FAIL 正确性证据。所有 attempt 必须写入：

```json
{"performance_eligible":false,"correctness_only":true,"timing_use":"forbidden","reason":"shared-server-load"}
```

当前 runner 不识别 `PERFORMANCE_ELIGIBLE` 环境变量，因此必须保留上述 `run-classification.json` sidecar 并纳入 artifact SHA-256 manifest。任何 wall time、latency、CPU、RSS 或 I/O 数值都不得进入论文性能图。

## W13 schema-evolution 10-test

```bash
cd /data/WorkSpace/lsmgraph-rs
run_id="P02-W13-CORRECTNESS-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
raw_dir="$PWD/cidr-experiments/runs/P02-SF1-CORRECTNESS-GATE/raw/$run_id"
mkdir -p "$raw_dir"
printf '%s\n' '{"performance_eligible":false,"correctness_only":true,"timing_use":"forbidden","reason":"shared-server-load"}' > "$raw_dir/run-classification.json"
nice -n 19 ionice -c3 env ROOT="$PWD" RUN_ID="$run_id" LOG_ROOT="$raw_dir" CARGO_TARGET_DIR="$PWD/target" MIN_FREE_GIB=200 MIN_MEM_GIB=80 TEST_TIMEOUT_SECONDS=1800 bash baseline/run_w13_schema_evolution_20260613.sh
```

成功 gate：进程退出 0、`DONE` 存在、`tests.tsv` 恰好 10 项且全部 `pass`，每项日志和 sidecar 均进入 manifest。`FAILED` 缺失不能单独证明成功。

## W6 SF1 九变体 neighbor-compare

```bash
cd /data/WorkSpace/lsmgraph-rs
run_id="P02-W6-SF1-CORRECTNESS-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
raw_dir="$PWD/cidr-experiments/runs/P02-SF1-CORRECTNESS-GATE/raw/$run_id"
store_root="$PWD/store/$run_id"
mkdir -p "$raw_dir"
printf '%s\n' '{"performance_eligible":false,"correctness_only":true,"timing_use":"forbidden","reason":"shared-server-load"}' > "$raw_dir/run-classification.json"
nice -n 19 ionice -c3 env ROOT="$PWD" SCALE=sf1 DRY_RUN=1 SAMPLES=20 REPEATS=1 RUN_COMPARE=1 KEEP_STORES=0 RUN_ID="$run_id" OUT_DIR="$raw_dir" STORE_ROOT="$store_root" INPUT=/data/WorkSpace/ldbc-sf1/social_network BIN="$PWD/target/release/lsmgraph" IO_BACKEND=blocking MEMGRAPH_BYTES=67108864 EDGE_TYPES=1,2,3,7,8,9,10,11,12 IMPORT_TIMEOUT=30m MIN_FREE_GIB=200 MIN_MEM_GIB=80 bash baseline/run_w6_sf100_matrix_20260613.sh
```

`DRY_RUN=1` 仍会真实 import 九个 variants，并执行八组 naive-vs-variant compare；它只把 sample 数降为 20。成功 gate：进程退出 0、`DONE` 存在、八个 compare JSON 均 `checked=180`、`mismatches=0`。不得仅凭 `DONE` 或非正式 bench 输出判定成功。

审计时 SHA-256：

- W13 runner：`96f80133dc37f40d13e94e6f068b88c5d94d154c56398875260c0cb76ccc1930`
- W6 runner：`ce51846cc886a012f01ad01969b621cbbceb9dd369e2f04c4defc19c1b49a3f5`
- `target/release/lsmgraph`：`bbf13639a0ebc46b06c3a7c3f39185d452e59be3940a9bb27f5d447571a57687`（启动时仍须重新记录）
