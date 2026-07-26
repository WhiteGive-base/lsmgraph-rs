# F1C / E01 v2（Linux-only engineering contract）

本目录是 Figure 1 正式补全的隔离工程实现。它只定义并验证 E01 的
`7 systems × 3 repeats = 21` 输入合同；不会启动 runner、P10、P31、Docker、
release build 或 timing，也不会改写任何冻结 raw root。

## 资格边界

`l6-control/sample-l5/L5-CONDITIONAL-COMBINED.json` 是旧的六系统、18-cell
conditional fixture。`existing-evidence-inventory.json` 只登记它的 lineage 和
缺口，明确保留 `formal_eligible=false`、`performance_eligible=false`、
`paper_claim_eligible=false`。normalizer 不接受它，也不允许把它和
`SemL0-naive` 的三行拼成 formal 21-run campaign。

E01 v2 的 source manifest 必须：

* 按固定顺序包含 `SemL0`、`SemL0-naive`、`LiveGraph`、`Aster RocksGraph`、
  `TuGraph`、`NebulaGraph`、`Neo4j`，每组恰好 repeat 1/2/3；
* 声明 `execution_mode=formal`、`formal_eligible=true`、`performance_eligible=true`；
* 保持 `paper_claim_eligible=false`，直到独立的 Figure/QA claim receipt 出现。
  因此 normalizer 永远不会仅凭 tidy 数据把论文 claim 资格提升为 true；
* 为每个 run 回链 validated result、adapter request/provenance、P31 manifest /
  validation / DONE、batch admission 和 cost receipt，并逐个验证 path、size、SHA；
* 对 digest、mismatch、timeout、P50/P95/P99、QPS、load、disk、host、trace、
  cache、binary/version 和 eligibility 做 fail-closed 检查。

## 使用（仅在 Linux 安全窗口）

```bash
python3 normalize_e01_v2.py \
  --manifest /ABS/E01-formal-manifest-v2.json \
  --out-dir /ABS/e01-normalized
```

`out-dir` 必须不存在。成功时以目录原子重命名的方式写入
`E01-results.tsv`、`normalization-receipt.json`、manifest/schema 副本和
`SHA256SUMS`；任何失败都不产生图或半成品目录。

纯工程回归（不读 raw、不运行 timing）：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

正式 21-run campaign、fresh P03/P02B gate/lease、store seal、renderer 和 QA
均不属于本目录的命令；必须在 L8 完成并重新取得独立 formal gate/lease 后，
由 Linux runbook 串行执行。

## 21-run 正式启动 manifest 门

`build_e01_formal_manifest.py` 只构建不可覆盖的启动 manifest，不执行 launcher。
输入 spec 必须固定七个系统的顺序与三个 repeat，并提供同一 campaign 的
fresh P03 clean-ready、P02B gate、独占 batch lease/marker，以及 dataset、
trace、每系统 binary/store 的 immutable fresh seal。builder 只读取这些显式
小收据（单文件上限 16 MiB），验证 P03→P02B→lease 血缘和所有协议绑定，不会
遍历或计算 store/binary 本体的 SHA。

成功输出 `state=PASS` 的固定 21-cell 静态合同，并把
host、dataset、trace、cache、concurrency、
interface、git、binary 和 store SHA 写入每个 cell。启动 manifest 仍保持
`formal_eligible=false`、`performance_eligible=false` 和
`paper_claim_eligible=false`；后续 launcher、fresh resource gate 及逐 run
验证收据尚未完成前，不得提升资格或复用旧 conditional evidence。

```bash
python3 build_e01_formal_manifest.py \
  --spec /ABS/E01-formal-launch-spec-v1.json \
  --output /NEW/E01-formal-launch-manifest-v1.json
```

