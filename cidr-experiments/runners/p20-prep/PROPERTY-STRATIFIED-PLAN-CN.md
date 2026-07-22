# P20 property-aware stratified plan：冻结口径与命令

本文只覆盖 P20 正式计时之前的 property plan 生成与验收，不运行 SF10，不产生论文 timing。generation wrapper 固定并执行现有 `storage-bench` 命令，selector 只接受受 SHA 保护的 generation receipt；两者都不修改 Engine。

## 冻结口径

- profile：`p20-balanced-property-diagnostic-v1`
- candidate pool 请求量：300,000 个唯一 source；候选 plan 必须由全邻接扫描生成，唯一 entry 使用 `edge_type=null`，记录 all-edge degree。
- property：`presence`、`property_id=5`、`workload_mode=one_hop`。
- 固定 seed：`20260722`。
- 确定性顺序：各 stratum 先按 `src` 稳定升序，再使用 `stable-src-then-sha256-counter-fisher-yates-v1` shuffle；选中后再做一次固定 stream 的确定性 shuffle。
- 最终恰好 1,000 个全局唯一 source：500 个 mixed-positive（`0 < property_count < all_edge_degree`）和 500 个 zero（`property_count=0` 且 `all_edge_degree>0`）。因此 `min_positive=500`、`min_mixed=500`、`min_zero=500`。
- 最终 plan 至少覆盖 low（1–16）、medium（17–1024）、high（>1024）中的两类。
- property 与 typed plan 的绝对路径和 SHA-256 都必须不同；property plan 的每个 entry 都必须是 `edge_type=null`。
- 这是人为平衡的 diagnostic，不估计自然 prevalence。receipt 固定记录 `sampling_design=balanced_diagnostic` 和 `natural_prevalence_claim=false`。
- SF1 只做 calibration，即使满足 500/500，也固定为 `formal_plan_eligible=false`、`performance_eligible=false`、`sample_plan_role=calibration_only`。
- plan artifact 本身在所有 scale 都固定 `performance_eligible=false`。SF10 只有在 generation wrapper 和 selector 都验证同一份 pristine inventory manifest（store path、scale、dataset SHA、binary SHA、inventory SHA，且 store full-content 前后无漂移）后，才允许 `formal_plan_eligible=true` 和 `downstream_input_eligible=true`。

任何阈值不足都会返回 2，stderr 给出 candidate pool 的实际 total/positive/mixed/zero/full-positive 分布，并且不创建 plan/receipt；禁止静默降低阈值。

## SF1 calibration 命令

以下命令复用已经通过 property smoke 的独立 SF1 store，只产生 calibration artifact，不产生 timing：

```bash
CANDIDATE=/data/WorkSpace/lsmgraph-cidr-p20-stratified-plan
BIN=/data/WorkSpace/lsmgraph-rs-p20-materializer/target/release/lsmgraph
STORE=/data/WorkSpace/results/P20-PROPERTY-SF1-SMOKE-20260722T055810Z/store
SNAPSHOT=34692699
TYPED_PLAN=/data/WorkSpace/results/P20-PROPERTY-SF1-SMOKE-20260722T055810Z/typed-plan.json
DATASET_SHA=REPLACE_WITH_VERIFIED_SF1_DATASET_SHA256
RUN=/data/WorkSpace/results/P20-PROPERTY-PLAN-SF1-CALIBRATION/$(date -u +%Y%m%dT%H%M%SZ)
test ! -e "$RUN"

python3 "$CANDIDATE/cidr-experiments/runners/p20-prep/generate_property_candidate_pool.py" \
  --scale sf1 --property-id 5 \
  --binary "$BIN" \
  --binary-sha256 "$(sha256sum "$BIN" | awk '{print $1}')" \
  --store "$STORE" --snapshot "$SNAPSHOT" \
  --dataset-sha256 "$DATASET_SHA" \
  --typed-plan "$TYPED_PLAN" \
  --typed-plan-sha256 "$(sha256sum "$TYPED_PLAN" | awk '{print $1}')" \
  --output-root "$RUN"

python3 "$CANDIDATE/cidr-experiments/runners/p20-prep/build_property_stratified_plan.py" \
  --generation-receipt "$RUN/generation-receipt.json" \
  --generation-receipt-sha256 "$(sha256sum "$RUN/generation-receipt.json" | awk '{print $1}')" \
  --output-plan "$RUN/property-balanced-plan.json" \
  --output-receipt "$RUN/property-balanced-plan.receipt.json"
```

wrapper 固定两条 exact argv：先以 300,000 samples 和 degree hint 生成 plan，再以同一 plan 重放 presence/property-id=5/result-digests。它会在运行前后对 store 做 full-content inventory；任一命令非零、binary/typed plan 漂移、store 内容漂移或输出已存在时都不会产生 `generation-receipt.json`。selector 不再接受可手工拼接的 candidate result、binary、store 或 scale 参数。

## 正式 SF10 前的配置门禁

SF10 以后单独生成新的 candidate/result/plan/receipt，不能复用 SF1 plan。`config-sf10.template.json` 已升级为 `p20-correctness-prep-config-v2`，property workload 必须显式固定：

```json
{
  "candidate_pool_requested": 300000,
  "expected_queries": 1000,
  "min_positive": 500,
  "min_mixed": 500,
  "min_zero": 500,
  "stratified_plan_receipt": "/absolute/path/property-plan.receipt.json",
  "stratified_plan_receipt_sha256": "64-lowercase-hex"
}
```

SF10 generation wrapper 还必须传递 `--inventory-manifest` 和 `--inventory-manifest-sha256`；其 manifest 必须与同一 binary/dataset/store 及 `sf10` 完全匹配。`prepare_correctness.py` 会重新验证 generation/plan 两份 receipt SHA、exact argv、candidate plan/result、typed plan、binary、pristine inventory、store snapshot、最终 plan path/SHA、1,000 个 source 的顺序与 strata、两类 degree coverage，并拒绝 symlink、输入漂移、重复 source 和任何阈值降级。

冻结 property truth 时必须显式传递同一组阈值：

```bash
python3 build_workload_truth.py \
  --output /absolute/path/property-truth.json \
  --scale sf10 --workload property-presence --property-id 5 \
  --sample-plan /absolute/path/property-balanced-plan.json \
  --sample-plan-sha256 PLAN_SHA \
  --reference-output /absolute/path/reference-property.json \
  --reference-output-sha256 REFERENCE_SHA \
  --reference-stage A0 \
  --expected-queries 1000 \
  --min-positive 500 --min-mixed 500 --min-zero 500
```

正式 reference 和 A0–A6 每个 observation 都必须继续满足 500 positive、500 mixed、500 zero；不能只验证 `positive>0`。
