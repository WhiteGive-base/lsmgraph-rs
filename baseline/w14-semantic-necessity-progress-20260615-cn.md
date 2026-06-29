# W14 SemL0 必要性实验 + 清理计划 Progress

Last update: 2026-06-16 08:38 CST

## G1/G2/G3 主线 gate 状态

当前 W14 被明确定位为 G1。G2/C10 延迟归因、G3/S0 semantic dilution 诊断已经加入主线计划，但在 G1 SF100 formal 运行期间不并发启动，避免和 SF100 import 抢内存、磁盘和 cache。

| Gate | 名称 | 状态 | 进度文档 | 当前动作 |
|---|---|---|---|---|
| G1 | W14 必要性实验 | FALLBACK / SF100 FAILED | `baseline/w14-semantic-necessity-progress-20260615-cn.md` | 记录失败原因并清理 failed partial store |
| G2 | C10 延迟归因 | PLANNED | `baseline/c10-latency-attribution-progress-20260616-cn.md` | G1 结束后执行 |
| G3 | S0 semantic dilution 诊断 | PLANNED | `baseline/s0-semantic-dilution-progress-20260616-cn.md` | G2 结束后执行 |

统一 gate 进度表：

```text
baseline/seml0-mainline-gates-progress-20260616-cn.md
```

2026-06-16 08:38 CST 检查：G1/W14 SF100 formal 仍在运行，`DONE`/`FAILED` 均不存在。当前阶段是 `edge-type-only` SF100 import 已完成输入扫描，正在从 import stream 构建 adjacency cache。进程仍活跃，MemAvailable 约 156GiB，`/data` 剩余 397G，未触发停止线；但 `edge-type-only` 临时 store 已到 313G，需要持续观察磁盘风险。

2026-06-16 08:47 CST 检查：adjacency build 已完成，但 `edge-type-only-import.json` 仍为 0 字节，说明 import 进程尚未正常退出；未进入 `budg-b64`，`DONE`/`FAILED` 均不存在。MemAvailable 约 219GiB，`/data` 剩余 397G，store 仍为 313G。短时 guard 已运行 5 分钟，因为 import JSON 尚未落盘所以没有终止 runner。继续等待 import 完成或 3 小时 timeout。

2026-06-16 08:55 CST 检查：状态仍未变化，`edge-type-only-import.json` 仍为 0 字节，runner 日志停在 `adjacency build complete`。lsmgraph 进程仍在高 CPU 运行，MemAvailable 约 219GiB，`/data` 剩余 397G。判断为 import 尾部 CPU-side finalization 阶段过长，继续等正常完成或 3 小时 import timeout。

2026-06-16 09:25 CST 检查：SF100 formal 按停止条件失败，`FAILED=2026-06-16T09:25:04+08:00 unexpected exit status=124`。失败阶段是 `edge-type-only` SF100 import，原因是单 import 超过 `IMPORT_TIMEOUT_SECONDS=10800`，`edge-type-only-import.json` 仍为 0 字节。runner 未进入 `budg-b64`，无子进程残留。失败后 MemAvailable 约 447GiB，`/data` 剩余 397G；失败 partial store 位于 `store/w14-semantic-necessity-sf100-formal-20260616-codex1/edge-type-only`，约 313G。

当前 W14 最终判定：`FALLBACK`。SF30 smoke 仍支持 correctness/telemetry 和保守必要性写法；SF100 W14 composite matrix 未完成，不能写任何 SF100 W14 composite advantage claim。313G partial store 不构成论文证据，记录后应删除释放磁盘。

2026-06-16 09:27 CST 清理：已删除 `store/w14-semantic-necessity-sf100-formal-20260616-codex1`，释放约 313G。`/data` 从 397G free 恢复到 709G free。保留 `remote-logs/w14-semantic-necessity-sf100-formal-20260616-codex1` 下的 `FAILED`、`runner.log`、`manifest.tsv` 和 0 字节 `edge-type-only-import.json` 作为失败证据。

## 本阶段目标

W14 的目标是回答当前最强 reviewer attack：