`run_e01_formal_matrix.py --synthetic-test-mode` 只验证 restart/resume、
STRICT_SERIAL 顺序和 `CELL-DONE`/`MATRIX-DONE` 合同；它不会调用 P10 adapter，
不会产生 timing。已完成 cell 在 resume 时重新核验 manifest/result/CELL-DONE
SHA 后跳过，残缺或漂移 cell 会 fail-closed 且不覆盖。只有 21/21
`CELL-DONE` 才发布 `MATRIX-DONE`。不带 synthetic 参数的生产入口有意拒绝运行，
直到真实 fresh admission/assets 与 adapter 执行合同完成。

`e01-cell-evidence-v1.schema.json` 与 `validate_e01_cell_evidence.py` 冻结生产
launcher 必须交付的统一 cell 接口：command、adapter、P31、correctness、
fairness、cgroup、cleanup 七类收据都必须逐项绑定 run/ordinal/launch
manifest SHA，并由 `CELL-DONE.json` 记录相对路径、size 和 SHA。生产模式还
强制 `adapter_invoked=true`、`timing_generated=true`；任一收据缺失、SHA
漂移或 eligibility 提升都会拒绝。

synthetic scheduler 使用完全相同的 `CELL-DONE v2`/七收据形状，但每层都明确
记录 `mode=synthetic`、`synthetic_test_only=true`、
`adapter_invoked=false`、`timing_generated=false`，因此不能冒充生产数据。

`build_e01_production_command_plan.py` 与
`validate_e01_production_command_plan.py` 只生成/复核小型生产命令计划 JSON。
每个 21-run cell 都展开为显式 argv 数组、allowlist env、cell 内 cwd、已验 SHA
的 adapter entry/P31/cgroup wrapper，以及 validated result、七收据和
CELL-DONE 的唯一目标路径。禁止 shell command 字符串、路径越出 campaign
root、重复 cell root 和 eligibility 提升。计划固定
`execution_state=NOT_IMPLEMENTED`，构建过程不会创建 campaign root；现有
production execution 入口仍在创建结果根前 fail-closed。

`produce_e01_receipt_fixtures.py` 是七类收据的 fixture-only CLI。它只在已存在、
非 symlink 的 cell root 内以临时目录加原子 rename 发布七张小 JSON，返回相对
路径/size/SHA；所有收据都固定 synthetic/fixture-only、adapter 未调用、未产生
timing 且 eligibility 全 false。路径逃逸、已有 receipt 目录、run/ordinal 或
manifest SHA 漂移都会在发布前拒绝。`--production` 有意在检查/创建输出根之前
返回 `NOT_IMPLEMENTED`，这些 fixture 不能满足 production evidence validator。

`e01-production-command-spec-v1.schema.json` 与
`validate_e01_production_spec.py` 补齐 executor 前的真实 spec/admission 接口。
command spec 必须逐 SHA 绑定同一 formal manifest、P03 clean-ready、P02B gate、
batch lease、lease marker 和 cell-evidence schema，并声明还需要 fresh resource
gate。command-plan builder 会重新验证该绑定并原样写入 plan；任一 lineage/SHA
漂移、synthetic/fixture 标记或 eligibility 提升都会拒绝。executor 状态仍被
合同固定为 `NOT_IMPLEMENTED`，本接口不生成或续签 admission receipt。

`e01-adapter-artifact-identity-v1.schema.json` 与
`validate_e01_adapter_artifact_identity.py` 将每个真实 spec 的 adapter entry
从裸路径提升为静态 identity receipt：必须是 canonical absolute、已存在、
非 symlink、小文件，并绑定 entry SHA/size、formal manifest 中对应的 engine
binary/store SHA、harness git 和 protocol SHA。production spec 与 command
plan 会分别重新验证 receipt 及其内容；路径、文件或任一 identity 漂移都会在
plan 输出和正式根创建前 fail-closed。receipt 本身仍固定
`execution_state=NOT_IMPLEMENTED`、非 synthetic/fixture 且 eligibility 全 false。
