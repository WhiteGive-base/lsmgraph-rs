# P20 远端 artifact prep：命令、门槛与 ETA

本文只覆盖正式 timing 之前的输入准备和 correctness-only 阶段。当前不应直接执行末尾 driver；property materializer 是硬阻塞。

## 0. 硬阻塞：先补 property materializer

不能只把当前 import 的 `SNB_SKIP_ADJ_CACHE` 去掉。安全的最小实现应：

1. 在独立 SF1 store 注册一个低 ID、固定宽度、属于选定 edge type 的 property（建议继续冻结 `PROPERTY_ID=5`，但必须在 schema receipt 中证明 owner/type/encoding）。
2. 从 CSV 或受 hash 保护的 `snb_edge_props.jsonl` 读取选定 edge type 的真实 property rows。
3. 通过 `insert_edge_with_property_values_prototype` 写入同一 `(src,dst,edge_type)` 的新可见版本；不得只写旁路 JSONL。
4. reopen 后 `schema-show` 必须显示非空 property catalog；brute-force/reference 必须证明 presence query 有正例、0 mismatch。
5. SF1 smoke 必须覆盖：正例、负例、reopen、A0--A6 digest 一致、compaction 后仍一致；任何全零 truth 都失败。
6. 通过 SF1 后，只建一个 SF10 semantic-budgeted/B64 pristine store；完成后 quiesce、关闭所有持有 store 的进程，再做 full inventory。

建议 materializer 直接读取外部临时 property stream，避免把巨大的 `snb_edge_props.jsonl` 留在 pristine store 内并在后续 21/63 次 clone 中反复复制。

预计：Rust materializer + 单测/negative tests 约 3--6 h；SF1 smoke 10--30 min。SF10 物化本身先按 20--50 min、额外 15--35 GiB 预留，必须由 SF1/小 SF10 实测校准。旧 W8 的约 60 min 是 SF30 三类大型旁路 artifact 路径，不能当作“CSR property 已物化”的证据。

## 1. 同步 staging（待主分支决定合入位置后）

目标示例：

```bash
P20_PREP=/data/WorkSpace/lsmgraph-cidr-integration/cidr-experiments/runners/p20-prep
```

同步后先执行轻量测试：

```bash
python3 -m py_compile \
  "$P20_PREP/build_pristine_inventory.py" \
  "$P20_PREP/build_workload_truth.py" \
  "$P20_PREP/prepare_correctness.py"
python3 -m unittest discover -s "$P20_PREP" -p 'test_*.py' -v
```

预计 2--5 s；必须在服务器的 Python 3.8 上再跑一次。

## 2. 构建 P20 full pristine inventory

以下变量在 property store 封板后才可填写：

```bash
BIN=/data/WorkSpace/lsmgraph-cidr-integration/target/release/lsmgraph
DATASET=/data/WorkSpace/ldbc-sf10/social_network
DATASET_SHA=baa7c4b9701936253ebaeb4b3436c16403373af0b48c4276dacef780479952c5
PRISTINE=/data/WorkSpace/lsmgraph-rs/store/P20_SF10_PROPERTY_B64_PRISTINE
INPUT_RUN=/data/WorkSpace/results/P20-PREP/INPUT_RUN_ID
mkdir -p "$INPUT_RUN"
BIN_SHA=$(sha256sum "$BIN" | awk '{print $1}')
python3 "$P20_PREP/build_pristine_inventory.py" \
  --store "$PRISTINE" \
  --output "$INPUT_RUN/pristine-store-manifest.json" \
  --scale sf10 \
  --dataset-sha256 "$DATASET_SHA" \
  --binary-sha256 "$BIN_SHA"
sha256sum "$INPUT_RUN/pristine-store-manifest.json"
```

现有 14.3 GB tree 的校准 full hash 是 16.81 s；新的 30--50 GB property store 先估 0.5--2 min。期间 store 必须无人打开/修改；builder 会 fail closed 检测 identity drift。

## 3. 三个 trusted reference outputs 与 truth

reference output 必须满足：`warmup_runs=0`、`repeats=1`、`emit_result_digests=true`、`scan_requested=false`、绑定绝对 sample-plan path，并来自单独审计的 reference/oracle 流程。

typed 与 degree 可以复用现有 9000-query W6 plan；property 必须生成独立的 positive-result-aware plan，path/SHA 不能与前两者相同，并且所有 `entries[].edge_type=null`，否则与 typed one-hop 共线。冻结后逐个预验：

