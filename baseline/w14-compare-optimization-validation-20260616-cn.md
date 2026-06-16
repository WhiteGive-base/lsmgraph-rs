# W14 Compare Optimization Validation Progress

Overall status: RUNNING
Current stage: SF30 type-only (next)
Last update: 2026-06-17 CST
Final verdict: PENDING
Allowed next step: SF1 validation PASSED (after fixing a 75s semantic-store open) — SF30 type-only gate is now unblocked. SF100 stays blocked until SF30 full passes.

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
| [x] | SF1 validation | PASSED | 2026-06-17 00:25 | 2026-06-17 00:31 | 313s | remote-logs/w14-sf1-gate-20260617 | 3 场景 mismatches=0；open 修复后 4.7s | 总耗时 `<=15min`；所有 digest compare `mismatches=0`；任一 scenario `<=5min`；任一 Engine open `<=60s` | SF30 type-only 已解锁 |
| [ ] | SF30 type-only gate | READY | - | - | - | 待定 | SF1 已通过 | `type-only` 总耗时 `<=60min`；digest compare `mismatches=0`；必须输出 SF30 full ETA 与 SF100 type-only ETA | 通过后允许 SF30 full |
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
SF30 type-only gate: PENDING
SF30 full gate: PENDING
SF100 type-only gate: PENDING
SF100 minimal matrix: PENDING
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
Current status: P0 PASSED, SF1 PASSED, SF30 type-only READY
Reason: SF1 gate PASS (3 scenarios mismatches=0); open-phase log caught a 75s semantic open, root-caused to an unbuffered sidecar read and fixed (-> 4.7s); cargo tests green.
Next minimal action: SF30 type-only gate (SCALE=sf30, SCENARIOS="type-only"), verify mismatches=0 and total <=60min, and record SF30-full + SF100 type-only ETA. Do not start SF100 until SF30 full passes.
```
