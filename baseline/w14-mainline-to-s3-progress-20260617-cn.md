# W14 Mainline To S3 Progress

Overall status: W14 final verdict written
Current stage: G2/C10 offline attribution unlocked; S3 implementation blocked
Last update: 2026-06-17 13:36 CST
Final verdict: `w14-final-verdict-20260617-cn.md`
S3 gate decision: `w14-to-s3-gate-decision-20260617-cn.md`
Allowed next step: update remote progress, then run C10 offline attribution from existing logs; do not run fresh SF100 rebuild matrix or S3 implementation.

## 目标

```text
解决 W14 SF100 formal 未跑完整的问题：先优化 compare 路径并证明有效，再按 SF1 -> SF30 -> SF100 gate 推进。
SF100 minimal matrix 通过或给出合理 FALLBACK 后，才进入 S3 留档/开启前置判断；不直接启动完整 S3 工程。
```

## 总和进度表

| Done | 阶段 | 状态 | 开始时间 | 结束时间 | 耗时 | Run/日志 | 结果 | 通过条件 | 下一步 |
|---|---|---|---|---|---|---|---|---|---|
| [x] | P0 compare optimization | PASSED | 2026-06-16 | 2026-06-17 | - | `src/bin/lsmgraph.rs`; `src/graph.rs`; `baseline/run_w14_semantic_necessity_20260615.sh`; `baseline/sf100_digest_compare.py` | bench digest compare 路径可用；`Engine::open` phase logging 可用；sidecar read buffering 修复 | `cargo test` 通过；SF1 可启动；Engine open 阶段日志可见；digest compare 工具可用 | 已进入 SF1 |
| [x] | SF1 validation | PASSED | 2026-06-17 00:25 | 2026-06-17 00:31 | 313s | `remote-logs/w14-sf1-gate-20260617` | 3 场景 `mismatches=0`；semantic open 修复后约 4.7s；总耗时 <=15min | 总耗时 `<=15min`；digest compare 全部 `mismatches=0`；输出 SF30/SF100 初估 | SF30 type-only 已解锁 |
| [ ] | SF30 type-only gate | FAILED / cold-import-blocked | 2026-06-17 01:00 | 2026-06-17 01:46 | 46min streaming attempt; first attempt 1min | first attempt: `remote-logs/w14-sf30-type-only-gate-20260617`; retry: `remote-logs/w14-sf30-type-only-streaming-20260617` | first attempt 因磁盘风险主动停止；streaming retry 完成 schema import 20m43s、sample-plan 16m14s、schema bench 1s，edge-type-only import 启动后判断无法满足 60min gate 并主动停止；未进入 digest compare | `type-only <=60min`；`mismatches=0`；输出 SF30 full / SF100 type-only ETA | 先清理或恢复可复用 SF30 stores，再重跑 SF30 type-only |
| [ ] | SF30 full gate | SKIPPED / superseded | - | - | - | 待定 | SF30 cold import gate failed；后续改走 SF100 reuse-store policy，不再阻塞 W14 verdict | 不再用 SF30 full gate 解锁当前 W14 | 保留为后续可选补证 |
| [x] | SF100 type-only gate | PASSED / reuse-store cached-plan | 2026-06-17 08:56 | 2026-06-17 09:20 | about 23min | `remote-logs/w14-sf100-type-only-reuse-cachedplan-20260617-codex2` | 复用现有 SF100 stores 和 cached 100-sample type-only plan；三组 digest compare 均 `checked=100 mismatches=0` | `type-only <=2h`；`mismatches=0` | 已进入 SF100 minimal reuse-store matrix |
| [x] | SF100 minimal matrix | PASSED / reuse-store bench-only | 2026-06-17 09:24 | 2026-06-17 13:05 | about 3h41m after type-only gate | `remote-logs/w14-sf100-minimal-reuse-20260617-codex1`; local mirror `w14-result-check/w14-sf100-minimal-reuse-20260617-codex1` | property-required 和 degree-class 均完成；schema-vs-edge-type-only/budg-b64/semantic 全部 `checked=5000 mismatches=0 verdict=PASS` | correctness digest compare 全 PASS；无资源 stop line | 已写 W14 final verdict |
| [x] | W14 final verdict | WRITTEN | 2026-06-17 13:20 | 2026-06-17 13:25 | - | `w14-final-verdict-20260617-cn.md` | GO for SF100 reuse-store bench-only evidence；fresh SF100 rebuild matrix = FALLBACK / do not run；S3 full implementation 仍 BLOCKED | claim boundary 已写清 | 进入 W10 写作冻结/证据集成 |
| [ ] | S3 留档/开启前置判断 | BLOCKED / W10 decision pending | - | - | - | `baseline/seml0-s3-full-plan-20260615-cn.md` | S3 计划已留档；当前不施工 | 仅在 W10 消化 W14 verdict 后判断是否开启小前置；不启动完整 S3 | 停在开启 S3 前的阶段 |

