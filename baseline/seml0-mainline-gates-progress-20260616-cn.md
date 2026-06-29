# SemL0 G1/G2/G3 主线总进度表

Last update: 2026-06-16 23:38 CST

## 当前正在做什么

```text
ACTIVE TASK: G1 / W14 compare optimization validation setup. SF30/SF100 are blocked until validation gates pass.
run id: w14-sf100-import-only-tuned-20260616-codex2
run root: /data/WorkSpace/lsmgraph-rs/remote-logs/w14-sf100-import-only-tuned-20260616-codex2
store root: /data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex2
current stage: Step A DONE; Step B failed at compare-type-only-semantic timeout; compare optimization validation NOT_STARTED
```

G1 之前的 all-in-one SF100 formal 已经按停止条件失败，保留为 `prior FALLBACK`。现在这次 tuned split retry 是为了判断 G1 能否从 fallback 升级：先只导入 SF100 stores，再复用 stores 跑 minimal scenario matrix。

## 总览

| Done | Gate | 目标 | 当前状态 | 当前动作 | 执行时间/耗时 | 进度文档 | 下一步最小动作 |
|---|---|---|---|---|---|---|---|
| [ ] | G1 / W14 | 证明 SemL0 不只是 edge-type-only；验证 composite semantics 的必要性 | FALLBACK retained / optimization validation NOT_STARTED | SF100 Step A import-only matrix 已完成并校验；Step B minimal matrix 停在 `compare-type-only-semantic`，7200s timeout；当前新增 compare 优化验证 gate，SF30/SF100 暂停 | Step A 约 4h07m；Step B 15:26 启动、22:19 失败，总约 6h53m；优化验证尚未开始 | `baseline/w14-sf100-split-tuning-progress-20260616-cn.md`; `baseline/w14-compare-optimization-validation-20260616-cn.md` | 先实现 P0 compare optimization；SF1 通过后才允许 SF30；SF30 full 通过后才允许 SF100 |
| [ ] | G2 / C10 | 解释 read/candidate 优势为什么没有稳定变成 latency 优势 | PLANNED / LIGHTWEIGHT ONLY | 只允许做离线日志盘点和文档整理；不启动 heavy runner | heavy runner 0；等待 G1 verdict 后计时 | `baseline/c10-latency-attribution-progress-20260616-cn.md` | G1 Step B verdict 后，先离线分析 W6/W8/W14 raw JSON 与 runner logs |
| [ ] | G3 / S0 | 诊断 semantic locality 是否在 L1+ merge/compaction 后被稀释 | PLANNED / BLOCKED BY G1+G2 | 当前不启动新实验；只保留诊断计划 | heavy runner 0；等待 G2 verdict 后计时 | `baseline/s0-semantic-dilution-progress-20260616-cn.md` | G2 verdict 后，复用 W6/W8/W9/W14 logs/stores 做 semantic dilution 诊断 |

## G1 / W14 详细进度

