# C10 延迟归因 Progress

Last update: 2026-06-17 14:05 CST

## 目标

C10 是 G2，用来解释当前 SemL0 证据里的 latency 矛盾：

```text
W6/W8/W14 里 candidate/read/body read 有改善，但端到端 latency 不稳定。
C10 要把这个差距拆成可解释的实现开销、cache 状态、property lookup、body decode、compare/materialization 和 runner/logging overhead。
```

C10 的结果决定论文是否可以写 latency 相关 claim：

| Verdict | 论文处理 |
|---|---|
| GO | 可以写 bounded latency / overhead attribution，并解释为何 read-amp 改善没有完全转成 latency |
| FALLBACK | 不写 latency advantage，只写 candidate/read/body read 与实现 caveat |
| NO-GO | 发现会推翻当前性能结论的实现瓶颈，需要修代码或收窄 claims |

## 当前状态

```text
status: COMPLETED / OFFLINE ATTRIBUTION
blocked by: none
current rule: no heavy runner was started; attribution reused W6/W8/W14 existing JSON/stderr only
verdict: FALLBACK / CLAIM-SAFETY
```

## 计划观测项

- sample-plan 时间。
- per-query engine time。
- metadata scan time。
- candidate filtering time。
- body read/decode time。
- property lookup/evaluation time。
- result materialization/merge time。
- compare path overhead。
- JSON/log wrapping overhead。
- cold-cache 与 warm-cache 差异。
- process-level wall clock 与 JSON 内部 latency 的差异。

## 计划产物

| Done | 产物 | 状态 |
|---|---|---|
| [ ] | `baseline/run_c10_latency_attribution_20260616.sh` | not needed for offline pass; no heavy runner started |
| [x] | `baseline/summarize_c10_latency_attribution_20260617.py` | completed |
| [x] | `baseline/c10-latency-attribution-summary-20260617-cn.md` | completed |
| [x] | `baseline/c10-latency-attribution-summary-20260617.tsv` | completed |
| [x] | `baseline/c10-latency-attribution-progress-20260616-cn.md` | created |

## 执行顺序

1. 离线读取 W6/W8/W14 raw JSON、runner log 和 manifest，先做不跑新实验的归因。
2. 如果离线信息不足，再添加 lightweight instrumentation 或 targeted SF30 runner。
3. 只在 G1/W14 SF100 formal 完成、失败或被明确停止后启动。

## 阶段日志

### 2026-06-16 08:38 CST

```text
stage: planned
reason: G1/W14 SF100 formal is active
action: progress doc created; no heavy command started
next minimal action: after G1 verdict, inspect W6/W8/W14 raw JSON/logs for available latency fields
```

### 2026-06-16 12:18 CST

```text
stage: planned / lightweight only
reason: G1/W14 tuned split retry is active; current task is SF100 Step A import-only matrix
current G1 run: remote-logs/w14-sf100-import-only-tuned-20260616-codex2
current G1 variant: budg-b64 import running
resource snapshot: /data free 517G, MemAvailable 445GiB
action: C10 heavy runner not started; only document updates and existing-log inventory are allowed concurrently
next minimal action: after G1 Step B verdict, start offline attribution from W6/W8/W14 JSON/logs before adding any new runner
```

### 2026-06-17 14:05 CST

```text
stage: completed / offline attribution
inputs:
  W6: remote-logs/w6-sf100-matrix-20260613-132325
  W8: remote-logs/w8-property-2hop-20260614-2025
  W14: remote-logs/w14-sf100-minimal-reuse-20260617-codex1
command:
  python3 baseline/summarize_c10_latency_attribution_20260617.py /data/WorkSpace/lsmgraph-rs
outputs:
  baseline/summarize_c10_latency_attribution_20260617.py
  baseline/c10-latency-attribution-summary-20260617.tsv
  baseline/c10-latency-attribution-summary-20260617-cn.md
result:
  32 attribution rows generated.
  W6/W8/W14 existing JSON parsed; W8 runner-log-prefixed JSON handled by robust parser.
  No SF30/SF100 runner started.
verdict:
  FALLBACK / CLAIM-SAFETY
paper claim:
  explain read/candidate reduction vs latency mismatch;
  allow bounded schema-vs-pruned latency cliff evidence from W14 degree-class;
  do not claim broad latency superiority or composite-over-edge-type-only latency superiority.
next minimal action:
  start G3/S0 semantic dilution as a small offline/targeted diagnosis only; do not start S1/S2/S3 implementation.
```