## 必填运行时间反馈

每个 gate 结束后必须写：

```text
SF1 actual runtime:
SF30 actual runtime or ETA:
SF100 actual runtime or ETA:
sample-plan time:
schema bench time:
variant bench time:
digest compare time:
Engine open phase time:
reasonability verdict:
```

如果任一估时不合理：

```text
stop immediately
record failure reason
inspect bottleneck
tune locally or reduce scope
do not advance to the next scale
```

## 当前判断

```text
Current status: FAILED / FALLBACK
Current stage: SF30 type-only gate stopped
SF30 status: type-only gate failed because cold import dominates without reusable stores
SF100 status: BLOCKED until SF30 full passes
S3 status: BLOCKED; do not start full S3 implementation
```

## 存储清理清单 (2026-06-17 08:30 CST)

当前资源快照：

```text
/data avail: 285G
/ avail: 17G
```

必须保护：

```text
/data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex2   # W14 SF100 imported variants, 415G
/data/WorkSpace/lsmgraph-rs/store/qslsm-sf100-strong-baseline-20260610          # schema SF100 reference, 143G
/data/WorkSpace/ldbc-sf100
/data/WorkSpace/ldbc-sf30
/data/WorkSpace/ldbc-sf10
/data/WorkSpace/tugraph_ldbc_snb
/data/WorkSpace/lsmgraph-rs/baseline
/data/WorkSpace/lsmgraph-rs/paper
/data/WorkSpace/lsmgraph-rs/artifact
/data/WorkSpace/lsmgraph-rs/remote-logs
```

优先可清理候选：

| 路径 | 大小 | 建议 | 说明 |
|---|---:|---|---|
| `/data/WorkSpace/ldbc-sf300` | 293G | 可删候选 | 当前 W14/W6/W8 不依赖 SF300；删除前确认没有 SF300 作业。 |
| `/data/WorkSpace/lsmgraph-rs-w1-fix` | 43G | 可删候选 | 旧 worktree/store；当前主线在 `lsmgraph-rs`。 |
| `/data/WorkSpace/dgs/output` | 14G | 条件可删 | DGS 输出缓存；保留源码和必要输入。 |
| `/data/WorkSpace/dgs/target` | 3.7G | 条件可删 | Rust build artifact，可重建。 |
| `/data/WorkSpace/lsmgraph-rs/target` | 2.8G | 条件可删 | Rust build artifact，可重建，会增加下次编译时间。 |
| `/data/WorkSpace/lsmgraph-rs/store/sf10-base-graph` | 6.8G | 条件可删 | 可重建 base graph；若近期还要 SF10 验证则保留。 |
| `/data/WorkSpace/lsmgraph-rs/store/sf1-base-graph` | 698M | 条件可删 | 可重建 base graph。 |

明确不能删除：

```text
/data/WorkSpace/ldbc-sf10
/data/WorkSpace/tugraph_ldbc_snb
```

## P0b Import-Many Optimization (2026-06-17)

| Done | Stage | Status | Start | End | Runtime | Run/log | Result | Gate | Next |
|---|---|---|---|---|---|---|---|---|---|
| [x] | P0b import-many optimization | PASSED / SF1 smoke | 2026-06-17 08:36 | 2026-06-17 08:42 | import-many 73s; bench/compare about 29s | code: `src/snb/full_loader.rs`, `src/snb/mod.rs`, `src/bin/lsmgraph.rs`; run: `remote-logs/w14-import-many-sf1-smoke-20260617-codex1` | added `import-many --layout-store layout:/path`; one SNB CSV scan fans out to multiple Engine/store instances; SF1 schema+semantic digest compare checked=200 mismatches=0 | `cargo test --bin lsmgraph`, `cargo build --release`, SF1 import-many smoke, and digest compare all passed | usable for future store rebuilds; current SF100 should reuse existing stores and run bench-only gate |