| Done | 阶段 | 状态 | 执行时间/耗时 | 证据/日志 | 说明 |
|---|---|---|---|---|---|
| [x] | W14 code implementation | DONE | 未单独计时 | `src/bin/lsmgraph.rs`, W14 runner/summarizer | pruning reason telemetry、dst label signature/filter、compare path 已实现 |
| [x] | cargo tests | DONE | 分钟级 | W14 progress doc | 必要单测和 `cargo test --lib` 已通过 |
| [x] | SF30 correctness/telemetry smoke | DONE / FALLBACK | 见历史 runner | `remote-logs/w14-semantic-necessity-sf30-smoke-20260615-codex4` | compare mismatches=0；但 composite semantics 尚未稳定优于 edge-type-only |
| [x] | old all-in-one SF100 formal | FAILED / PRIOR FALLBACK | 约 3h，失败在 edge-type-only import timeout | `remote-logs/w14-semantic-necessity-sf100-formal-20260616-codex1` | `edge-type-only` import 超时，partial store 已清理；不能写 SF100 W14 matrix claim |
| [x] | SF1 tuning validation | DONE | baseline 1:34；tuned 0:42 | `remote-logs/w14-tuning-sf1-ab-20260616-codex1` | `SNB_SKIP_ADJ_CACHE=1` 约 2.2x import speedup |
| [x] | SF30 tuned import smoke | DONE | 19:43 | `remote-logs/w14-tuning-sf30-edge-import-20260616-codex1` | tuned import 19:43；旧路径约 55:05；约 2.8x speedup |
| [x] | Step A import-only: schema | REUSED | 0 import time | `store/qslsm-sf100-strong-baseline-20260610/schema` | 不重复导入 schema |
| [x] | Step A import-only: edge-type-only | DONE | 约 65m | `edge-type-only-import.json` | `input_rows=1995609800`, `directed_edges=3570968680`, store 约 134G |
| [x] | Step A import-only: budg-b64 | DONE | 约 64m | `budg-b64-import.json` | `input_rows=1995609800`, `directed_edges=3570968680`, store 约 134G |
| [x] | Step A import-only: semantic | DONE | 约 1h58m；sidecar persist 51m49s | `semantic-import.json` valid | sidecar persist elapsed_s=3109.2；semantic store 约 147G |
| [x] | Step A import-only matrix marker | DONE | 总计约 4h07m | `DONE` + `IMPORT_ONLY_DONE` at 2026-06-16T15:13:33+08:00 | 三个 import JSON 全部 valid |
| [ ] | Step B minimal scenario matrix | FAILED / FALLBACK | 15:26 启动；22:19 失败；总约 6h53m；sample-plan-type-only 约 59m；edge compare 1h47m14s，mismatches=0；budg compare 1h42m18s，mismatches=0；semantic compare 7200s timeout，JSON 0 bytes | `remote-logs/w14-sf100-scenarios-min-tuned-20260616-codex1/FAILED` | 未启动 `property-required / degree-class`；不能写完整 SF100 W14 matrix claim |
| [ ] | Compare optimization validation | NOT_STARTED | 0 | `baseline/w14-compare-optimization-validation-20260616-cn.md` | 必须证明优化有效并估算 SF30/SF100 总运行时间后，才允许重启 SF30/SF100 |
| [ ] | W14 final verdict | FALLBACK retained | Step B 已结束；优化验证未开始 | `baseline/w14-sf100-split-tuning-progress-20260616-cn.md`; `baseline/w14-compare-optimization-validation-20260616-cn.md` | W14 必要性方向保留，但当前 SF100 minimal matrix 未完成；下一步必须先降 compare 成本 |

### G1 13:15 资源快照

```text
DONE: absent
FAILED: absent
completed import: edge-type-only, budg-b64
current import process: semantic
runner elapsed: about 2h09m
semantic import elapsed: about 40s
lsmgraph CPU: about 222%
lsmgraph RSS: about 1.8GiB
/data free: 395G
/tmp(root) free: 17G
MemAvailable: 446GiB
store root size: 269G
edge-type-only store: 134G
budg-b64 store: 134G
semantic store: 1.2G
```

判定：

```text
RUNNING normally. No resource stop line is close.
Do not start a second SF100 task.
Lightweight doc/log inventory may run concurrently; no G2/G3 heavy runner during Step A.
```

### G1 14:30 资源快照

```text
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed import: edge-type-only, budg-b64
current import process: semantic
current stage: persist semantic sidecars
semantic edge import complete elapsed_s: 3977.1
semantic total_elapsed_s before sidecars: 3979.8
runner elapsed: about 3h24m
semantic process elapsed: about 1h15m
lsmgraph CPU: about 222%
lsmgraph RSS: about 115GiB
/data free: 262G
/tmp(root) free: 17G
MemAvailable: 337GiB
store root size: 402G
edge-type-only store: 134G
budg-b64 store: 134G
semantic store: 134G
```

判定：

```text
RUNNING normally, but /data headroom is now tight.
Do not start Step B until semantic writes a valid JSON and /data is rechecked.
Do not launch G2/G3 heavy runners while G1 Step A is finalizing.
```

### G1 15:20 Step A 完成快照

