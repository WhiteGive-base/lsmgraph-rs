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

## P31 collector 预留接口

如设置可执行文件 `P31_COLLECTOR`，每个 arm 会按以下接口交给 collector 包装：

```text
collector --run-id ID --repeat N --arm NAME --raw-dir DIR -- COMMAND...
```

formal 模式额外要求 `RUN_FORMAL=1`、P31 collector、通过的 clean-window `READY` 文件和显式 `PERFORMANCE_ELIGIBLE=1`。本阶段不运行 formal。

## P20 可复用接口

P20 可以直接复用 `make_p40_fixed_trace.py` 生成并冻结 operation trace，再通过 runner 的 `--arms` 参数只运行所需策略。trace 本身、源数据 SHA-256、seed 和逐查询 digest 的格式保持不变，因此 P20 的单策略/双策略结果可和 P40 使用同一套 validator 与 mismatch gate；不要另行生成一份未校验的操作序列。

## 当前边界

- 当前是顺序 trace replay，不是时间驱动的并发 read/write load generator；只能验证策略与结果等价性。
- 当前 smoke 是 SF1-derived fixture，不是完整 SF1 base-store 生命周期结果。
- `semantic-static` 的固定 target 是第一条 query 的 range；正式实验冻结 trace 时应同时冻结该 range 的选取依据。
- P31 接口已预留，但在 collector 分支合并前没有 CPU/PSS/disk 临时峰值数据。
