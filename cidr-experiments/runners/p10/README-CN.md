# P10/P11 六系统端到端统一运行器

本目录实现 Figure 1 所需的受限 typed-neighbor 端到端实验契约。它只负责调用已经准备好的 adapter，并将每个 repeat 交给 P31 采集资源；**不会启动、停止或重建任何数据库服务，也不会下载镜像**。

## 冻结比较边界

- 系统词表固定为 SemL0、LiveGraph、Aster、TuGraph、Neo4j、NebulaGraph，缺少任意系统的完整 run 不得生成顶层 `DONE`。
- SemL0、LiveGraph、Aster、TuGraph 属于 `embedded`；Neo4j、NebulaGraph 属于 `client-server`。
- 两组分别报告，`group_policy=report-separately-no-cross-group-speedups`；结果 manifest 明确写入 `cross_group_speedups_allowed=false`。
- 接口固定为 `typed-neighbor-dense-id-v1`，查询顺序严格等于 P01 truth 的连续 `query_index`。
- 计时边界固定为 typed-neighbor 调用、完整结果物化和 digest，时钟为 `CLOCK_MONOTONIC`，并发度为 1。
- 每个 repeat 在**同一个 adapter 进程**中先跑完整 truth warmup，再跑 measured，避免重启丢失进程内 cache。每个 repeat 都重新 warmup。
- timeout、truth mismatch、顺序变化、缺行/多行、digest 不一致、P31 非 `DONE`、未知 manifest 字段都会 fail closed。

## 文件与可执行契约

权威运行入口为：

```bash
python3 -B cidr-experiments/runners/p10/run_suite.py validate \
  --manifest /abs/path/p10-p11-sf10.json \
  --run-root /abs/path/results/P10-P11-preflight \
  --mode formal

python3 -B cidr-experiments/runners/p10/run_suite.py run \
  --manifest /abs/path/p10-p11-sf10.json \
  --run-root /abs/path/results/P10-P11-run01 \
  --mode formal \
  --clean-ready-file /abs/path/CLEAN-READY
```

正式 `run-root` 必须位于 Git worktree 之外，否则创建运行产物会让 P31 的 clean-tree gate 失败。正式模式不允许覆盖真实 P31 路径，不允许 `fixture_only`，且 readiness 文件必须含独立一行：

```text
readiness_gate=PASS
```

可使用 `--group embedded`、`--group client-server` 或重复的 `--system ID` 做分阶段运行。这类子集只生成 `PARTIAL-DONE`，不会生成可被完整 Figure 1 消费的 `DONE`。

每个 adapter 只能以 argv 数组直接执行，不经过 shell。调用形状固定为：

```text
adapter [manifest args...] --request adapter-request.json --output-dir DIR
```

adapter 必须在一次进程生命周期内依次生成：

1. `phase-events.jsonl`：严格四行，依次为 warmup start/end、measured start/end；时间来自 Linux `CLOCK_MONOTONIC`。
2. `query-observations.tsv`：warmup 和 measured 的每个 pass、每个 truth 查询各一行；固定列在 `p10_contract.py::OBSERVATION_COLUMNS`。
3. `adapter-result.json`：两阶段的 requested/completed/timeout/mismatch、时间边界和 expected/actual sequence digest。

单查询 digest 仍使用 P01 truth 的 `count/sum_hash/xor_hash`。阶段级 digest 为 SHA-256，规范输入逐行为：

```text
pass_index<TAB>query_index<TAB>count<TAB>sum_hash<TAB>xor_hash<LF>
```

timeout 行改为 `pass_index<TAB>query_index<TAB>TIMEOUT<LF>`，正式协议允许的 timeout 数固定为 0。P50/P95/P99 使用 completed measured 查询的 nearest-rank 统计；timeout 不混入延迟分位数，但会直接令正式 repeat 失败。

JSON 形状记录在 `schemas/`。运行时不依赖第三方 `jsonschema` 包，`p10_contract.py` 会额外执行严格 key、类型、文件 SHA-256、truth 顺序和跨文件一致性检查。

## P31 与输出

`run_adapter_with_p31.sh` 是唯一 P31 bridge。它向 P31 传递：

- system binary、dataset、truth/query trace、resolved suite config 及 SHA-256；
- 所有 store/temp roots；
- client-server 的所有 container 名或额外 PID；
- 统一 device、mount、采样间隔和 min-samples。

adapter 整体由 GNU `timeout` 限制，超时退出会由 P31 正常走 FAILED 路径。orchestrator 只有在 P31 `DONE` 和 adapter 三份产物全部验证通过后才写 `validated-result.json`。

完整输出包括：

- `repeat-results.tsv`：逐 repeat QPS、P50/P95/P99、warmup/measurement 时间、timeout/mismatch、CPU、RSS/PSS、读写字节和磁盘峰值；
- `system-results.tsv`：逐系统跨 repeat 的中位数以及资源峰值；
- `suite-summary.json`：分组、协议、输入 SHA 和结果 SHA；
- `DONE`：仅六系统完整通过时创建；子集只能得到 `PARTIAL-DONE`；任意失败只保留 `FAILED`。

P31 当前覆盖整个 warmup+measured adapter 进程，资源列因此是 repeat 全周期口径；query latency/QPS 只来自 adapter 声明并验证过的 measured 边界。正文和图注必须保持这一区分。

## 正式 manifest 尚需填充的系统依赖

| 系统 | adapter 必须提供 | 运行前外部条件 |
|---|---|---|
| SemL0 | 共享 truth 消费、同进程 warmup/measured、逐查询观测 | integration release binary；四个变体各自 store；共享 sample plan |
| LiveGraph | query-only adapter，不得每个 repeat 重载图 | 重编译含 `--truth-tsv` 的 driver；兼容 block/WAL store |
| Aster | RocksGraph typed-neighbor bridge | clean/pinned source 与重建 driver；兼容 DB |
| TuGraph | typed-neighbor adapter | 冻结 runtime/image/binary；正式运行期间独占服务或 in-process 入口 |
| Neo4j | Bolt typed-neighbor client adapter | 精确 image digest、固定 client；外部预启动且容器名写入 manifest |
| NebulaGraph | nGQL typed-neighbor client adapter | graphd/metad/storaged 精确 digest；三个外部预启动容器均写入 manifest |

正式 manifest 的 adapter、binary、truth、file dataset 必须给出精确 SHA-256；目录 dataset 使用冻结 lineage SHA-256。client-server 还必须声明全部 image digest 和可由 P31 解析的 container/PID，运行器不会代替用户管理服务生命周期。

## Fixture 自测

六系统 fixture 使用同一个合成 adapter 的六种配置和一个明确标记为非性能的 P31 fixture；不连接数据库，不运行 benchmark：

```bash
cidr-experiments/runners/p10/tests/run_tests.sh
```

测试覆盖完整六系统 `DONE`、embedded 子集只能 `PARTIAL-DONE`、formal 拒绝 fixture、未知字段、truth mismatch、timeout 和查询乱序的 fail-closed 行为。
