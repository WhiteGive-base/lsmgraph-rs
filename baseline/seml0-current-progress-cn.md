# SemL0 G1/G2/G3 最新覆盖状态

## 2026-06-17 最新权威覆盖

Last gate update: 2026-06-17 14:07 CST

当前权威进度表：

```text
baseline/w14-mainline-to-s3-progress-20260617-cn.md
baseline/w14-compare-optimization-validation-20260616-cn.md
baseline/w14-final-verdict-20260617-cn.md
baseline/w14-to-s3-gate-decision-20260617-cn.md
```

| Gate | 名称 | 状态 | 当前结论 | 产物/日志 |
|---|---|---|---|---|
| G1 | W14 必要性实验 | PARTIAL GO / W14 run complete | SF100 reuse-store type-only gate 和 minimal matrix 均通过；correctness parity 和 schema-vs-pruned evidence 可用，但 W14 未证明 composite semantics 稳定优于 edge-type-only，因此不打开完整 S3。 | `remote-logs/w14-sf100-type-only-reuse-cachedplan-20260617-codex2`; `remote-logs/w14-sf100-minimal-reuse-20260617-codex1`; `baseline/w14-final-verdict-20260617-cn.md` |
| G2 | C10 延迟归因 | COMPLETED / FALLBACK-CLAIM-SAFETY | 已完成 offline attribution，复用 W6/W8/W14 JSON/stderr，没有启动 heavy runner。结论：可以解释 latency 矛盾并约束 claims，但不能写 broad latency superiority。 | `baseline/c10-latency-attribution-progress-20260616-cn.md`; `baseline/c10-latency-attribution-summary-20260617-cn.md`; `baseline/c10-latency-attribution-summary-20260617.tsv` |
| G3 | S0 semantic dilution 诊断 | COMPLETED / FALLBACK-PROXY-SIGNAL | 已完成 offline proxy diagnosis。W14 degree-class 给强 proxy signal，W8 给中等 proxy signal，W6 core 弱；不足以启动 S1/S2/S3 或完整 S3。 | `baseline/s0-semantic-dilution-progress-20260616-cn.md`; `baseline/s0-semantic-dilution-summary-20260617-cn.md`; `baseline/s0-semantic-dilution-summary-20260617.tsv` |

最新已验证事实：

```text
SF100 type-only reuse-store cached-plan gate:
  runtime: about 23min
  digest compare: schema-vs-edge-type-only/budg-b64/semantic checked=100 mismatches=0

SF100 minimal reuse-store matrix remainder:
  runtime: about 3h41m
  scenarios: property-required, degree-class
  digest compare: all six compares checked=5000 mismatches=0 verdict=PASS
  marker: remote-logs/w14-sf100-minimal-reuse-20260617-codex1/PASSED written at 13:50

G2/C10:
  offline attribution completed.
  verdict: FALLBACK / CLAIM-SAFETY.
  summary: baseline/c10-latency-attribution-summary-20260617-cn.md

S3 decision:
  full S3 implementation remains BLOCKED.
  S0 completed as FALLBACK / PROXY SIGNAL.
  next minimal action is W10 claim safety / writing freeze.
```

Last gate update: 2026-06-16 23:38 CST

当前主线已经改为三段 gate 收敛：

| Gate | 名称 | 状态 | 当前阶段 | 执行时间/耗时 | 产物/日志 |
|---|---|---|---|---|---|
| G1 | W14 必要性实验 | FALLBACK retained / compare optimization validation NOT_STARTED | SF100 tuned split Step A 已完成并校验；Step B minimal scenario matrix 停在 `compare-type-only-semantic`，7200s timeout；当前转入 compare 优化验证阶段，SF30/SF100 暂停 | Step A 约 4h07m；Step B 15:26 启动、22:19 失败，总约 6h53m；新验证阶段尚未开始 | `baseline/seml0-mainline-gates-progress-20260616-cn.md`; `baseline/w14-sf100-split-tuning-progress-20260616-cn.md`; `baseline/w14-compare-optimization-validation-20260616-cn.md`; `remote-logs/w14-sf100-scenarios-min-tuned-20260616-codex1/FAILED` |
| G2 | C10 延迟归因 | PLANNED / LIGHTWEIGHT ONLY | G1 Step A/Step B 活跃期间只做离线日志盘点和文档，不启动 heavy runner | heavy runner 0；等待 G1 verdict 后计时 | `baseline/c10-latency-attribution-progress-20260616-cn.md` |
| G3 | S0 semantic dilution 诊断 | PLANNED / BLOCKED BY G1+G2 | 不启动新实验；等 C10 verdict 后复用已有 logs/stores 做诊断 | heavy runner 0；等待 G2 verdict 后计时 | `baseline/s0-semantic-dilution-progress-20260616-cn.md` |

