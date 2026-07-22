# P10/P11 六系统端到端统一运行器

本目录实现 Figure 1 所需的受限 typed-neighbor 端到端实验契约。它调用冻结的 adapter，并将每个 repeat 交给 P31 采集资源；**不会启动或停止外部数据库服务，也不会下载镜像**。只有显式声明 fresh-import 生命周期的 embedded adapter 可以在本 repeat 的全新目录中导入冻结 dataset。

## 冻结比较边界

- 系统词表固定为 SemL0、LiveGraph、Aster、TuGraph、Neo4j、NebulaGraph，缺少任意系统的完整 run 不得生成顶层 `DONE`。
- SemL0、LiveGraph、Aster、TuGraph 属于 `embedded`；Neo4j、NebulaGraph 属于 `client-server`。
- 两组分别报告，`group_policy=report-separately-no-cross-group-speedups`；结果 manifest 明确写入 `cross_group_speedups_allowed=false`。
- 接口固定为 `typed-neighbor-dense-id-v1`，查询顺序严格等于 P01 truth 的连续 `query_index`。
- 计时边界固定为 typed-neighbor 调用、完整结果物化和 digest，时钟为 `CLOCK_MONOTONIC`，并发度为 1。
- 每个 repeat 在**同一个 adapter 进程**中完成其声明的 setup（若有）、完整 truth warmup 和 measured，避免重启丢失进程内 cache。每个 repeat 都重新 warmup。
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
  --batch-lease /abs/path/batch-lease.json \
  --batch-gate-tool "$PWD/cidr-experiments/runners/batch_gate_v2.py"
