# P20 A0--A6 canonical profiles

此目录冻结 Figure 2 查询控制面逐组件消融的配置契约。`profiles.json` 是唯一配置源；runner 必须先调用 `validate_profiles.py` 解析配置，禁止自行拼接 stage、layout、CPU phase 或 automatic-maintenance 开关。

## 因果阶梯

| Stage | 本阶段新增的唯一逻辑机制 | 物理 layout | 测量前固定处理 |
|---|---|---|---|
| A0 | 无；evidence 已物化但 query control 全部关闭 | `semantic-budgeted` | 无 |
| A1 | exact evidence admission | `semantic-budgeted` | 无 |
| A2 | semantic routing | `semantic-budgeted` | 无 |
| A3 | budgeted degree promotion；A4 的 adaptation control | `semantic-budgeted` | 重放共享训练 trace，feedback 关闭且不执行反馈 compaction |
| A4 | feedback-driven adaptation | `semantic-budgeted` | 从与 A3 相同前态重放共享训练 trace，启用 feedback 并执行一次 legacy-global feedback compaction |
| A5 | semantic compaction output | `semantic-budgeted` | 同一训练 trace 后 semantic-partitioned compaction |
| A6 | automatic lifecycle control | `semantic-budgeted` | 同一训练 trace，由 closed loop 维护并等待 quiescence |

A3/A4 必须从同一个 frozen pristine store 的独立新 clone 与相同 cache policy 开始，重放完全相同的训练 trace；A3 不记录/应用 feedback，A4 启用 feedback 并执行其选择的 compaction。因此 A3→A4 只能表述为 feedback-driven adaptation（含其触发的 compaction）的整体增益，不能声称隔离了纯 priority 或纯 compaction 算法。A4/A5/A6 必须从各自独立的 pristine clone 开始并重放同一训练 trace，避免前序 stage 污染后序 store。

A0 与 A1 必须使用同一 `semantic-budgeted` 物理 store；A0 只是不读取 exact evidence，A1 只新增 admission。真正的 naive LSM-CSR 端到端基线放在 Figure 1，不混入 Figure 2 的单因素 staircase。若把 A0 layout 改成 `naive`，validator 会立即失败。

## 四种运行模式

- `latency`：正式单流延迟/QPS；CPU phase instrumentation 关闭。只有 A6 开 automatic maintenance。
- `cpu-phase`：单流诊断，非正式 latency；打开 process-CPU phases，关闭 background maintenance，因此 A6 被 fail-closed 排除。
- `closed-loop-resource`：只允许 A6，采集完整进程树 CPU/RSS/PSS/I/O；关闭 CPU phase instrumentation。
- `correctness`：是独立的第一阶段，固定 trace 与 result digest 门控，永远 `performance_eligible=false`；由 `build_correctness_pass.py` 聚合全 stage 输出，测量 runner 不自证当前 run。

测量中的 `--auto-compact` 一律禁止。A4/A5 所需 feedback compaction 是单独的测量前训练步骤，训练输出、digest 与 store SHA 必须进入 manifest。

resolver 会把预处理直接编码进同一个 `storage-bench` 进程：A3 使用 `--training-runs 1`；A4/A5 使用 `--training-runs 1 --training-feedback-compactions 1 --ra-min-score 0`；A6 使用 `--training-runs 1 --automatic-maintenance`。不能把训练与 compaction 拆成两个 CLI 进程，因为 feedback map 只存在于当前 Engine 进程；拆开后得到的不是 A4/A5。

A4/A5 只重放一次冻结训练 trace，因此在测量前 feedback compaction 中显式固定 `--ra-min-score 0`。生产默认 score 下限 10 会拒绝这条刻意缩短的 trace（本次 SF10 诊断的最大 score 为 4.397）。minimum-query 与 minimum-L0-segment 两个资格门仍保持默认值，故该设置不会在观测量或可选 segment 不足时强行制造 compaction 候选。

## 矩阵

- SF10：A0--A6 × typed-one-hop / degree-stratified / property-presence；至少 3 个 independent runs。
- SF30：A0/A2/A4/A6 × 同三类 workload；至少 3 个 independent runs。
- 同一 scale/workload 使用同一个 sample plan、truth、import order 和训练 trace。

## 使用

只验证配置：

```bash
python3 cidr-experiments/runners/p20/validate_profiles.py
```

解析一个可执行 profile：

```bash
python3 cidr-experiments/runners/p20/validate_profiles.py \
  --resolve --scale sf10 --stage A3 --mode latency --workload typed-one-hop
```

property workload 必须显式绑定冻结后的 property id：

```bash
python3 cidr-experiments/runners/p20/validate_profiles.py \
  --resolve --scale sf10 --stage A3 --mode latency \
  --workload property-presence --bind PROPERTY_ID=17
```

解析结果只包含 canonical feature 参数；路径、样本数、warmup、repeat、binary/truth/dataset SHA 等由上层 runner 添加并写入 manifest。正式任务必须由 P31 `run_with_resources.sh` 包裹，且先通过 shared-truth correctness gate 与 clean-window sentinel。

## v2 batch admission

新正式批次向 `run_single_profile.py` 同时传入 `--batch-lease` 与 canonical
`--batch-gate-tool`。runner 在创建 stage store 前和释放 P31 命令前各重验一次 lease；
P31 必须返回 consumer=`P20` 的 integrity guard PASS。lease、两次 admission、guard
READY/status/samples 及 command release 的 SHA-256 均写入 input manifest、summary 与
`P20-PASS.json`。Figure 2 聚合器禁止在同一批次混用 v1/v2 admission。

旧 6 小时 P02B freshness 路径仅在显式 `--legacy-v1-admission` 时启用，并同时要求
`--p02b-sentinel-result` 与 `--p02b-sentinel-result-sha256`；v2 与 legacy 参数混用会在
克隆 stage store 前 fail closed。