统一 gate 进度表：

```text
baseline/seml0-mainline-gates-progress-20260616-cn.md
```

当前运行快照：

```text
time: 2026-06-16 23:38 CST
active run: remote-logs/w14-sf100-import-only-tuned-20260616-codex2
active Step B run: remote-logs/w14-sf100-scenarios-min-tuned-20260616-codex1
active store: store/w14-sf100-import-only-tuned-20260616-codex2
DONE/IMPORT_ONLY_DONE: 2026-06-16T15:13:33+08:00
FAILED: Step B 2026-06-16T22:19:17+08:00 unexpected exit status=124
completed: schema reuse, edge-type-only import JSON valid, budg-b64 import JSON valid, semantic import JSON valid
running: none for Step B
not started: G2/C10 heavy runner, G3/S0 targeted experiment
/data free: 239G
MemAvailable: about 448GiB
policy: do not start SF30/SF100 until `baseline/w14-compare-optimization-validation-20260616-cn.md` passes its gates
```

当前提速状态：

```text
已做：SNB_SKIP_ADJ_CACHE=1、W14_IMPORT_ONLY=1、schema store reuse、Step A/Step B split、sample-plan 专用 SNB_SKIP_SEM_INDEX、Step B 计划 CSR_METADATA_CACHE_ENTRIES=32768、streaming degree sidecar source patch。
已验证：SF1 tuned import 约 2.2x；SF30 tuned edge-type-only import 约 2.8x。
当前不做：SF30/SF100 heavy run、SF100 variants 并发导入、运行中重编译 target-cpu/LTO、提前删除 Step A stores。
当前必须先做：P0 compare optimization validation；通过前不允许进入 SF30/SF100。
```

W14 compare 优化验证入口：

```text
progress doc: baseline/w14-compare-optimization-validation-20260616-cn.md
overall status: NOT_STARTED
current stage: P0
final verdict: PENDING
required before SF30/SF100: prove optimization effective, record Engine open time, digest compare mismatches=0, and estimate SF30/SF100 total runtime.
```

G1/W14 2026-06-16 08:38 CST 检查：

```text
DONE: absent
FAILED: absent
current variant: edge-type-only
current stage: SF100 import finished input scan; building adjacency cache from import stream
elapsed: about 2h13m
MemAvailable: about 156GiB
/data free: 397G
store root size: 313G
判定: RUNNING, but disk-risk watch required
```

G1/W14 2026-06-16 08:47 CST 检查：

```text
DONE: absent
FAILED: absent
current variant: edge-type-only
current stage: adjacency build complete; import process still finalizing; import JSON still 0 bytes
elapsed: about 2h23m
MemAvailable: about 219GiB
/data free: 397G
store root size: 313G
guard result: did not stop runner because edge-type-only-import.json had not landed yet
判定: RUNNING; no budg-b64 import has started
```

G1/W14 2026-06-16 09:25 CST 检查：

```text
DONE: absent
FAILED: 2026-06-16T09:25:04+08:00 unexpected exit status=124
failure stage: SF100 edge-type-only import
failure reason: single import exceeded 10800s timeout
import JSON: 0 bytes
runner tail: stopped after adjacency build complete
processes: no child process remains
MemAvailable after failure: about 447GiB
/data free after failure: 397G
partial store: 313G at store/w14-semantic-necessity-sf100-formal-20260616-codex1/edge-type-only
判定: G1 current final = FALLBACK; no SF100 W14 matrix claim
```