```text
DONE: 2026-06-16T15:13:33+08:00
IMPORT_ONLY_DONE: 2026-06-16T15:13:33+08:00
FAILED: absent
completed imports: schema reuse, edge-type-only, budg-b64, semantic
all import JSON: valid
semantic sidecar persist elapsed_s: 3109.2
semantic DEGREE_DIRECTORY: 13.4GB
/data free: 250G
/tmp(root) free: 17G
MemAvailable: 447GiB
```

判定：

```text
Step A GO for Step B.
Do not start G2/G3 heavy runners yet; Step B is the active G1 task.
```

## G2 / C10 详细进度

目标：

```text
把 latency 不稳定拆成 sample-plan、metadata scan、candidate filtering、body read/decode、property lookup、
materialization/compare、JSON/log wrapping、cold/warm cache 等可解释开销。
```

| Done | 阶段 | 状态 | 说明 |
|---|---|---|---|
| [x] | C10 progress doc | CREATED | `baseline/c10-latency-attribution-progress-20260616-cn.md` |
| [ ] | Existing raw-log inventory | LIGHTWEIGHT / ALLOWED | 可在 G1 跑时只做文件列表和字段盘点，不跑 benchmark |
| [ ] | Offline attribution summarizer | NOT STARTED | 优先从 W6/W8/W14 JSON/logs 提取现有字段 |
| [ ] | Targeted C10 runner | BLOCKED | 只有离线归因不够且 G1 Step B 结束后才跑 |
| [ ] | C10 summary | NOT STARTED | `baseline/c10-latency-attribution-summary-20260616-cn.md` |
| [ ] | C10 verdict | PENDING | `GO / FALLBACK / NO-GO` |

当前并发策略：

```text
允许：读取已有 logs、列目录、整理 progress 文档。
禁止：启动新的 SF100/SF30 import、bench、compare 或冷缓存测试，直到 G1 Step A/Step B 完成。
```

## G3 / S0 详细进度

目标：

```text
诊断普通 merge/compaction 是否把 L0 的 query-semantic locality 稀释；
如果稀释成立，再考虑 S1/S2；不直接启动完整 S3 新磁盘格式。
```

| Done | 阶段 | 状态 | 说明 |
|---|---|---|---|
| [x] | S0 progress doc | CREATED | `baseline/s0-semantic-dilution-progress-20260616-cn.md` |
| [ ] | Existing evidence inventory | BLOCKED / AFTER C10 | 复用 W6/W8/W9/W14 logs/stores |
| [ ] | Semantic purity/dilution metrics | NOT STARTED | purity、candidate segments、body reads、fallback reason |
| [ ] | Hot semantic partition benefit/cost estimate | NOT STARTED | expected pruning benefit / rewrite bytes / write amplification |
| [ ] | Targeted SF30 diagnostic | CONDITIONAL | 仅 G2 后仍需要时运行 |
| [ ] | S0 summary | NOT STARTED | `baseline/s0-semantic-dilution-summary-20260616-cn.md` |
| [ ] | S0 verdict | PENDING | `GO / FALLBACK / NO-GO` |

## 停止条件

```text
MemAvailable <80GiB
/data free <200GiB
任一 SF100 import >2h in tuned split run
runner FAILED marker appears
import JSON invalid
Step B compare mismatch
telemetry fields missing
```

触发后动作：

```text
1. 不继续启动下一阶段。
2. 写入 FAILED/FALLBACK 原因。
3. 记录保留/删除的数据。
4. 给出下一步最小动作。
```

## 下一步队列

| 顺序 | 动作 | 是否可并发 | 备注 |
|---|---|---|---|
| 1 | 启动并监控 W14 Step B minimal matrix | yes | 当前主任务 |
| 2 | 轻量更新 progress 文档和 existing-log inventory | yes | 不跑 benchmark，不大规模扫描 store |
| 3 | Step B 每个 scenario 结束后检查 JSON/compare/telemetry | no | 必须基于真实 marker |
| 4 | Step B 完成后再生 summary verdict | no | 判 G1 GO/FALLBACK/NO-GO |
| 5 | G1 verdict 后决定是否进入 C10 offline attribution | no | 当前不抢跑 heavy runner |
| 6 | Step B verdict 后进入 C10 offline attribution | no heavy overlap | 若 Step B FALLBACK，C10 更重要 |