```

正式 `run-root` 必须位于 Git worktree 之外，否则创建运行产物会让 P31 的 clean-tree gate 失败。正式模式不允许覆盖真实 P31 路径，不允许 `fixture_only`。v2 会在 suite admission 和每个 repeat 开始前重验 lease，并要求每个 P31 validation 含完整 integrity guard PASS；lease、repeat admission、guard READY/status/samples/release 的 hash 都进入 suite provenance。

旧协议只可显式使用 `--legacy-v1-clean-ready /abs/path/CLEAN-READY`；
`--clean-ready-file` 仅保留为兼容别名。legacy readiness 文件必须含独立一行：

```text
readiness_gate=PASS
```

可使用 `--group embedded`、`--group client-server` 或重复的 `--system ID` 做分阶段运行。这类子集只生成 `PARTIAL-DONE`，不会生成可被完整 Figure 1 消费的 `DONE`。

每个 adapter 只能以 argv 数组直接执行，不经过 shell。调用形状固定为：

```text
adapter [manifest args...] --request adapter-request.json --output-dir DIR
```

request 会把 `execution_mode`、`process_lifetime`、system binary、runtime
libraries、dataset 以及每个 store root 一并传给 adapter。正式模式中
binary/dataset/truth/runtime-library SHA-256 均不能为空。prebuilt store 还必须
绑定 lineage SHA；fresh-import store 在 repeat 开始前不存在，因此以冻结的
dataset/importer/runtime SHA 和本次实际 store 路径绑定，不能冒充 prebuilt store。

adapter 必须在一次进程生命周期内依次生成：

1. `phase-events.jsonl`：严格四行，依次为 warmup start/end、measured start/end；时间来自 Linux `CLOCK_MONOTONIC`。
2. `query-observations.tsv`：warmup 和 measured 的每个 pass、每个 truth 查询各一行；固定列在 `p10_contract.py::OBSERVATION_COLUMNS`。
3. `adapter-result.json`：两阶段的 requested/completed/timeout/mismatch、时间边界和 expected/actual sequence digest；fresh-import 生命周期还必须提供独立 `setup` 边界、CPU 和导入后 store bytes。

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

P31 覆盖 adapter 的完整进程生命周期。对于 LiveGraph，这明确包括
`fresh import -> warmup -> measured -> teardown`，所以 P31 CPU/RSS/I/O 是完整
repeat 口径。query latency/QPS/P50/P95/P99 只来自 adapter 声明并验证过的
measured 边界；import wall/CPU/store bytes 单独报告，不得混入 query speedup。

## 正式 manifest 尚需填充的系统依赖

| 系统 | adapter 必须提供 | 运行前外部条件 |
|---|---|---|
| SemL0 | 共享 truth 消费、同进程 warmup/measured、逐查询观测 | integration release binary；四个变体各自 store；共享 sample plan |
| LiveGraph | `adapters/livegraph_adapter.py`；原生 worker 同进程 fresh import→warmup→measured | 每个 repeat 使用全新的 store/temp；冻结 dense dataset、worker 和 `liblivegraph.so` SHA |
| Aster | `adapters/aster_adapter.py` + 原生 RocksGraph worker | clean/pinned source、冻结 binary/dataset/logical-store SHA、reopen DB、P02B PASS |
| TuGraph | typed-neighbor adapter | 冻结 runtime/image/binary；正式运行期间独占服务或 in-process 入口 |
| Neo4j | `adapters/neo4j_adapter.py`；真实 Bolt/Cypher typed-neighbor、逐查询 monotonic latency/digest/timeout | 精确 image digest、固定 Python driver；只读外部预启动容器与独立 runtime-store copy |
| NebulaGraph | nGQL typed-neighbor client adapter | graphd/metad/storaged 精确 digest；三个外部预启动容器均写入 manifest |

正式 manifest 的 adapter、binary、truth、file dataset 必须给出精确 SHA-256；目录 dataset 使用冻结 lineage SHA-256。client-server 还必须声明全部 image digest 和可由 P31 解析的 container/PID，运行器不会代替用户管理服务生命周期。

client-server request 还会携带 `external_service`，把 suite manifest 的
`service_lifecycle/containers/extra_pids/image_digests` 原样交给真实 adapter。这样 adapter
复验的容器和 P31 采集的容器不能由两套互不相干的参数指定。

每个系统必须显式填写 `process_lifetime` 和 `runtime_libraries`。LiveGraph 的
正式值只能是 `fresh-import-and-query-process-lifetime-v1`，不能标成 prebuilt、
reopenable 或 query-only；其 `runtime_libraries` 至少包含实际加载的
`liblivegraph.so` 绝对路径和 SHA-256。

prebuilt 生命周期中的每个正式 `store_roots[]` 还必须提供 `sha256`，表示已发布
store manifest 的 lineage SHA-256。`fresh-import-and-query-process-lifetime-v1`
声明的是空的 base root；运行器为每个 repeat 派生并创建唯一 store/temp 子目录，
拒绝复用已有目录。实际路径写入 adapter request 和 P31 argv，导入后 logical/
allocated bytes 写入结果。

### Aster adapter

Aster 已有真实 `RocksGraph::AddVertexWithEdges/GetAllEdges` adapter、fresh/reopen
生命周期、逐查询延迟与 digest、逻辑 store SHA、P02B/P31 formal gate。完整构建、
store 发布和剩余正式依赖见
[`adapters/ASTER-ADAPTER-CN.md`](adapters/ASTER-ADAPTER-CN.md)。`fresh` 仅用于
tiny/SF1 correctness；正式 Figure 1 必须使用已冻结的 `reopen` store。

### LiveGraph adapter 的当前边界

构建真实 LiveGraph query worker：

```bash
make -C baseline/external-drivers livegraph_p10 \
  LG=/abs/path/to/LiveGraph
