# P40 fixed-trace 四策略 runner

该 runner 将同一份、带连续 `seq` 的 JSONL operation trace，分别重放到四个新 store：

- `none`：maintenance 事件不执行 compaction；
- `capacity-naive`：L0 文件数达到阈值时执行完整 L0→L1；
- `semantic-static`：始终 compact trace 第一条查询所在的固定语义 range，不读取运行期 query feedback；
- `semantic-feedback`：使用运行期 query probes 调用 best-partition-by-score。

默认 trace 从 SF1 `person_knows_person_0_0.csv` 的固定子集生成，seed、源文件 SHA-256、trace SHA-256 都写入 raw。每条 query 保存 canonical neighbor count 和 FNV-1a digest；validator 在四个 arms 与三个独立新进程/新 store repeats 间逐查询比较，任何 mismatch 都不会写 `DONE`。

## Correctness-only 运行

```bash
cd /data/WorkSpace/lsmgraph-cidr-p40
MODE=fixture REPEATS=3 bash baseline/run_p40_fixed_trace.sh
```

默认结果位于 `cidr-experiments/runs/P40-FIXED-TRACE/raw/<run-id>/`，并明确记录 `performance_eligible=false`。

## P31 collector 接口

`P31_COLLECTOR` 必须指向 P31 的真实
`cidr-experiments/runners/p31/run_with_resources.sh`。每个 repeat/arm 都会获得独立的
P31 run 目录和 store root；runner 会传入 `--run-dir`、`--task-id`、
`--performance-eligible`、`--repo-root`、设备/挂载点、store、binary、trace 和
逐 arm 的冻结 JSON config。自包含 operation trace 同时作为本次 replay 的 dataset
lineage 与 query/trace 输入，并由 P31 分别记录 SHA-256。

可调参数为 `P31_DEVICE`（默认 `nvme1n1`）、`P31_DATA_MOUNT`（默认 `/data`）、
`P31_INTERVAL`、`P31_DISK_INTERVAL` 和 `P31_MIN_SAMPLES`。仅 correctness fixture
允许设置 `P31_ALLOW_MISSING_AUX_TOOLS=1`；formal 模式会 fail closed。

formal 模式额外要求 `RUN_FORMAL=1`、P31 collector、通过的 clean-window `READY`
文件和显式 `PERFORMANCE_ELIGIBLE=1`。本阶段不运行 formal。

P31 参数契约的轻量 fixture 测试（不编译 Rust、不启动 benchmark）：

```bash
bash cidr-experiments/runners/p40/tests/test_p31_contract.sh
```

## P20 可复用接口

P20 可以直接复用 `make_p40_fixed_trace.py` 生成并冻结 operation trace，再通过 runner 的 `--arms` 参数只运行所需策略。trace 本身、源数据 SHA-256、seed 和逐查询 digest 的格式保持不变，因此 P20 的单策略/双策略结果可和 P40 使用同一套 validator 与 mismatch gate；不要另行生成一份未校验的操作序列。

## 当前边界

- 当前是顺序 trace replay，不是时间驱动的并发 read/write load generator；只能验证策略与结果等价性。
- 当前 smoke 是 SF1-derived fixture，不是完整 SF1 base-store 生命周期结果。
- `semantic-static` 的固定 target 是第一条 query 的 range；正式实验冻结 trace 时应同时冻结该 range 的选取依据。
- correctness fixture 仍不提供可写入论文的 CPU/PSS/disk 数据；正式资源数据必须由
  真实 P31 wrapper 在 clean-window gate 通过后采集。