G1/W14 2026-06-16 09:27 CST 清理：

```text
deleted: store/w14-semantic-necessity-sf100-formal-20260616-codex1
deleted size: about 313G
/data free after cleanup: 709G
kept logs: remote-logs/w14-semantic-necessity-sf100-formal-20260616-codex1
```

G2/C10 和 G3/S0 已纳入计划。09:25 的 all-in-one SF100 formal 失败已经作为历史 fallback 记录；12:18 当前主线是 W14 tuned split retry，尚未结束。只有 Step A import-only 和 Step B minimal matrix 给出 verdict 后，才进入 C10 heavy runner 或 S0 targeted experiment。后续每个 gate 都必须写详细 progress：run id、命令、当前阶段、资源快照、完成项、未完成项、失败原因、保留/删除数据、下一步最小动作。

# W14 最新覆盖状态

Last W14 update: 2026-06-16 08:38 CST

当前主线 W14 已完成代码、清理、必要测试和 SF30 smoke；SF30 verdict 是 `FALLBACK`，不是 `GO`。目标模式续跑审计后，补充启动一个有时间盒的 SF100 formal attempt，以满足原 objective 中的 SF100 composite matrix 要求。

| Done | 项目 | 当前状态 | 产物 |
|---|---|---|---|
| [x] | W14 清理计划 | 已执行；`/tmp/hadoop-root` root-owned 保留 | `baseline/w14-semantic-necessity-progress-20260615-cn.md` |
| [x] | S3 留档计划 | 已完成；定位为同一篇论文的 future-work / optional-strengthening backlog，当前不施工 | `baseline/seml0-s3-full-plan-20260615-cn.md` |
| [x] | W14 代码实现 | 已同步远端并通过测试 | `src/semantic.rs`, `src/csr/format.rs`, `src/metrics.rs`, `src/graph.rs`, `src/bin/lsmgraph.rs` |
| [x] | W14 runner/summarizer | 已完成；runner 支持逗号分隔 `SCENARIOS` | `baseline/run_w14_semantic_necessity_20260615.sh`, `baseline/summarize_w14_semantic_necessity_20260615.py` |
| [x] | cargo test | `cargo test --lib` 64 passed；3 个目标测试 passed | remote cargo output |
| [x] | SF30 smoke | DONE；3 scenarios x 4 variants；all compares `mismatches=0` | `remote-logs/w14-semantic-necessity-sf30-smoke-20260615-codex4` |
| [x] | W14 SF30 summary verdict | `FALLBACK`；correctness/telemetry 有效，但没有稳定证明 composite semantics 优于 edge-type-only | `baseline/w14-semantic-necessity-summary-20260615-cn.md` |
| [ ] | W14 SF100 formal | planned/running；6 scenarios x 4 variants；`SAMPLES=500`, `REPEATS=1`, reuse existing SF100 schema | `remote-logs/w14-semantic-necessity-sf100-formal-20260616-codex1` |

W14 当前安全写法：

```text
edge-type-only 是 SemL0 的一维特例；SemL0 提供 exact/conservative semantic telemetry，并能在 property-required 等 query semantics 下解释额外 pruning surface。
```

W14 当前不能写：

```text
SemL0 composite semantics 已稳定优于 edge-type-only；SemL0 已证明端到端 latency advantage。
```

下一步最小动作：监控 W14 SF100 formal attempt。若 DONE，则再生 summary 并给出最终 GO/FALLBACK/NO-GO；若 FAILED，则记录失败原因和保留/删除 store 状态，并以 SF30 `FALLBACK` 作为当前论文边界。

---

# SemL0 当前收敛进度表

Last update: 2026-06-15 16:52 CST

当前运行中的长任务：

| 阶段 | 状态 | 当前进度 | 完成后动作 |
|---|---:|---|---|
| none | IDLE | W6/W7/W8/W9/W13 证据块完成；W10 source-ready paper 已装配；当前没有 SF30/SF100 长任务在跑。 | 停止扩实验，进入非实验投稿打包：template/PDF/page/artifact bundle。 |

并发策略：