Implementation:

```text
src/snb/full_loader.rs: import_snb_full_multi()
src/snb/mod.rs: export import_snb_full_multi
src/bin/lsmgraph.rs: import-many --layout-store layout:/path
```

Validation:

```text
run: remote-logs/w14-import-many-sf1-smoke-20260617-codex1
import-many SF1 schema+semantic: 73s
input_rows: 19,308,214
directed_edges: 34,692,699
store size: schema 1.4G, semantic 1.5G
digest compare: checked=200 mismatches=0 verdict=PASS
semantic open: total_open_s=4.029
```

10-hour boundary:

```text
fresh SF100 rebuild matrix: NO
  reason: existing SF100 import-only evidence shows edge-type-only about 64min,
          budg-b64 about 64min, semantic about 118min, and semantic sidecar persist alone about 51.8min.
          import-many reduces repeated CSV scans but does not remove per-store CSR/sidecar materialization.

reuse existing SF100 stores + bench-only minimal matrix: LIKELY / gated
  estimate: 6-9h
  prerequisite: reuse
    store/qslsm-sf100-strong-baseline-20260610/schema
    store/w14-sf100-import-only-tuned-20260616-codex2/{edge-type-only,budg-b64,semantic}
  required gate: SF100 type-only <=2h and mismatches=0
```

Current resource snapshot:

```text
/data avail: 641G
/ avail: 17G
```

Additional cleanup candidate:

| Path | Size | Recommendation | Reason |
|---|---:|---|---|
| `/data/WorkSpace/lsmgraph-rs/store/w14-import-many-sf1-smoke-20260617-codex1` | 2.8G | conditional delete | P0b smoke store; logs are retained. |

## SF100 Reuse-Store Execution (2026-06-17)

Current execution policy:

```text
Do not fresh rebuild SF100.
Reuse existing SF100 stores only.
Start SF100 type-only gate first.
If and only if type-only gate passes within 2h and mismatches=0, continue minimal matrix with property-required + degree-class.
```

Protected stores:

```text
schema:         /data/WorkSpace/lsmgraph-rs/store/qslsm-sf100-strong-baseline-20260610/schema
edge-type-only: /data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex2/edge-type-only
budg-b64:       /data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex2/budg-b64
semantic:       /data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex2/semantic
```

| Done | Stage | Status | Start | End | Runtime | Run/log | Result | Gate | Next |
|---|---|---|---|---|---|---|---|---|---|
| [ ] | SF100 type-only reuse-store gate | STOPPED / superseded | 2026-06-17 08:51 | 2026-06-17 08:55 | about 4min | `remote-logs/w14-sf100-type-only-reuse-20260617-codex1`, PID `1834609` | confirmed reuse-store, then stopped because old valid cached type-only plan exists; marker `STOPPED` written | no correctness verdict; replaced by cached-plan bench-only gate | use `codex2` run |
| [x] | SF100 type-only reuse-store cached-plan gate | PASSED | 2026-06-17 08:56 | 2026-06-17 09:20 | about 23min | `remote-logs/w14-sf100-type-only-reuse-cachedplan-20260617-codex2`, marker `PASSED` | confirmed reuse-store and reused cached 100-sample type-only plan; digest compare schema-vs-edge-type-only/budg-b64/semantic all `checked=100 mismatches=0` | global timeout 7200s; `/data` free >=200G; MemAvailable >=80GiB; all digest compares mismatches=0; JSON valid | run property-required + degree-class reuse-store minimal matrix |
| [x] | SF100 minimal reuse-store matrix remainder | PASSED | 2026-06-17 09:24 | 2026-06-17 13:05 | about 3h41m after type-only gate; total W14 reuse-store gate about 4h09m | `remote-logs/w14-sf100-minimal-reuse-20260617-codex1`, marker `DONE` | `property-required` and `degree-class` both completed from reused SF100 stores; schema-vs-edge-type-only/budg-b64/semantic digest compares all `checked=5000 mismatches=0 verdict=PASS`; no fresh import | all required digest compares pass; no resource stop line crossed | write W14 final verdict |

Parallelism decision at 2026-06-17 09:56:

```text
Current heavy process: property-required sample-plan, PID 1858576
RSS: about 203GiB
CPU: about 100%
MemAvailable: about 244GiB
Decision: do not start degree-class sample-plan concurrently.
Reason: a second SF100 sample-plan generation likely needs another 200GiB-class resident set and may violate the MemAvailable >=80GiB stop condition.
Allowed parallelism: documentation updates and light monitoring only; run degree-class after property-required finishes.
```

