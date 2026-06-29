# W14 Compare Optimization Validation Progress

Overall status: PASSED
Current stage: SF100 reuse-store type-only gate and minimal matrix completed
Last update: 2026-06-17 13:50 CST
Final verdict: PASSED
Allowed next step: continue W14 summary verdict / G2 C10 latency attribution / G3 S0 semantic dilution; do not run fresh SF100 rebuild matrix.

## 目标

本阶段只回答一个问题：

```text
优化后的 W14 correctness compare 是否真实有效，并且是否能在可控时间内支撑 SF30 / SF100。
```

在本文档顶部 `Overall status: PASSED` 前，不启动 SF30/SF100 heavy run。

## 总进度

| Done | 阶段 | 状态 | 开始时间 | 结束时间 | 耗时 | Run/日志 | 结果 | 通过条件 | 下一步 |
|---|---|---|---|---|---|---|---|---|---|
| [x] | P0 optimization | PASSED | 2026-06-16 | 2026-06-17 | - | SF1 A/B 烟测 (store/w14-tuning-sf1-ab-20260616-codex1) | 实现并验证 | `cargo test` 通过；SF1 可启动；Engine open 阶段日志可见；digest compare 工具可用 | 启动 SF1 validation（小规模 W14 runner） |
| [x] | P0b import-many optimization | PASSED / SF1 smoke | 2026-06-17 08:36 | 2026-06-17 08:42 | import-many 73s; bench/compare 约 29s | code: `src/snb/full_loader.rs`, `src/snb/mod.rs`, `src/bin/lsmgraph.rs`; run: `remote-logs/w14-import-many-sf1-smoke-20260617-codex1` | 新增 `import-many --layout-store layout:/path`，同一次 SNB CSV 扫描 fan-out 到多个 Engine/store；SF1 schema+semantic 两 store 生成成功；digest compare checked=200 mismatches=0 | `cargo test --bin lsmgraph` 通过；`cargo build --release` 通过；SF1 import-many JSON valid；schema vs semantic digest PASS | 该优化可用于未来重建 store；当前 SF100 优先复用已存在 stores 做 bench-only gate |
| [x] | SF1 validation | PASSED | 2026-06-17 00:25 | 2026-06-17 00:31 | 313s | remote-logs/w14-sf1-gate-20260617 | 3 场景 mismatches=0；open 修复后 4.7s | 总耗时 `<=15min`；所有 digest compare `mismatches=0`；任一 scenario `<=5min`；任一 Engine open `<=60s` | SF30 type-only 已解锁 |
| [ ] | SF30 type-only gate | FAILED / cold-import-blocked | 2026-06-17 01:00 | 2026-06-17 01:46 | 46min streaming attempt; first attempt 1min | first attempt: `remote-logs/w14-sf30-type-only-gate-20260617`; streaming retry: `remote-logs/w14-sf30-type-only-streaming-20260617` | first attempt 主动中断，原因是原 runner 会同时保留多个 SF30 stores，磁盘可能跌破 200G；streaming retry 完成 schema import 20m43s、sample-plan 16m14s、schema bench 1s，启动 edge-type-only import 后判断无法满足 60min gate 并主动停止；未进入 digest compare | `type-only` 总耗时 `<=60min`；digest compare `mismatches=0`；必须输出 SF30 full ETA 与 SF100 type-only ETA | 先清理或恢复可复用 SF30 stores；在 gate 重新设计前不启动 SF100 |
| [ ] | SF30 full gate | BLOCKED | - | - | - | 待定 | 等待 SF30 type-only | 三场景总耗时 `<=3h`；所有 digest compare `mismatches=0`；必须输出 SF100 minimal matrix ETA | 通过后允许 SF100 type-only |
| [ ] | SF100 type-only gate | BLOCKED | - | - | - | 待定 | 等待 SF30 full | 复用现有 SF100 Step A stores；`type-only` 总耗时 `<=2h`；digest compare `mismatches=0`；必须输出 SF100 minimal matrix final ETA | 通过后允许 SF100 minimal matrix |
| [ ] | SF100 minimal matrix | BLOCKED | - | - | - | 待定 | 等待 SF100 type-only | `type-only / property-required / degree-class` 总耗时 `<=6h`；所有 digest compare `mismatches=0` | 通过后写 `Overall status: PASSED` 和 run `PASSED` marker |