```bash
python3 "$P20_PREP/build_workload_truth.py" \
  --output "$INPUT_RUN/truth/typed-one-hop.json" \
  --scale sf10 --workload typed-one-hop --property-id 0 \
  --sample-plan /data/WorkSpace/lsmgraph-rs/remote-logs/w6-sf10-priority-20260709/sample-plan-core-s1000.json \
  --sample-plan-sha256 61f3bd9f391c0a056f76d60c228a44801d00265b8f8798c495e127fbfc424d0c \
  --reference-output "$INPUT_RUN/reference/typed-one-hop.json" \
  --reference-output-sha256 "$(sha256sum "$INPUT_RUN/reference/typed-one-hop.json" | awk '{print $1}')" \
  --reference-stage A0
```

degree 命令只需把 workload/output/reference 改为 `degree-stratified`；property 使用独立 property plan、`--workload property-presence --property-id 5`。任一 workload 没有正例时 builder 返回 2 且不发布 truth。

预计三个 9000-query reference + truth validation 为 2--10 min；property oracle 若需要 brute-force 扫描，另计 10--30 min。

### Property stratified plan v2

旧的“直接把 candidate result 路径交给 selector”流程已废弃。必须先运行 `generate_property_candidate_pool.py`：它固定 300,000-source exact `storage-bench` argv，清除两个 skip 环境变量，对 store 做前后 full-content inventory，并绑定 binary、typed plan、candidate plan/result、exit code 和所有 SHA。随后 `build_property_stratified_plan.py` 只接受 generation receipt。

最终 plan 固定 1,000 个唯一 source、500 mixed、500 zero，seed=`20260722`，至少两类 degree。plan receipt 永远 `performance_eligible=false`；只有 SF10 generation receipt 验证了正式 pristine inventory manifest 后才允许 `formal_plan_eligible=true` 与 `downstream_input_eligible=true`。SF1 始终 `calibration_only`。精确命令见 `PROPERTY-STRATIFIED-PLAN-CN.md`。

## 4. A0--A6 correctness raw 与 PASS

复制 `config-sf10.template.json` 为一个新文件，替换所有 `REPLACE_*`、`INPUT_RUN_ID` 和 pristine path；模板保留非法 SHA 占位符，未完成替换时会立即失败。

```bash
CONFIG="$INPUT_RUN/config.sf10.json"
CORRECTNESS_RUN=/data/WorkSpace/results/P20-PREP/CORRECTNESS_RUN_ID
WORK_ROOT=/data/WorkSpace/results/P20-PREP-WORK/CORRECTNESS_RUN_ID
mkdir -p "$WORK_ROOT"
python3 "$P20_PREP/prepare_correctness.py" \
  --config "$CONFIG" \
  --output-root "$CORRECTNESS_RUN" \
  --work-root "$WORK_ROOT" \
  --cleanup-clones
```

driver 的固定行为：

- 输出/work root 必须分别为 absent/empty，且不能与 pristine 重叠。
- 开跑前重新 full-hash pristine。
- 结束前再次 full-hash pristine；前后任一内容漂移都不签发 PASS。
- 三 workload 必须齐全；每个 stage/workload 都从独立新 clone 开始。
- 只解析 canonical `correctness` profile，强制 `performance_eligible=false`。
- profiles、validator、correctness builder 都由配置中的 SHA-256 固定；plan/reference 会复制进 run 自有的 `frozen-inputs/` 后再供 21-cell 使用。
- 清除 `SNB_SKIP_SEM_INDEX`、`SNB_SKIP_ADJ_CACHE`。
- 每个 raw 的 sample/entry digest 必须等于冻结 truth，随后才调用 integrated `build_correctness_pass.py`。
- 失败时不写总 PASS，保留 raw 和失败 clone；`--cleanup-clones` 只删除已经通过 digest gate、且确认是 work-root 直接子目录的 clone。

ext4 没有 reflink，严格 21-cell correctness 会复制 21 次 pristine。按 30--50 GB store 估算 copy traffic 630--1050 GB；加 A4/A5 compaction，预计 1--3 h。若实际 property store 仍约 15--20 GB，可降到约 40--100 min。不要和正式 timing 并发；这是 correctness-only 前置，不产生论文性能点。

最终门槛：

```bash
test -f "$CORRECTNESS_RUN/PASS"
test ! -f "$CORRECTNESS_RUN/P20-PREP-FAILED.json"
python3 -m json.tool "$CORRECTNESS_RUN/summary.json" >/dev/null
find "$CORRECTNESS_RUN/raw" -name PASS.json | wc -l   # SF10 必须是 21
```

只有三份 correctness PASS、21/21 raw PASS、full inventory 和 property positive-count gate 全部成立，才能开始 P20 formal timing。
