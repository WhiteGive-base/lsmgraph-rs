# P20 SNB property materializer（candidate）

## 冻结语义

- profile：`snb-knows-creation-date-v1`
- source：`dynamic/person_knows_person_0_0.csv` 第三列 `creationDate`
- property：`id=5`（CLI 可显式改低于 64 的 ID）、owner=`EdgeLabel::Knows(+1)`、logical=`datetime_ms`、physical=`plain_i64`、encoding version=`1`、missing/default=`null`
- 写入：两个正向 Knows 方向直接调用 `insert_edge_with_property_values_prototype`；导入器生成的 `-1` reverse index 和所有其它 relation 均为 absent/null。
- 计数：receipt 的 `materialized_property_values` 是每个 store 的源派生正向边数，不乘 Engine/store 数。

它没有把 JSONL sidecar 当作 Engine property，也没有人工制造稀疏 marker。由于 LDBC Knows 的 `creationDate` 对每条 Knows 都存在，单个 source 上 property-only 结果会与 typed-Knows 结果相关。正式 P20 的边界是：property workload 的 query signature **只给 property constraint**，sample-plan 的每个 `entry.edge_type` 必须为 `null`；若同时限定 `edge_type=1`，则与 typed one-hop 共线，gate 必须失败。

## Fail-closed gate

opt-in import 只有同时满足以下条件才返回 receipt：

1. fresh store、空 property catalog、`property_id < 64`；
2. CSV header 第三列严格为 `creationDate`，首行 ID/value 可解析为 i64；
3. materialized count > 0 且 topology-only count > 0；
4. 每个 store 都有 property bitmap 非零 segment 和 bitmap 为零 segment；
5. catalog owner/type/encoding/default 精确匹配；
6. multi-engine 任一步失败时整批 candidate invalid，所有 store 必须丢弃，不能复用部分成功 store。

默认（不传 opt-in flag）的 `import`/`import-many` 仍走原函数，单 store 的一行三字段输出及 multi-store 的三字段 JSON shape 不变。只有 opt-in 才增加 `property_materialization` receipt。

## SF1 smoke 命令

以下命令只用于独立临时 store；路径必须不存在。不要直接指向现有正式 store。

```bash
CANDIDATE=/data/WorkSpace/lsmgraph-rs-p20-materializer
BIN="$CANDIDATE/target/release/lsmgraph"
DATA=/data/WorkSpace/dgs/data/social_network_tugraph
RUN=/data/WorkSpace/results/P20-PROPERTY-SF1/$(date -u +%Y%m%dT%H%M%SZ)
STORE="$RUN/store"
mkdir -p "$RUN"
test ! -e "$STORE"

SNB_SKIP_ADJ_CACHE=1 "$BIN" import \
  --input "$DATA" --data-dir "$STORE" --relation snb-full \
  --l0-layout semantic-budgeted --memgraph-bytes 67108864 \
  --materialize-knows-creation-date \
  --knows-creation-date-property-id 5 \
  >"$RUN/import.json" 2>"$RUN/import.log"

# 新进程 reopen：catalog + zero/nonzero bitmap 双门。
"$BIN" snb-property-audit --data-dir "$STORE" --property-id 5 \
  >"$RUN/reopen-audit.json"

# property-only plan：故意不传 --edge-type/--edge-types，因此 entry.edge_type=null。
"$BIN" storage-bench --data-dir "$STORE" --samples 1000 \
  --warmup-runs 0 --repeats 1 --sample-plan-degree-hint \
  --sample-plan-out "$RUN/property-plan.json" \
  >"$RUN/property-plan-generation.json"
"$BIN" storage-bench --data-dir "$STORE" \
  --sample-plan-in "$RUN/property-plan.json" \
  --warmup-runs 0 --repeats 1 --workload-mode one-hop \
  --property-predicate-mode presence --property-id 5 --emit-result-digests \
  >"$RUN/property-result.json"

# 独立 typed plan；不能复用 property plan。
"$BIN" storage-bench --data-dir "$STORE" --samples 1000 --edge-type 1 \
  --warmup-runs 0 --repeats 1 --sample-plan-degree-hint \
  --sample-plan-out "$RUN/typed-plan.json" \
  >"$RUN/typed-plan-generation.json"
"$BIN" storage-bench --data-dir "$STORE" \
  --sample-plan-in "$RUN/typed-plan.json" \
  --warmup-runs 0 --repeats 1 --workload-mode one-hop \
  --property-predicate-mode none --property-id 0 --emit-result-digests \
  >"$RUN/typed-result.json"

python3 "$CANDIDATE/cidr-experiments/runners/p20/validate_sf1_property_smoke.py" \
  --import-receipt "$RUN/import.json" \
  --reopen-audit "$RUN/reopen-audit.json" \
  --property-plan "$RUN/property-plan.json" \
  --property-result "$RUN/property-result.json" \
  --typed-plan "$RUN/typed-plan.json" \
  --typed-result "$RUN/typed-result.json" \
  --property-id 5 --output "$RUN/PASS.json"
```

validator 还要求 property workload 同时出现：positive query、zero-result negative query、同一 source 内 present+absent records、positive/absent record 总数，以及与 typed workload 不同的 plan SHA 和结果分布。仅 `positive_result_samples>0` 不足以 PASS。

SF1 gate 只验证实现和正确性，输出会固定标注 `sample_plan_role=smoke_only`、`performance_eligible=false`、`formal_plan_eligible=false`。本次冻结 smoke 的 positive rate 是 `3/1000=0.003`；该 plan 和结果绝不能复用为论文性能数据。正式 SF10 必须另建 property-aware stratified plan，预注册足量 positive/mixed/zero 样本阈值；这不属于本 materializer commit 的范围。

## 时间与磁盘（SF1 先校准）

- 编译和聚焦测试：约 2–8 分钟（取决于 cargo cache）。
- SF1 property import：初估 10–30 分钟；额外 property payload/metadata 先预留 3–10 GiB。
- 两个 1000-query smoke + reopen audit：约 2–10 分钟。
- 总计初估 15–50 分钟。只有 SF1 receipt、reopen、分布 gate 和 compaction/reopen 回归全过，才能据此估算 SF10；本 candidate 不启动 SF10。

任何 CSV/schema/count/bitmap/digest/plan 约束失败都不得生成 PASS；失败 store 不可修补后继续用，必须从不存在的新路径重建。