| Done | 并发项 | 状态 | 规则 |
|---|---|---|---|
| [x] | 资源评估 | 已完成 | 64 核、503GiB RAM，CPU/内存可并发；`/data` 是瓶颈。 |
| [x] | W7 smoke/formal | DONE | synthetic smoke DONE；real-SF30-derived formal DONE, Gate=GO. Formal run id: `w7-sf30-workload-shift-formal-20260615-1536`. |
| [x] | W9 smoke | DONE | 6 checkpoints and final `done` event parsed; writer_errors=0, slow_ops=0; private store copy used. Run id: `w9-steady-state-smoke-20260613-1653`. |
| [x] | W8/SF30 并发禁用 | 已解除 | W6 已 DONE；W8 formal 已单独启动，不与 W7/W9 并发。 |
| [x] | W8 property + 2-hop formal | DONE | Run id `w8-property-2hop-20260614-2025`; all schema/budg-b64/semantic property and 2-hop outputs validated; summary and caveated conclusion written. |
| [x] | W9 formal steady-state | DONE | Run id `w9-steady-state-formal-20260615-1200`; schema/budg-b64/semantic all have final `done` events; `DONE` exists, `FAILED` absent. |
| [x] | W13 lightweight concurrent run | DONE | Run id: `w13-schema-evolution-20260614-0004`; 10 schema-evolution tests passed; `DONE`, `tests.tsv`, and `summary.md` generated. |

## 阶段总览

| Done | 阶段 | 当前状态 | 论文处理 |
|---|---|---|---|
| [x] | W6 SF100 matrix + Gate 1/2 | DONE | Gate 1 = FALLBACK；Gate 2 = GO。主文可写 RSS measured result；latency advantage 要保守。 |
| [x] | W7 self-tuning/workload shift | DONE with caveat | 新增 real-SF30-derived formal runner；Gate=GO。可写 query-signature-driven semantic compaction / workload-shift adaptation，但必须说明不是 full-store production trace。 |
| [x] | W8 property + 2-hop | DONE | ready-for-paper with caveats；2-hop 不能写 universal candidate reduction。 |
| [x] | W9 mixed read/write steady-state | DONE | ready-for-paper with caveats；可写 mixed read/write workload coverage 和按 delta 表限定的优势。 |
| [x] | W13 schema evolution | DONE | 可写 additive/fixed-width boundary 内的 schema evolution correctness。 |
| [x] | W10 writing freeze | SOURCE-READY; VENUE/PDF PACKAGING OPEN | `paper/main.tex` 已替换为 W10 证据冻结版；`paper/tables/w10-*.tex` 由 renderer 生成；artifact checklist/package manifest 和 `artifact/README-W10-SemL0.md` 已刷新。剩余 template/PDF/page/venue-ready archive 是投稿工程 blocker，不是实验缺口。 |

