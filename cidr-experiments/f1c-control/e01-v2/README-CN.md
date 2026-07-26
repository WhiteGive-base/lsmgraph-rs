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