```

`livegraph_p10_driver --capabilities` 固定声明
`fresh-import-and-query-process-lifetime-v1`，并报告进程实际加载的
`liblivegraph.so` 绝对路径。adapter 复算该 library SHA 并与 request 比较。

当前 LiveGraph 的 block/WAL 不能跨进程重开，因此正式协议不再假装使用
query-only reusable store。每个独立 repeat 都从同一冻结 dense dataset 导入到
全新 store，在同一原生进程中依次执行 import、完整 truth warmup 和 measured。
driver 分开输出 import CLOCK_MONOTONIC wall、user/system CPU、block/WAL logical/
allocated bytes，以及 query-only QPS/P50/P95/P99。orchestrator 再从逐查询记录
独立重算分位数和 digest；两者不一致即 fail closed。跨系统 speedup 只比较
matched measured phase，import 成本必须在独立列/表中报告。

真实 LiveGraph 小 fixture 自测（不产生性能数据）：

```bash
LD_LIBRARY_PATH=/abs/path/to/LiveGraph/build \
LIVEGRAPH_P10_BINARY=$PWD/baseline/external-drivers/livegraph_p10_driver \
python3 -B -m unittest -v \
  cidr-experiments/runners/p10/tests/test_livegraph_adapter.py
```

### Neo4j adapter 的当前边界

Neo4j 使用 `external-prestarted-query-process-lifetime-v1`，但服务生命周期由 P10 orchestrator
在每个 repeat 内显式管理。launcher 从 `docker container create` 捕获完整 64-hex ID；后续
inspect/start/stop/rm 均只使用该 ID，并发布 launch/stop v2 receipt。真实 adapter 在查询前后
复验 ID/PID/StartedAt/RestartCount、numeric `Config.User`、`neo4j:5.26.24` RepoDigest、localhost Bolt 端口、
`/data` mount、`restart=no`、只读默认数据库和 Python driver `5.28.3`。连接 readiness 有界；
查询前还会实时确认数据库名严格为 `neo4j`，且唯一 `:V(id)` RANGE index `v_id` 为 `ONLINE`。

正式系统片段模板位于 `adapters/neo4j/formal-system.template.json`。store manifest v3 使用
owner-only `0700` root、`0600` `store_lock`、跨完整哈希持续持有的独占锁，以及哈希前后
Docker mount/root/lock 复查来证明离线；它不依赖其他用户不可读的 `/proc`。pristine source
与三个 runtime clone 在 P31 前做逐文件 SHA-256 和全树一致性证明，并拒绝 shared inode。
store 与 logs root 必须同属当前 UID:GID、mode 精确为 `0700`，importer 与 launcher 均显式
传入相同 `--user UID:GID` 并在 receipt/inspect 中闭合。受控 import receipt 为 v3，P31/adapter
只消费封存 receipt，不在测量窗口重新哈希大 CSV。
dataset/truth/store、canonical P02B、P31 DONE/manifest/validation/collector status/ready 的
路径、大小与 SHA-256 必须逐项闭合；运行中任何身份漂移都会 fail closed。详细流程见
`adapters/NEO4J-ADAPTER-CN.md`。

无 Docker、无 SF10 大文件读取的 contract 自测：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  cidr-experiments/runners/p10/tests/test_neo4j_runtime_lifecycle.py \
  cidr-experiments/runners/p10/tests/test_neo4j_hardening.py \
  cidr-experiments/runners/p10/tests/test_neo4j_sealed_receipts.py \
  cidr-experiments/runners/p10/tests/test_p31_consumer_contract.py
```

上述无容器自测通过后，仍须在合入 main 后、SF10 前，以官方 `neo4j:5.26.24` RepoDigest
完成一次真实 tiny import/launch/stop/remove/post-stop gate，验证 numeric UID:GID 与官方
entrypoint 的运行兼容性；缺少该真实 PASS receipt 时禁止启动 SF10。

## Fixture 自测

六系统 fixture 使用同一个合成 adapter 的六种配置和一个明确标记为非性能的 P31 fixture；不连接数据库，不运行 benchmark：

```bash
cidr-experiments/runners/p10/tests/run_tests.sh
```

测试覆盖完整六系统 `DONE`、embedded 子集只能 `PARTIAL-DONE`、formal 拒绝 fixture、未知字段、truth mismatch、timeout 和查询乱序的 fail-closed 行为。