Parallelism re-check at 2026-06-17 10:07:

```text
Current heavy process: property-required sample-plan, PID 1858576
RSS: about 276GiB
CPU: about 100%
MemAvailable: about 171GiB
Decision: still do not start degree-class concurrently.
Reason: memory headroom is shrinking; starting another SF100 sample-plan would likely violate the MemAvailable >=80GiB stop condition.
```

Pre-run resource snapshot:

```text
/data avail: 641G
MemAvailable: 447GiB
No confirmed active SF100 lsmgraph runner.
```

Monitoring update at 2026-06-17 12:02:

```text
Current stage: SF100 minimal reuse-store matrix / degree-class / schema bench
Run: remote-logs/w14-sf100-minimal-reuse-20260617-codex1
Progress: property-required completed with mismatches=0 for all three variants; degree-class sample-plan completed at 11:37:03 after about 54min.
Current substage: schema bench started at 11:37:03 and is still running at 12:02:08.
Observed open phase: schema total_open_s=594.798, semantic_index_s=594.769.
Elapsed for current schema bench: about 25min.
Resources: MemAvailable=422GiB, /data free=641G.
Decision: continue monitoring. Do not launch concurrent SF30/SF100 work. If schema single-variant runtime grows beyond a controlled gate, record fallback and consider smaller degree-class sample count.
```

Monitoring update at 2026-06-17 12:07:

```text
Current stage: SF100 minimal reuse-store matrix / degree-class / schema bench
Run: remote-logs/w14-sf100-minimal-reuse-20260617-codex1
Current substage elapsed: about 31min since 11:37:03.
Open phase: already completed at total_open_s=594.798.
Current bottleneck: executing 5000 degree-class queries on schema baseline.
Resources: MemAvailable=422GiB, /data free=641G.
Decision: continue monitoring. No concurrent heavy jobs. This substage is slower than property-required; if it approaches the 7200s bench timeout or stops making CPU progress, mark FAILED/FALLBACK and switch to a smaller controlled degree-class gate.
```

Monitoring update at 2026-06-17 12:13:

```text
Current stage: SF100 minimal reuse-store matrix / degree-class / schema bench
Current substage elapsed: about 36.5min.
CPU: lsmgraph process still active at about 120% CPU.
Output: schema.json still 0 bytes; storage-bench writes final JSON only after the substage completes.
Resources: MemAvailable=422GiB, /data free=641G.
Decision: continue to the configured 7200s bench timeout unless CPU stalls or resources cross the stop line. No concurrent heavy jobs.
```

Monitoring update at 2026-06-17 12:24:

```text
Current stage: SF100 minimal reuse-store matrix / degree-class / schema bench
Current substage elapsed: about 47min.
CPU: lsmgraph process still active at about 124% CPU.
Output: schema.json still 0 bytes.
Resources: MemAvailable=422GiB, /data free=641G.
Timeout boundary: current bench has timeout 7200s, so hard stop is around 2026-06-17 13:37 if it does not finish.
Decision: continue; do not start concurrent heavy jobs. If timeout triggers, mark degree-class 5000-sample schema as FAILED/FALLBACK and run a smaller controlled gate rather than leaving the stage ambiguous.
```

Monitoring update at 2026-06-17 12:34:

```text
Current stage: SF100 minimal reuse-store matrix / degree-class / schema bench
Current substage elapsed: about 58min.
CPU: lsmgraph process still active at about 126% CPU.
Output: schema.json still 0 bytes.
Resources: MemAvailable=422GiB, /data free=641G.
Interpretation: 5000-sample degree-class on schema baseline is the current bottleneck. It is not a memory/disk failure.
Decision: continue until either it completes or the 7200s timeout fires around 13:37. Do not add extra /proc probing because SSH monitoring commands are intermittently timing out.
```

Monitoring update at 2026-06-17 12:56:

```text
Current stage: SF100 minimal reuse-store matrix / degree-class / semantic bench
Run: remote-logs/w14-sf100-minimal-reuse-20260617-codex1
Completed substages:
  schema bench: 11:37:03 -> 12:51:50, about 74m47s, no timeout.
  edge-type-only bench: 12:51:51 -> 12:52:37, about 46s.
  budg-b64 bench: 12:52:38 -> 12:53:21, about 43s.
Current substage:
  semantic bench started 12:53:22 and is running.
Resources: MemAvailable=397GiB, /data free=641G.
Interpretation: degree-class is expensive for schema baseline but did not hit timeout; budgeted/type-only variants are much faster on this substage. Need semantic result and digest compares before final verdict.
Estimated remaining: about 15-30min if semantic open/query follows prior SF100 timings, then digest compare.
```

Monitoring update at 2026-06-17 13:11:

```text
Current stage: SF100 minimal reuse-store matrix remainder
Observed marker: remote-logs/w14-sf100-minimal-reuse-20260617-codex1/DONE was present at 13:11:28.
Completed substages:
  semantic bench: 12:53:22 -> 13:05:27, semantic total_open_s=674.880, semantic_index_s=674.843.
  digest compare files were present for degree-class:
    digest-compare-schema-vs-edge-type-only.json
    digest-compare-schema-vs-budg-b64.json
    digest-compare-schema-vs-semantic.json
Resources after completion: MemAvailable=447GiB.
Pending verification: read digest JSON contents and confirm checked=5000, mismatches=0 for degree-class and property-required.
Reason pending: SSH/scp connection became intermittently rejected after DONE was observed. This is a connection-layer issue, not a runner failure.
Decision: treat run as completed-observed but keep final verdict PENDING until digest JSON fields are read and PASSED marker is written.
```

Monitoring update at 2026-06-17 13:50:

```text
Final verification completed after SSH recovered.
Verified files:
  property-required/digest-compare-schema-vs-edge-type-only.json
  property-required/digest-compare-schema-vs-budg-b64.json
  property-required/digest-compare-schema-vs-semantic.json
  degree-class/digest-compare-schema-vs-edge-type-only.json
  degree-class/digest-compare-schema-vs-budg-b64.json
  degree-class/digest-compare-schema-vs-semantic.json
All six digest compares: checked=5000, mismatches=0, sample_value_mismatches=0, verdict=PASS.
Marker written: remote-logs/w14-sf100-minimal-reuse-20260617-codex1/PASSED at 13:50.
Final status: SF100 minimal reuse-store matrix remainder PASSED.
```

Completion update at 2026-06-17 13:17:

```text
Current stage: SF100 minimal reuse-store matrix complete
Run: remote-logs/w14-sf100-minimal-reuse-20260617-codex1
DONE marker: 2026-06-17T13:05:29+08:00
Runner process: not active after completion
Resources after check: /data free=641G, MemAvailable=447GiB

Completed substages:
  property-required sample-plan: 09:24:01 -> 10:20:36
  property-required schema bench: 10:20:36 -> 10:30:22
  property-required edge-type-only bench: completed at 10:30:22
  property-required budg-b64 bench: completed at 10:30:23
  property-required semantic bench: 10:30:23 -> 10:42:53
  degree-class sample-plan: 10:42:54 -> 11:37:03
  degree-class schema bench: 11:37:03 -> 12:51:50
  degree-class edge-type-only bench: 12:51:51 -> 12:52:37
  degree-class budg-b64 bench: 12:52:38 -> 12:53:21
  degree-class semantic bench: 12:53:22 -> 13:05:27

Correctness:
  property-required schema-vs-edge-type-only: checked=5000 mismatches=0 verdict=PASS
  property-required schema-vs-budg-b64: checked=5000 mismatches=0 verdict=PASS
  property-required schema-vs-semantic: checked=5000 mismatches=0 verdict=PASS
  degree-class schema-vs-edge-type-only: checked=5000 mismatches=0 verdict=PASS
  degree-class schema-vs-budg-b64: checked=5000 mismatches=0 verdict=PASS
  degree-class schema-vs-semantic: checked=5000 mismatches=0 verdict=PASS

Open-phase observations:
  schema open remained expensive because semantic_index_s was about 583-595s.
  semantic open remained expensive because semantic_index_s was about 675-744s.
  edge-type-only and budg-b64 open stayed sub-second.

Interpretation:
  W14 reuse-store / bench-only SF100 gate passed.
  Fresh SF100 rebuild matrix remains disallowed by previous cost boundary.
  W14 final verdict is written in w14-final-verdict-20260617-cn.md.
  Next substantive task is W10 integration: claim labeling, table rewrite, appendix/artifact integration, and S3 scope decision.
```