```text
SemL0 看起来只是 edge-type partitioning + metadata filtering。
W6 里 edge-type-only 已经接近 budg-b64，为什么还需要 SemL0？
```

本阶段不做 PDF/模板/artifact 收尾，不启动完整 S3，也不把 S3 写成第二篇文章。S3 只作为同一篇论文的 future-work / optional-strengthening backlog 留档。

## 预计耗时与停止条件

- 文档和代码实现：1-3 小时，已完成。
- 安全清理：0-30 分钟，已完成；`/tmp/hadoop-root` 因 root-owned 保留。
- `cargo test`：已完成。
- SF30 smoke：实际 2026-06-16 02:06:47-06:14:51 CST，约 4 小时 8 分钟。
- SF100 formal：续跑目标审计后补充启动。使用全 6 scenarios x 4 variants，但将样本数设置为 `SAMPLES=500`、`REPEATS=1`，避免默认 `5000 x 3` 把任务拖成不可控长跑。

停止条件：

- MemAvailable <80GiB。
- `/data` free <200GiB。
- 单个 SF100 import >3h。
- 任何 compare mismatch。
- telemetry 字段缺失。
- 同一外部条件连续三次恢复仍阻断。

## 资源快照

清理前：

```text
Mem: total 503Gi, used 51Gi, free 148Gi, buff/cache 303Gi, available 447Gi
/data: size 2.0T, used 1.6T, avail 307G, use 84%
/tmp(root /dev/vda3): size 295G, used 281G, avail 1.9G, use 100%
```

清理后：

```text
Mem: total 503Gi, used 51Gi, free 148Gi, buff/cache 303Gi, available 447Gi
/data: size 2.0T, used 1.5T, avail 369G, use 81%
/tmp(root /dev/vda3): size 295G, used 266G, avail 18G, use 94%
```

W14 SF30 smoke 完成后：

```text
Mem: total 503Gi, used 51Gi, free 138Gi, buff/cache 313Gi, available 447Gi
/data: size 2.0T, used 1.6T, avail 257G, use 87%
/tmp(root /dev/vda3): size 295G, used 266G, avail 17G, use 94%
```

`/data` 从 369G 降到 257G 的主要原因是 W14 临时 `edge-type-only` SF30 store，约 113G，目前保留用于复查。

SF100 formal 启动前二次清理后：

```text
Mem: total 503Gi, used 51Gi, free 370Gi, buff/cache 81Gi, available 447Gi
/data: size 2.0T, used 1.2T, avail 709G, use 63%
/tmp(root /dev/vda3): size 295G, used 266G, avail 17G, use 94%
```

二次清理删除了 W14 计划中明确列为 "SF30 smoke 通过后可删" 的 W8/W14 SF30 stores；保留 SF100 schema reference store。

## 清理计划执行状态

| Done | 路径 | 计划大小 | 状态 |
|---|---|---:|---|
| [x] | `/tmp/e9-manual-sf10-tc4` | 6.8G | deleted |
| [x] | `/tmp/e9-sf10-tc4` | 6.8G | deleted |
| [ ] | `/tmp/hadoop-root` | 27G | blocked: root-owned, still exists |
| [x] | `/tmp/hadoop-ydl` | 1.7G | deleted |
| [x] | `remote-logs/livegraph-sf10-20260612/livegraph-block` | 35G | deleted |
| [x] | `remote-logs/livegraph-sf10-20260612/edges-raw.tsv` | 14G | deleted |
| [x] | `remote-logs/livegraph-sf10-20260612/edges-dense.txt` | 6.2G | deleted |
| [x] | `remote-logs/livegraph-sf10-20260612/livegraph-wal` | 1.0G | deleted |
| [x] | `store/w9-steady-state-smoke-20260613-1653` | 699M | deleted |
| [x] | `store/sf1-full-validation` | 759M | deleted |
| [x] | `store/budget-sf1` | 8.2G | deleted |

W14 smoke 后暂不删除：