## 必填记录项

每次启动 runner 前，必须在本文件写入：

```text
run id:
command:
expected runtime:
timeout:
stop conditions:
```

每次 runner 结束后，必须写入：

```text
actual runtime:
scenario runtime breakdown:
Engine open phase runtime:
digest compare checked / mismatches:
timeout triggered: yes/no
allowed next step:
```

## 运行时间评估规则

优化后不仅要验证 correctness，还必须给出 SF30/SF100 总运行时间评估。

| 评估点 | 必须输出 | 判定用途 |
|---|---|---|
| SF1 结束 | SF30 type-only ETA、SF30 full ETA 初估 | 决定是否允许 SF30 |
| SF30 type-only 结束 | SF30 full ETA、SF100 type-only ETA 初估 | 决定是否继续 SF30 full |
| SF30 full 结束 | SF100 type-only ETA、SF100 minimal matrix ETA 初估 | 决定是否允许 SF100 |
| SF100 type-only 结束 | SF100 minimal matrix final ETA | 决定是否跑完整 SF100 minimal matrix |

估时必须分解为：

```text
sample-plan time
schema bench time
variant bench time
digest compare time
Engine open total time
expected total wall time
```

如果估时超过 gate 上限，即使 correctness 通过，也不能进入下一阶段。

## 阶段 Verdict

```text
P0 optimization: PASSED
SF1 validation: PASSED
SF30 type-only gate: FAILED / cold-import-blocked
SF30 full gate: PENDING
SF100 type-only gate: PASSED / reuse-store cached-plan gate
SF100 minimal matrix: PASSED / reuse-store bench-only matrix
```

## P0b import-many 代码级优化 (2026-06-17)

实现内容：

```text
src/snb/full_loader.rs
  - 新增 import_snb_full_multi(engines, csv_root, store_dirs)
  - SNB CSV 只解析一次；每条 edge fan-out 到多个 Engine/store
  - 原 import_snb_full 保留，旧 import 路径不变
  - 多 store 模式当前要求 SNB_SKIP_ADJ_CACHE=1，避免 vertex JSONL / edge-prop JSONL / adjacency cache 的单 store 语义被误用

src/snb/mod.rs
  - 导出 import_snb_full_multi

src/bin/lsmgraph.rs
  - 新增 import-many
  - 参数: --layout-store layout:/absolute/store/path，可重复
```

验证：

```text
cargo test --bin lsmgraph: 4 passed
cargo build --release: passed

SF1 smoke:
  run: remote-logs/w14-import-many-sf1-smoke-20260617-codex1
  command shape:
    SNB_SKIP_ADJ_CACHE=1 ./target/release/lsmgraph import-many
      --input /data/WorkSpace/ldbc-sf1/social_network
      --relation snb-full
      --layout-store schema:/data/WorkSpace/lsmgraph-rs/store/w14-import-many-sf1-smoke-20260617-codex1/schema
      --layout-store semantic:/data/WorkSpace/lsmgraph-rs/store/w14-import-many-sf1-smoke-20260617-codex1/semantic
  import-many wall time: 73s
  input_rows: 19,308,214
  directed_edges: 34,692,699
  store size: schema 1.4G, semantic 1.5G
  digest compare: checked=200 mismatches=0 verdict=PASS
  semantic open after sidecar buffering: total_open_s=4.029
```

效果判断：

```text
SF1 old separate imports: schema 42s + semantic 44s ~= 86s
SF1 import-many two stores: 73s
observed speedup: about 15% for two stores at SF1
```

这说明“同一次 CSV 扫描 fan-out 多 layout”是有效的，但它只减少 CSV parse/read 与 per-row dispatch 的重复；不能消除每个 layout 自己的 CSR materialization、manifest、semantic sidecar 写入成本。

