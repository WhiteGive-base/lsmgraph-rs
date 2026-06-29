# S0 Semantic Dilution 诊断 Progress

Last update: 2026-06-17 14:07 CST

## 目标

S0 是 G3，用来回答：

```text
SemL0 在 L0 的 query-semantic locality 是否会在普通 merge/compaction 后被稀释？
如果 semantic dilution 真实存在，S1/S2 的 query-signature-aware merge/L1+ 设计才有必要进入当前论文增强路线。
如果证据弱，就只写 limitation/future work，不启动完整 S3。
```

S0 不是完整 S3 实现，也不改新磁盘格式。

## 当前状态

```text
status: COMPLETED / OFFLINE PROXY DIAGNOSIS
blocked by: none
current rule: no heavy runner was started; no compaction or new store was created
verdict: FALLBACK / PROXY SIGNAL
```

## 诊断问题

- L0 segment 的 semantic purity 到 L1/L2 后是否下降。
- edge_type/src_label/dst_label/property/degree metadata 是否从 exact pruning 退化成 conservative fallback。
- merge 后 candidate segment 数是否上升。
- body reads/cache misses 是否上升。
- 哪些 query signature 高频访问同一 semantic partition。
- 如果重写 hot semantic partition，预计减少多少 read bytes。
- 重写代价是多少 rewrite bytes / write amplification。

## 计划产物

| Done | 产物 | 状态 |
|---|---|---|
| [ ] | `baseline/run_s0_semantic_dilution_20260616.sh` | not needed for offline proxy diagnosis |
| [x] | `baseline/summarize_s0_semantic_dilution_20260617.py` | completed |
| [x] | `baseline/s0-semantic-dilution-summary-20260617-cn.md` | completed |
| [x] | `baseline/s0-semantic-dilution-summary-20260617.tsv` | completed |
| [x] | `baseline/s0-semantic-dilution-progress-20260616-cn.md` | created |

## 执行顺序

1. 先复用 W6/W8/W9/W14 已有 logs、manifest 和 summary，判断是否已有 semantic dilution 信号。
2. 如果已有信号，做 SF30 targeted diagnosis，不直接开 SF100。
3. 如果 S0 显示 GO，再考虑是否把 S1/S2 纳入当前论文；否则只写 limitation/future work。

## 判定

| Verdict | 含义 |
|---|---|
| GO | semantic dilution 明确存在，S1/S2 有必要进入当前论文增强路线 |
| FALLBACK | 稀释现象弱或证据不足，写 limitation/future work |
| NO-GO | 诊断不支持 L1+ 语义设计，当前论文回到 W14 + claims safety |

## 阶段日志

### 2026-06-16 08:38 CST

```text
stage: planned
reason: G1/W14 SF100 formal is active; G2/C10 not started
action: progress doc created; no heavy command started
next minimal action: after C10 verdict, inspect existing W6/W8/W9/W14 logs for semantic purity/candidate/body read dilution signals
```

### 2026-06-16 12:18 CST

```text
stage: planned / blocked behind G1+G2
reason: G1/W14 tuned split retry is active, and G2/C10 has not produced a latency attribution verdict
current G1 run: remote-logs/w14-sf100-import-only-tuned-20260616-codex2
current G1 variant: budg-b64 import running
action: no S0 heavy runner started; no new store or benchmark created
next minimal action: after C10 verdict, perform existing-evidence inventory for semantic purity, candidate segments, body reads, fallback reasons, and possible hot-partition rewrite benefit/cost
```

### 2026-06-17 14:07 CST

```text
stage: completed / offline proxy diagnosis
inputs:
  baseline/c10-latency-attribution-summary-20260617.tsv
command:
  python3 baseline/summarize_s0_semantic_dilution_20260617.py /data/WorkSpace/lsmgraph-rs
outputs:
  baseline/summarize_s0_semantic_dilution_20260617.py
  baseline/s0-semantic-dilution-summary-20260617.tsv
  baseline/s0-semantic-dilution-summary-20260617-cn.md
result:
  15 schema/coarse-vs-variant ratio rows generated.
  W14 degree-class gives strong proxy signal:
    schema / budg-b64 elapsed ratio about 130x
    schema / budg-b64 filter-passed ratio about 146x
    schema / budg-b64 offset-lookup ratio about 154x
  W8 property/2-hop gives smaller proxy signal:
    typical elapsed ratio about 1.25-1.33x
  W6 core gives weak S0 signal and should not be used as composite-semantic superiority evidence.
verdict:
  FALLBACK / PROXY SIGNAL
paper claim:
  semantic dilution is plausible motivation and limitation/future-work evidence;
  not enough to start S1/S2/S3 implementation or claim measured multi-level semantic compaction.
next minimal action:
  return to W10 claim safety / writing freeze with W14, C10, and S0 caveats integrated.
```