## W6：SF100 全矩阵与 Gate 1/2

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | 执行卡与停止条件确认 | 已完成 | 以 `baseline/seml0-w6-launch-and-finishline-20260613-cn.md` 和本 progress 文档为准。 |
| [x] | 资源检查 | 已完成 | 启动前 MemAvailable 448GiB，`/data` 649G free，无第二个 SF100 任务。 |
| [x] | 清理 residue stores | 已完成 | 仅删除已复核的 W2/W4/W5/W6 smoke/profile residue；保留 base graph 和旧 SF100 参照 store。 |
| [x] | W6 runner 修正 | 已完成 | `RUN_COMPARE=1`；保留 `schema`/`naive`；compare 使用 naive anchor；`bash -n` 通过。 |
| [x] | W6 启动与 heartbeat | 已完成 | run id: `w6-sf100-matrix-20260613-132325`；日志在 `remote-logs/w6-sf100-matrix-20260613-132325/`。 |
| [x] | `schema` import | 已完成 | 14:27:58 完成；wall clock 1:04:32；store 约 134G。 |
| [x] | sample-plan | 已完成 | 15:30:55 完成；`sample-plan-core-s5000.json` 存在且 JSON valid。 |
| [x] | `schema` bench + sentinel A1 | 已完成 | `schema-bench.json`、`sentinel-A1.json` 均 valid。 |
| [x] | `naive` import/bench | 已完成 | `naive` import done; `naive-bench.json` valid. |
| [x] | `schema` vs `naive` compare | 已完成 | `compare-naive-vs-schema.json` valid, checked=45000, mismatches=0. |
| [x] | `kv-lsm` variant | 已完成 | import/bench/compare 完成；`compare-naive-vs-kv-lsm.json` valid, checked=45000, mismatches=0；非 anchor store 已删除。 |
| [x] | `edge-type-only` variant | 已完成 | import/bench/compare 完成；`compare-naive-vs-edge-type-only.json` valid, checked=45000, mismatches=0；非 anchor store 已删除。 |
| [x] | sentinel B | 已完成 | `sentinel-B.json` valid。 |
| [x] | `semantic` variant | 已完成 | import/bench/compare 完成；`compare-naive-vs-semantic.json` valid, checked=45000, mismatches=0；非 anchor store 已删除。 |
| [x] | `budg-b64` variant | 已完成 | import/bench/compare 完成；`compare-naive-vs-budg-b64.json` valid, checked=45000, mismatches=0；非 anchor store 已删除。 |
| [x] | `budg-b256` variant | 已完成 | import/bench/compare 完成；`compare-naive-vs-budg-b256.json` valid, checked=45000, mismatches=0；非 anchor store 已删除。 |
| [x] | `budg-b1024` variant | 已完成 | import/bench/compare 完成；`compare-naive-vs-budg-b1024.json` valid, checked=45000, mismatches=0；非 anchor store 已删除。 |
| [x] | `oracle` variant | 已完成 | import/bench/compare 完成；`compare-naive-vs-oracle.json` valid, checked=45000, mismatches=0；非 anchor store 已删除。 |
| [x] | sentinel A2 | 已完成 | `sentinel-A2.json` valid。 |
| [x] | W6 DONE marker | 已完成 | `remote-logs/w6-sf100-matrix-20260613-132325/DONE` 存在；无 `FAILED` marker。 |
| [x] | W6 matrix report | 已完成 | `baseline/sf100-matrix-20260613-cn.md` 从原始 JSON/TSV 再生；脚本：`baseline/summarize_w6_sf100_matrix_20260613.py`。 |
| [x] | Gate 1 判定 | final FALLBACK | b64 比 schema 快约 11.15%，但 1-stddev 区间重叠；主文不要写稳定 latency advantage。 |
| [x] | Gate 2 判定 | final GO | schema/naive RSS=0.99x，budg-b64/naive RSS=0.91x；RSS 可作为 measured main result，但按实测表述。 |

## W7：self-tuning / workload shift

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | W7 progress doc | 已建立 | `baseline/w7-workload-shift-progress-20260613-cn.md`。 |
| [x] | SF1-class smoke | DONE + valid | Run id `w7-workload-shift-smoke-20260613-1653`；JSON valid。 |
| [x] | 现有 controlled evidence 定位 | 已记录 | 可参考 `remote-logs/p6-sustained-feedback-20260612` 与 `remote-logs/p7-02-feedback-workload-shift-20260604`；只能作为 controlled/self-tuning evidence。 |
| [x] | SF30-derived formal workload-shift | DONE | Run id `w7-sf30-workload-shift-formal-20260615-1536`；输入来自 SF30 CSV，三变体 `feedback-only / static-budgeted / no-feedback`；`DONE` exists，`FAILED` absent。 |
| [x] | W7 formal summary | DONE | `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`；Gate=GO。feedback-only phase A/B hot compaction flush=1，selected range changed；no-feedback 无 hot compaction。 |
| [x] | W7 论文结论 | ready-for-paper with caveat | 可写 real-SF30-derived query-signature-driven semantic compaction；不要写 full-store production self-tuning trace。 |