## 10 小时判断 (2026-06-17)

```text
Fresh SF100 rebuild matrix inside 10h: NO / not safe to promise
Reason:
  existing SF100 import-only evidence:
    edge-type-only import: about 64min
    budg-b64 import: about 64min
    semantic import: about 118min
    semantic sidecar persist alone: 3109s (~51.8min)
  import-many can reduce repeated CSV scans, but each store still pays its own CSR/sidecar materialization.

Reuse existing SF100 stores + bench-only matrix inside 10h: LIKELY but still gated
Reason:
  stores already exist:
    store/qslsm-sf100-strong-baseline-20260610/schema
    store/w14-sf100-import-only-tuned-20260616-codex2/{edge-type-only,budg-b64,semantic}
  if no fresh import is performed, remaining work is sample-plan + storage-bench + digest compare.
  estimated wall time for 3-scenario minimal matrix: 6-9h
  stop condition: abort if SF100 type-only gate exceeds 2h or digest mismatch occurs.
```

## P0 optimization 交付与证据 (2026-06-17)

实现内容（沿用 codex 计划，duplicate semantic-index 调用一项已确认不存在）：

```text
1. Engine::open 阶段计时日志   src/graph.rs: log_open_phase_timings(), 由 LSMGRAPH_LOG_OPEN_PHASES=1 开启
   -> 输出 manifest_load / schema_load / rebuild_index / semantic_index / total_open
   -> 说明: open_inner 只调用一次 load_or_rebuild_semantic_indexes()，已被 SNB_SKIP_SEM_INDEX 守卫，
            codex 计划里 "移除重复调用" 一项 N/A（源码中本就只有一处）。
2. storage-bench --emit-result-digests   src/bin/lsmgraph.rs
   -> 每个 sample 输出 {edge_type, src, degree, dst_label, property_predicate_mode, result_count, result_digest}
   -> result_digest = 对排序后 visible EdgeRecord 的确定性 FNV-1a；另有 entry_result_digest 聚合
3. digest compare 脚本   baseline/sf100_digest_compare.py
   -> schema(baseline) vs variant 比 sample key / result_count / result_digest；mismatches!=0 或 checked=0 时退出非零
4. W14 runner   baseline/run_w14_semantic_necessity_20260615.sh
   -> run_bench 加 --emit-result-digests + LSMGRAPH_LOG_OPEN_PHASES；Step B 默认走 run_digest_compare
   -> neighbor-compare 降级为 debug fallback（digest mismatch 时自动跑小样本 preview，或 W14_NEIGHBOR_COMPARE=1 强制）
```

验证证据：

```text
cargo test --lib            : 64 passed
cargo test --bin lsmgraph   : 4 passed (含 result_digest 确定性/顺序无关/可检测变化 + entry_digest 折叠)
cargo test --lib dst_label / signature : graph::tests::dst_label_signature_filters_returned_edges + 3 个 csr 签名剪枝测试 passed
SF1 烟测 (baseline 当 schema, tuned 当 variant, edge-type 3, 20 samples):
  - Engine open phase 日志可见: total_open_s=0.003
  - digest compare PASS: checked=20 mismatches=0
  - 负向测试: 篡改一个 digest -> mismatches=1, exit=1, first_mismatch 正确
```

## SF1 validation gate 结果 (2026-06-17, run=w14-sf1-gate-20260617)

runner 已新增 `SCALE=sf1` 分支（从 `/data/WorkSpace/ldbc-sf1/social_network` 新鲜 import 到 `STORE_ROOT`）。本次缩减 `VARIANTS="schema semantic"`，三场景全跑：

```text
RUN_ID=w14-sf1-gate-20260617 SCALE=sf1 VARIANTS="schema semantic" \
  SCENARIOS="type-only property-required degree-class" SAMPLES=200 \
  bash baseline/run_w14_semantic_necessity_20260615.sh
```

correctness（digest compare，schema vs semantic）：

