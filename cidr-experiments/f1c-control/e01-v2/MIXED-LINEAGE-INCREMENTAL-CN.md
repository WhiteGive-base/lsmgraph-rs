# Figure 1 mixed-lineage 增量收口合同

本合同与原有 `E01 formal manifest v2` 严格隔离。它允许把已经完成的旧
18 个 L5 单元作为 `legacy_validated` 行引用，再补采 3 个
`SemL0-naive` repeat；另外运行 1 个当前 SemL0 bridge canary，只用于跨代
可比性门，不进入 Figure 1 的 21 行数据。

## 资格边界

- 旧根保持只读，不复制、不覆盖、不删除任何旧 receipt 或失败标记。
- 旧 18 行始终保持
  `formal_eligible=false`、`performance_eligible=false`、
  `paper_claim_eligible=false`，不得通过 composition 或 normalizer 追授
  lease/formal 资格。
- mixed-lineage 输出整体也保持上述三项资格为 `false`，并要求图注披露
  mixed lineage；它不是 strict-all-formal 21-cell campaign。
- Neo4j 使用 LDBC SF10 source tree，其余五个旧系统使用 dense typed-edge
  表示。合同通过共同 truth、共同 expected digest、零 mismatch/timeout、
  同 host/timing protocol 和 cardinality 约束逻辑数据身份，同时明确
  `physical_byte_identity_required=false`，不伪造两种物理输入的 byte
  equivalence。
- Neo4j/NebulaGraph 的 6 个 P31 单元引用冻结的原 validation、原 FAILED、
  conditional read-only revalidation 和 post-timing receipt；另外 12 个
  单元引用原 `validation.json` 与 `DONE`。两种方式都不改原 root。

`E01-mixed-lineage-plan-v1.json` 是真实旧 18/18 receipt 的冻结 HOLD 计划：

- 18 个 `legacy_validated` 单元；
- 12 个 direct P31 bridge、6 个 read-only post-timing P31 bridge；
- 每行 validated-result、adapter request/provenance、P31 bridge 和旧失败
  marker lineage 的 path/size/SHA；
- logical dataset identity 与逐系统 import lineage；
- 严格固定的 4 个新 timing cell：
  `seml0:bridge-canary`、`seml0-naive:r1/r2/r3`。

## 构建只读 HOLD 计划

构建器只哈希命令行显式引用的少量 JSON/marker 文件，不遍历数据集、store
或 results，不创建 formal root，也不启动 adapter/timing：

```bash
python3 build_e01_mixed_lineage.py \
  --source /data/WorkSpace/results/P10-L5-CONDITIONAL-22cb1e7-20260725/combined-l5-attempt1/L5-CONDITIONAL-COMBINED.json \
  --dataset-manifest /data/WorkSpace/results/P02B/sf10-dataset-manifest.json \
  --dense-conversion-summary /data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260624/livegraph/sf10-typed-neighbor/convert-summary.json \
  --created-at-utc 2026-07-27T10:30:00Z \
  --output /NEW/E01-mixed-lineage-plan-v1.json
```

输出默认 `state=HOLD`、`incremental_evidence=null`。已有目标文件会被拒绝。

## pre-output 门

`normalize_e01_mixed_lineage.py` 仅接受后续独立 composition root 中的完整
增量证据。以下任何一项缺失或不是 PASS，都必须在创建输出目录前拒绝：

- fresh asset seal 与 adapter identity；
- fresh P03、P02B、batch lease、resource gate；
- strict-serial scheduler 与 cleanup receipts；
- 4/4 新单元的 validated-result、P31、cleanup 和 correctness；
- 预注册规则下的 SemL0 bridge-canary comparability PASS；
- canary 保持 `included_in_figure_rows=false`；
- 旧 18 行资格没有被提升。

HOLD 计划已经在 timing 前冻结 canary 比较合同及其 canonical JSON SHA：
identity/protocol 字段必须精确相同，correctness 必须为
1700/1700、零 timeout/mismatch 且 digest 相等；相对旧 SemL0 三次的中位数，
fresh canary 的 QPS 必须在 `[0.85, 1.15]`，P50/P95 在
`[0.67, 1.50]`，P99 在 `[0.50, 2.00]`（端点包含）。失败时 normalizer
拒绝输出，只升级为 SemL0 current×3 + SemL0-naive×3 的定向 fallback，
不自动触发 21 格全重跑。后续比较 receipt 必须回链该冻结合同 SHA。

最终 normalizer 只输出 `legacy18 + fresh SemL0-naive3 = 21` 行；bridge canary
只进入 normalization receipt 的 gate lineage。输出仍为 mixed-lineage、
非 strict formal、非 paper-claim eligible。

在当前 HOLD 计划上执行 dry-run 应 fail-closed：

```bash
python3 normalize_e01_mixed_lineage.py \
  --composition E01-mixed-lineage-plan-v1.json \
  --dry-run
```

预期错误为 `pre-output gate: composition is not PASS`，且不产生输出目录。

轻量回归测试：

```bash
python3 -m unittest tests.test_e01_mixed_lineage -v
```

这些测试仅使用临时 synthetic receipts，不执行 build、import、adapter 或
timing，也不能作为任何正式实验 PASS 证据。