## W8：property + 2-hop SF30

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | W8 runner 启动 | DONE formal | Run id `w8-property-2hop-20260614-2025`，PID 358466；runner: `baseline/run_w8_property_2hop_20260612.sh`。 |
| [x] | W8 资源检查 | 持续通过 | 完成后 `/data` 307G free，MemAvailable 448GiB；未触发 `/data` <200GiB 或 MemAvailable <80GiB 停止线。 |
| [x] | wrapped JSON 规则确认 | 已确认 | W8 输出带 runner start/done 行；验证时剥离首尾 runner 行后用 `python3 -m json.tool`。 |
| [x] | `schema` import | DONE + valid | `remote-logs/w8-property-2hop-20260614-2025/schema/import.json` payload valid；store 约 113G。 |
| [x] | `schema` required-property | DONE + valid | `schema/property-required-property.json` payload valid。 |
| [x] | `schema` presence | DONE + valid | `schema/property-presence.json` payload valid。 |
| [x] | `schema` equality | DONE + valid | `schema/property-equality.json` payload valid。 |
| [x] | `schema` absent-default | DONE + valid | `schema/property-absent-default.json` payload valid。 |
| [x] | `schema` 2-hop | DONE + valid | `schema/2hop-typed.json` payload valid；该阶段约 1h50m。 |
| [x] | `budg-b64` import | DONE + valid | `budg-b64/import.json` payload valid。 |
| [x] | `budg-b64` required-property | DONE + valid | `budg-b64/property-required-property.json` payload valid。 |
| [x] | `budg-b64` presence | DONE + valid | `budg-b64/property-presence.json` payload valid。 |
| [x] | `budg-b64` equality | DONE + valid | `budg-b64/property-equality.json` payload valid。 |
| [x] | `budg-b64` absent-default | DONE + valid | `budg-b64/property-absent-default.json` payload valid。 |
| [x] | `budg-b64` 2-hop | DONE + valid | `budg-b64/2hop-typed.json` payload valid；该阶段约 1h27m。 |
| [x] | `semantic` import | DONE + valid | `semantic/import.json` payload valid。 |
| [x] | `semantic` required-property | DONE + valid | `semantic/property-required-property.json` payload valid。 |
| [x] | `semantic` presence | DONE + valid | `semantic/property-presence.json` payload valid。 |
| [x] | `semantic` equality | DONE + valid | `semantic/property-equality.json` payload valid。 |
| [x] | `semantic` absent-default | DONE + valid | `semantic/property-absent-default.json` payload valid。 |
| [x] | `semantic` 2-hop | DONE + valid | `semantic/2hop-typed.json` payload valid；该阶段约 2h03m。 |
| [x] | W8 DONE marker | DONE | `remote-logs/w8-property-2hop-20260614-2025/DONE` exists；`FAILED` absent。 |
| [x] | W8 summary table | DONE | `baseline/w8-property-2hop-summary-20260615-cn.md` 从原始 JSON 再生；脚本：`baseline/summarize_w8_property_2hop_20260615.py`。 |
| [x] | W8 结论 | ready-for-paper with caveats | property predicates 支持 candidate/body/read/elapsed 改善；2-hop 支持 body/read/elapsed 改善，但 candidate L0 高于 schema，不能写 universal 2-hop candidate reduction。 |

## W9：mixed read/write steady-state SF30

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | W9 smoke | DONE + valid | Run id `w9-steady-state-smoke-20260613-1653`；6 checkpoints + final `done` parsed；writer_errors=0，slow_ops=0。 |
| [x] | runner/store strategy | 已修正并记录 | `baseline/run_w9_steady_state_20260612.sh` 支持 `SCHEMA_STORE`/`BUDG_B64_STORE`/`SEMANTIC_STORE`；W9 使用 W8 frozen stores as mutable starting stores。 |
| [x] | formal run | DONE | Run id `w9-steady-state-formal-20260615-1200`；`remote-logs/w9-steady-state-formal-20260615-1200/DONE` exists；`FAILED` absent。 |
| [x] | `schema` steady-state | DONE + valid | 8 JSONL rows: start + 6 checkpoints + final done；total queries=293,502；writer_errors=0，slow_ops=0。 |
| [x] | `budg-b64` steady-state | DONE + valid | 8 JSONL rows: start + 6 checkpoints + final done；total queries=293,411；writer_errors=0，slow_ops=0。 |
| [x] | `semantic` steady-state | DONE + valid | 8 JSONL rows: start + 6 checkpoints + final done；total queries=295,251；writer_errors=0，slow_ops=0。 |
| [x] | W9 summary table | DONE | `baseline/w9-steady-state-summary-20260615-cn.md` 从 raw JSONL 再生；脚本：`baseline/summarize_w9_steady_state_20260615.py`。 |
| [x] | W9 结论 | ready-for-paper with caveats | 可写 mixed read/write coverage；只能按 delta 表写优势：budg-b64 candidate L0 -4.1%、p99 -1.2%、p50 +1.8%；semantic p50 -63.9%、p99 -82.5%，但 candidate L0 +86.3%、L0 files +93.4%。 |