```text
type-only        : PASS checked=200 mismatches=0 entry_digest_mismatches=0
property-required: PASS checked=200 mismatches=0 entry_digest_mismatches=0
degree-class     : PASS checked=200 mismatches=0 entry_digest_mismatches=0
result_count sanity: type-only 200/200 非空 (1..63)，剪枝返回真实结果而非空集
```

timing 与 open phase（来自新加的 LSMGRAPH_LOG_OPEN_PHASES）：

```text
build 4s; import schema 42s; import semantic 44s; 每场景 sample-plan ~23s
TOTAL build+import+3 scenarios = 313s (~5.2min)  <= 15min 门槛 OK
schema  store open: total_open_s=0.001 (无 semantic index)
semantic store open: total_open_s≈75.1s, 100% 落在 semantic_index 阶段
```

发现与修复（open phase 日志直接定位）：

```text
现象: semantic store 每次 open ≈75s，全在 semantic_index 阶段；违反 "任一 Engine open <=60s" 子门槛。
根因: read_degree_directory_sidecar_inner 用裸 File 逐字段读 147MB sidecar
      (~11M entries x 3 read = ~33M syscalls)；写路径用 BufWriter，读路径没缓冲。
修复: src/graph.rs 用 BufReader 包裹 sidecar 读取（与写路径对称）。
验证: 同一 semantic store 重开 -> semantic_index_s 75.1 -> 4.68 (~16x)，cargo test --lib 64 passed。
结论: SF1 gate 修复后 PASS（correctness + open<=60s + 总耗时<=15min 全满足）。
```

> 备注：correctness 与读取 buffering 无关（同样的字节、同样的解析，degree_directory sidecar round-trip 测试仍通过），所以三场景 mismatches=0 的结论在修复前后都成立；修复只影响 open 耗时。SF30/SF100 的 semantic open 成本会按 sidecar 大小放大，本修复正落在该关键路径上。

## SF30 type-only gate 结果 (2026-06-17)

两次尝试均未放行 SF30 full / SF100：

```text
first attempt:
  run: remote-logs/w14-sf30-type-only-gate-20260617
  result: FAILED, 主动停止
  reason: 原 runner 会同时保留多个 SF30 stores，当前 /data free 只有约 285G，继续导入会压到 200G 安全线以下。

streaming retry:
  run: remote-logs/w14-sf30-type-only-streaming-20260617
  result: FAILED, 主动停止
  schema import: 20m43s
  sample-plan: 16m14s
  schema bench: 1s
  edge-type-only import: 已启动但未完成
  digest compare: not reached
  reason: 仅 schema import + sample-plan 已约 37min，edge-type-only 冷导入仍未完成，无法满足 SF30 type-only <=60min gate。
```

结论：

```text
compare optimization: effective at P0/SF1 (semantic open 75.1s -> 4.68s, ~16x)
SF30/SF100 readiness: not passed
primary blocker: cold import + missing reusable SF30 stores + /data headroom
SF100 status: BLOCKED
```

## 存储清理清单 (2026-06-17 08:30 CST)

当前资源快照：