| Done | 路径 | 计划大小 | 状态 |
|---|---|---:|---|
| [x] | `store/w8-property-2hop-20260614-2025/schema` | 113G | deleted after W14 SF30 smoke |
| [x] | `store/w8-property-2hop-20260614-2025/budg-b64` | 113G | deleted after W14 SF30 smoke |
| [x] | `store/w8-property-2hop-20260614-2025/semantic` | 117G | deleted after W14 SF30 smoke |
| [x] | `store/w14-semantic-necessity-sf30-smoke-20260615-codex2/edge-type-only` | 约 113G | deleted after W14 SF30 smoke |

保护项未删除：

```text
store/sf100-base-graph
store/sf30-base-graph
store/qslsm-sf100-strong-baseline-20260610
remote-logs/w6-sf100-matrix-20260613-132325
remote-logs/w8-property-2hop-20260614-2025
baseline/*
paper/*
artifact/*
```

## 实现进度

| Done | 项目 | 文件 | 状态 |
|---|---|---|---|
| [x] | S3 留档计划 | `baseline/seml0-s3-full-plan-20260615-cn.md` | completed; 当前不施工 |
| [x] | query signature builder | `src/semantic.rs` | added `GraphAccessSignature::with_dst_label` |
| [x] | pruning reason decision | `src/csr/format.rs` | added `SignaturePruningDecision` and `signature_pruning_decision` |
| [x] | pruning telemetry JSON | `src/metrics.rs` | added `csr.pruning_reasons` |
| [x] | dst_label result filter | `src/graph.rs` | CSR and memgraph-visible results filtered before merge |
| [x] | compare/query filter support | `src/bin/lsmgraph.rs` | added `src_label`, `dst_label`, `force_signature`, property compare support |
| [x] | W14 runner | `baseline/run_w14_semantic_necessity_20260615.sh` | completed; supports comma-separated `SCENARIOS` |
| [x] | SF100 schema reuse | `baseline/run_w14_semantic_necessity_20260615.sh` | added `SF100_SCHEMA_STORE`, defaulting to `store/qslsm-sf100-strong-baseline-20260610/schema` |
| [x] | W14 launcher | `baseline/launch_w14_semantic_necessity_20260615.sh` | completed; nohup/PID/log |
| [x] | W14 summarizer | `baseline/summarize_w14_semantic_necessity_20260615.py` | completed |
| [x] | W14 summary | `baseline/w14-semantic-necessity-summary-20260615-cn.md` | generated from raw JSON |

## 测试

已通过：

```text
cargo test -j 16 --all-targets signature_pruning_decision_reports_semantic_reason -- --nocapture
cargo test -j 16 --all-targets dst_label_signature_filters_returned_edges -- --nocapture
cargo test -j 16 --all-targets metrics_reset_clears_counters_and_endpoints -- --nocapture
cargo test -j 16 --lib
```

`cargo test --lib` 结果：64 passed。仅有既有 warning：

```text
src/base_graph/csr.rs: unused import OpenOptions
src/base_graph/csr.rs: unused variable capacity
```

## W14 runs

| Run id | 状态 | 说明 |
|---|---|---|
| `w14-semantic-necessity-sf30-smoke-20260615-codex` | FAILED | runner 未 `cd $ROOT`，从 `/home/ydl` 调 cargo build 失败 |
| `w14-semantic-necessity-sf30-smoke-20260615-codex2` | FAILED after useful import | 成功导入 `edge-type-only` SF30 store；失败原因是 runner log 写入 stdout，污染 import JSON |
| `w14-semantic-necessity-sf30-smoke-20260615-codex3` | FAILED/stopped | 发现 scenario args 错误：`type-only` 被错误附带 property predicate；已停止并修 runner |
| `w14-semantic-necessity-sf30-smoke-20260615-codex4` | DONE | 有效 SF30 smoke，3 scenarios, 50 samples, all compares mismatches=0 |
| `w14-semantic-necessity-sf100-formal-20260616-codex1` | FAILED | `edge-type-only` SF100 import exceeded 10800s timeout; no valid import JSON; did not enter `budg-b64` |

有效 smoke：