## W13：schema evolution

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | W13 runner/test | DONE | Run id `w13-schema-evolution-20260614-0004`。 |
| [x] | correctness tests | DONE | 10 schema-evolution tests passed。 |
| [x] | markers/artifacts | DONE | `remote-logs/w13-schema-evolution-20260614-0004/DONE` exists；`tests.tsv` 与 `summary.md` generated。 |
| [x] | supplemental sanity rerun | DONE | Run id `w13-schema-evolution-20260615-143649`；由 help-check 误触发 runner，10 tests passed；只作 supplemental verification。 |
| [x] | W13 论文边界 | ready-for-paper with boundary | 可写 schema epoch、旧段可读、stable edge_type id 下 exact pruning safe；不要写 full migration/reclamation。 |

## W10：writing freeze / claims safety

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | W10 progress doc | 已建立 | `baseline/w10-writing-freeze-progress-20260613-cn.md`。 |
| [x] | W10 start preconditions | READY | W6/W7/W8/W9/W13 已 DONE；W7 是 real-SF30-derived formal evidence with caveat。 |
| [x] | evidence inventory | DONE | `baseline/w10-evidence-inventory-20260615-cn.md` 汇总 W6/W7/W8/W9/W13 的 raw logs、summary scripts、DONE/FAILED markers。 |
| [x] | table regeneration audit + paper table rewrite | DONE | Inventory 已把可再生表格映射到 raw JSON/TSV/JSONL 和 summary command；`baseline/render_w10_sigmod_tables_20260615.py` 已生成 `paper/tables/w10-*.tex`。 |
| [x] | measured/simulated/TODO labels | DONE in W10 draft | `baseline/seml0-w10-submission-freeze-draft-cn-20260615.md` 中主表行标为 measured / controlled；旧 simulated kv-style 行已用 W6 measured `kv-lsm` 替换。 |
| [x] | Gate 1 claim rewrite | DONE in W10 draft | Gate 1=FALLBACK；W10 draft 不写 stable latency superiority，只写 candidate/read-amp 和避免 unsafe latency claim。 |
| [x] | Gate 2 RSS rewrite | DONE in W10 draft | Gate 2=GO；RSS 作为 measured result 写入，schema/naive=0.99x，`budg-b64`/naive=0.91x。 |
| [x] | W7 claim decision | DONE | W7 formal Gate=GO；W10 draft/caveat/artifact 已更新为 real-SF30-derived formal with caveat。 |
| [x] | W8 claim decision | DONE in W10 draft | property claim 可写；2-hop 只写 body/read/elapsed 改善，不写 universal candidate reduction。 |
| [x] | W9 claim decision | DONE in W10 draft | mixed read/write 可写 workload coverage；优势按 W9 delta 表限定。 |
| [x] | W13 claim decision | DONE in W10 draft | 只写 additive/fixed-width/schema-evolution correctness 边界。 |
| [x] | paper draft update | W10-safe draft DONE | `baseline/seml0-w10-submission-freeze-draft-cn-20260615.md` 已生成；旧 `seml0-linux-main-paper-draft-cn-20260611.md` 不再作为安全事实源。 |
| [x] | appendix draft | SOURCE-READY | `paper/main.tex` appendix 已包含 artifact map、reproduction commands、submission gate status；`paper/tables/w10-artifact-map.tex` 已生成。 |
| [x] | artifact checklist draft | DONE + inserted | `baseline/w10-artifact-checklist-20260615-cn.md` 已完成，并在 W10-safe draft 中加入 artifact checklist insert；`artifact/README-W10-SemL0.md` 已完成。 |
| [x] | final caveat table draft | DONE + inserted | `baseline/w10-final-caveat-table-20260615-cn.md` 已完成，并在 W10-safe draft 中加入 caveat table。 |
| [x] | W10 current verdict | recorded | `baseline/w10-writing-freeze-progress-20260613-cn.md` 已更新：minimum/recommended evidence lines are source-ready with caveats；final submission packaging 仍需 template/PDF/page/artifact bundle。 |
| [x] | completion audit | DONE | `baseline/seml0-completion-audit-20260615-cn.md` 已创建；结论：Tier 1+2 到 source-ready with caveats，剩余 template/PDF/archive 是非实验投稿工程。 |