```text
/data avail: 641G
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

优先可清理候选（不包含 `ldbc-sf10` 和 `tugraph_ldbc_snb`）：

| 路径 | 大小 | 建议 | 说明 |
|---|---:|---|---|
| `/data/WorkSpace/ldbc-sf300` | 293G | 可删候选 | 当前 W14/W6/W8 不依赖 SF300；删除前确认没有 SF300 作业。 |
| `/data/WorkSpace/lsmgraph-rs-w1-fix` | 43G | 可删候选 | 旧 worktree/store；当前主线在 `lsmgraph-rs`。 |
| `/data/WorkSpace/dgs/output` | 14G | 条件可删 | DGS 输出缓存；保留源码和必要输入。 |
| `/data/WorkSpace/dgs/target` | 3.7G | 条件可删 | Rust build artifact，可重建。 |
| `/data/WorkSpace/lsmgraph-rs/target` | 2.8G | 条件可删 | Rust build artifact，可重建，会增加下次编译时间。 |
| `/data/WorkSpace/lsmgraph-rs/store/w14-import-many-sf1-smoke-20260617-codex1` | 2.8G | 条件可删 | P0b smoke store；日志已保留，若要回收可删除。 |
| `/data/WorkSpace/lsmgraph-rs/store/sf10-base-graph` | 6.8G | 条件可删 | 可重建 base graph；若近期还要 SF10 验证则保留。 |
| `/data/WorkSpace/lsmgraph-rs/store/sf1-base-graph` | 698M | 条件可删 | 可重建 base graph。 |

不建议现在清理：

```text
/data/WorkSpace/lsmgraph-rs/store/sf100-base-graph   # 66G, 可重建但会拖慢 SF100 后续
/data/WorkSpace/lsmgraph-rs/store/sf30-base-graph    # 21G, 可重建但会拖慢 SF30 gate
```

## 当前禁止事项

```text
Do not start SF30 before SF1 passes.
Do not start SF100 before SF30 full passes.
Do not write remote-logs/<run_id>/PASSED before the corresponding gate is actually satisfied.
Do not increase timeout to hide compare/open cost regressions.
Do not use the old standalone neighbor-compare path as the default W14 Step B correctness runner unless digest compare mismatches and a debug fallback is required.
```

## 当前判断

```text
Current status: P0 PASSED, SF1 PASSED, SF30 type-only FAILED / cold-import-blocked
Reason: SF1 gate PASS (3 scenarios mismatches=0); open-phase log caught a 75s semantic open, root-caused to an unbuffered sidecar read and fixed (-> 4.7s); SF30 streaming retry showed cold import dominates and cannot meet <=60min without reusable stores or more disk headroom.
Next minimal action: clean approved storage candidates or restore/prebuild reusable SF30 stores, then rerun SF30 type-only gate. Do not start SF100 until SF30 full passes.
```
## 2026-06-17 Reuse-Store SF100 Override And Final Verdict

The cold-import SF30 path above remains a valid historical failure, but the execution policy was changed by user decision:

```text
Do not fresh rebuild SF100.
Reuse existing SF100 stores.
Run SF100 type-only gate first with a 2h upper bound.
If it passes, run the minimal matrix remainder.
```

Final validated runs:

| Done | Stage | Status | Start | End | Runtime | Run/log | Result |
|---|---|---|---|---|---:|---|---|
| [x] | SF100 type-only reuse-store cached-plan gate | PASSED | 2026-06-17 08:56 | 2026-06-17 09:20 | about 23min | `remote-logs/w14-sf100-type-only-reuse-cachedplan-20260617-codex2` | schema-vs-edge-type-only/budg-b64/semantic all `checked=100 mismatches=0 verdict=PASS`; `PASSED` marker written. |
| [x] | SF100 minimal reuse-store matrix remainder | PASSED | 2026-06-17 09:24 | 2026-06-17 13:05 | about 3h41m | `remote-logs/w14-sf100-minimal-reuse-20260617-codex1` | `property-required` and `degree-class` both completed from reused stores; all six digest compares `checked=5000 mismatches=0 verdict=PASS`; `PASSED` marker written at 13:50. |

Observed time breakdown:

```text
type-only gate total: about 23min
property-required sample-plan: about 56min
degree-class sample-plan: about 54min
degree-class schema bench: about 74m47s
degree-class edge-type-only bench: about 46s
degree-class budg-b64 bench: about 43s
degree-class semantic bench: about 12m05s, semantic total_open_s=674.880
total reuse-store SF100 gate + minimal matrix: about 4h09m from 08:56 to 13:05
fresh SF100 rebuild matrix: still not allowed / not necessary for this gate
```

Final verdict:

```text
Compare optimization validation: PASSED
SF100 reuse-store path: PASSED
Allowed next step: W14 summary verdict, then G2 C10 latency attribution and G3 S0 semantic dilution.
Do not run fresh SF100 rebuild unless a later plan explicitly requires it.
```