```text
run root: remote-logs/w14-semantic-necessity-sf30-smoke-20260615-codex4
scenarios: type-only, type-src-label, property-required
variants: schema, edge-type-only, budg-b64, semantic
samples: 50
DONE: exists
FAILED: absent
compare mismatches: all 0
summary: baseline/w14-semantic-necessity-summary-20260615-cn.md
```

## W14 summary verdict

最终判定：`FALLBACK`。

原因：

- Correctness 通过：所有 compare `mismatches=0`。
- Telemetry 有效：summary 能读到 `edge_type`、`property_absence`、`schema_tombstone_fallback`、`kept_candidate` 等 pruning reason。
- `property-required` 场景显示 SemL0 可以记录额外 semantic pruning：semantic 的 `property_absence` pruned segments 为 287，高于 edge-type-only 的 9。
- 但 sampled `property-required` 结果没有稳定证明 composite semantics 优于 edge-type-only；edge-type-only 在该样本上也能 compare 正确，甚至因为样本结果稀疏而看起来更激进。
- process-level runner 时间暴露明显瓶颈：semantic bench/compare 每个关键步骤约 35 分钟，而 JSON 内部 per-query latency 不能直接代表端到端 run time。没有 C10 延迟归因前，不应写强 latency claim。

可以写进论文的安全 claim：

```text
edge-type-only 是 SemL0 的一维特例。
SemL0 提供 exact/conservative semantic telemetry，并能在 property-required 等 query semantics 下解释额外 pruning surface。
```

不能写的 claim：

```text
SemL0 composite semantics 已稳定优于 edge-type-only。
SemL0 已证明端到端 latency advantage。
```

## 当前完成线

| Done | 条件 | 状态 |
|---|---|---|
| [x] | 清理计划执行并记录 `/data` 与 `/tmp` 资源状态 | done |
| [x] | S3 留档文档完成，且明确当前不施工 | done |
| [x] | W14 代码实现完成并通过必要 cargo test | done |
| [x] | SF30 smoke 完成，mismatches=0，JSON/telemetry valid | done, verdict FALLBACK |
| [x] | SF100 formal 完成 schema / edge-type-only / budg-b64 / semantic composite matrix | FAILED by stop condition: `edge-type-only` import exceeded 10800s; no SF100 W14 matrix claim |
| [x] | summary 写出 Table X、pruning reason breakdown、claim verdict | done |
| [x] | 临时 store 的保留/删除状态写清楚 | done |

## 下一步最小动作

续跑目标审计后，虽然 SF30 smoke 已给出 `FALLBACK`，但目标模式仍显式要求 SF100 matrix。因此已补跑一个有时间盒的 SF100 formal attempt：

```bash
cd /data/WorkSpace/lsmgraph-rs
env RUN_ID=w14-semantic-necessity-sf100-formal-20260616-codex1 \
  SCALE=sf100 W14_ALLOW_SF100=1 \
  SF100_SCHEMA_STORE=/data/WorkSpace/lsmgraph-rs/store/qslsm-sf100-strong-baseline-20260610/schema \
  SAMPLES=500 WARMUP_RUNS=0 REPEATS=1 \
  STORE_ROOT=/data/WorkSpace/lsmgraph-rs/store/w14-semantic-necessity-sf100-formal-20260616-codex1 \
  baseline/launch_w14_semantic_necessity_20260615.sh
```

实际结果：`edge-type-only` import 在 adjacency build complete 后没有产出 valid import JSON，触发 10800s timeout，run 写出 `FAILED`。因此 SF100 formal 当前失败，W14 以 SF30 `FALLBACK` 作为论文边界。

下一步最小动作：

- `store/w14-semantic-necessity-sf100-formal-20260616-codex1` 这个 313G failed partial store 已删除。
- 不再用 all-variants store-root 方式重跑 SF100，除非先改 runner 为 per-variant/import-delete 策略或提高 timeout 并重新评估磁盘预算。
- 进入 G2/C10 前，先以已有 W6/W8/W14 raw logs 做离线 latency attribution，不启动新的 SF100。

当前不建议执行：

```text
W14 SF100 formal
完整 S3 多级容器段
L2/L3 scheduler
numeric range pruning
online schema migration
```