## Tier 1 最低可投线

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | W6 | DONE | SF100 matrix 完成，Gate 1/2 判定完成。 |
| [x] | W13 | DONE | Run id: `w13-schema-evolution-20260614-0004`; 10 schema-evolution tests passed; `remote-logs/w13-schema-evolution-20260614-0004/DONE` exists; `summary.md` generated. |
| [x] | W10 | SOURCE-READY | W10-safe draft、claims 安全化、paper/main.tex、W10 TeX tables、appendix/artifact README、当前 verdict 已完成。Progress: `baseline/w10-writing-freeze-progress-20260613-cn.md`。 |
| [x] | 最低可投线 | source-ready with narrowed claims | W6 + W13 + W10 已完成到 source-ready；venue-specific PDF/template/archive 另列为非实验投稿工程。 |

## Tier 2 推荐投稿线

| Done | 项目 | 当前状态 | 勾选条件 / 产物 |
|---|---|---|---|
| [x] | W7 self-tuning | DONE with caveat | Run id `w7-sf30-workload-shift-formal-20260615-1536`; summary `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`; Gate=GO. |
| [x] | W8 property + 2-hop | DONE; ready-for-paper with caveats | 细项见上方 W8 表。全部 raw outputs 已完成并验证；summary table 和 W8 结论已完成。Progress: `baseline/w8-property-2hop-progress-20260613-cn.md`；summary: `baseline/w8-property-2hop-summary-20260615-cn.md`。 |
| [x] | W9 mixed read/write steady-state | DONE; ready-for-paper with caveats | Run id `w9-steady-state-formal-20260615-1200`; schema/budg-b64/semantic all done and valid. Summary: `baseline/w9-steady-state-summary-20260615-cn.md`；script: `baseline/summarize_w9_steady_state_20260615.py`。 |
| [x] | W13 schema evolution | DONE | 同 Tier 1；用于关闭差异化与 correctness 追问。 |
| [x] | W10 writing freeze | SOURCE-READY; VENUE/PDF PACKAGING OPEN | Paper-safety pass 1+2 完成；`paper/main.tex`、W10 appendix/source tables、轻量 artifact README 已装配。剩余 template/PDF/page/venue-ready archive 是非实验投稿工程。 |
| [x] | 推荐投稿线 | source-ready with caveats | W6 + W7 + W8 + W9 + W13 证据块已完成；W10 source-ready paper 和轻量 artifact README 已完成；最终 PDF/template/venue-ready archive 仍未完成但不要求扩实验。 |

## 停止线

| Done | 项目 | 当前状态 | 规则 |
|---|---|---|---|
| [x] | 停止扩实验 | 已达到 | W6/W7/W8/W9/W13 证据块已完成；不再启动 W11/W12 或新外部实验，进入 W10 写作收敛。 |
| [x] | 进入投稿写作收敛 | 已进入；source-ready | 停止扩实验已生效；W10 claims 安全化、source paper/appendix、轻量 artifact README 已完成，最终 PDF/template/venue-ready archive 尚未完成。 |

## 当前最小下一步

1. 读取 `baseline/seml0-completion-audit-20260615-cn.md` 做最终决策：当前 SemL0 收敛目标已到 source-ready；venue/PDF/archive 是非实验 blocker。
2. 如要继续到可上传包，只做 template/PDF/page-budget/visual-inspection/venue-ready archive，不再改实验矩阵。
3. 不再扩新实验；W11/W12 和外部系统新跑批全部停止。
