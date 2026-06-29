# SemL0: 面向 LSM 动态属性图的查询语义 L0 设计

> W10 submission-freeze draft, claims-safe version, 2026-06-15.
> This draft supersedes the unsafe quantitative claims in `baseline/seml0-linux-main-paper-draft-cn-20260611.md`.
> Scope: freeze evidence, title, evaluation tables, caveats, and artifact hooks for a SIGMOD-style submission draft.

## 当前投稿口径

本文的安全标题应收窄为 `Query-Semantic L0 Design`，而不是更宽的 `Query-Semantic Physical Design`。原因是当前强证据集中在 L0 segment metadata、read-time pruning、budgeted L0 materialization、real-SF30-derived workload-shift evidence、mixed workload coverage 和 bounded schema evolution。W7 self-tuning 已有 formal Gate=GO，但口径必须限定为 real-SF30-derived workload，而不是 full-store production trace。

可以写进主文的核心 claim：

1. SemL0 将属性图查询语义写入 L0 segment metadata，在 exact-proof 规则下减少 L0 read amplification。
2. W6 SF100 证明 schema/edge-type/budgeted SemL0 相对 naive 大幅降低 candidate L0；`kv-lsm` 行是 measured，不再使用 simulated kv-style row。
3. Gate 1 = FALLBACK：`budg-b64` 均值延迟低于 schema，但 1-stddev 区间重叠，不能写 stable latency superiority。
4. Gate 2 = GO：schema 和 `budg-b64` RSS 与 naive 同量级，可以作为 measured main result。
5. W8 证明 property predicate 和 2-hop workload 被覆盖；2-hop 只能写 body/read/elapsed 改善，不能写 universal candidate reduction。
6. W9 证明 mixed read/write steady-state 覆盖；优势必须按 delta 表限定。
7. W13 证明 additive/fixed-width/schema-epoch 边界内的 schema evolution correctness；不能写 full migration/reclamation。
8. W7 证明 query-signature-driven semantic compaction 能在 real-SF30-derived workload shift 中迁移到新的热点语义分区；不能写 full-store production self-tuning trace。

## 摘要草稿

基于 LSM 的动态图存储适合高频更新，但 L0 段重叠会让语义很窄的属性图查询探测大量无关段。SemL0 提出一种面向 LSM 动态属性图的查询语义 L0 设计：将 source label、edge type、degree class、property presence 和 schema epoch 编码进 L0 segment metadata，并在读取时只用 exact metadata proof 裁剪不可能匹配的段。mixed、unknown、legacy、tombstone-sensitive 或 schema-uncertain 段全部保守读取，因此 SemL0 的失败模式是多读而不是漏读。

SemL0 进一步使用 budgeted materialization，在 schema-style 布局和 full semantic 布局之间选择成本可控的工作点。真实 LDBC SNB SF100 W6 矩阵显示，schema、edge-type-only 和 budgeted SemL0 相对 naive 显著降低 candidate L0；所有 W6 compare 行均为 checked=45,000、mismatches=0。`budg-b64` 均值延迟低于 schema，但 Gate 1 判定为 FALLBACK，因为 1-stddev 区间重叠；本文不将其表述为稳定延迟优势。Gate 2 判定为 GO：schema/naive RSS=0.99x，`budg-b64`/naive RSS=0.91x。补充的 W7/W8/W9/W13 证据覆盖 workload-shift self-tuning、property/2-hop、mixed read/write steady-state 和 bounded schema evolution。W7 的安全口径是 real-SF30-derived formal evidence，不是 full-store production trace。

## 贡献写法

1. 查询语义 L0 元数据。SemL0 把属性图查询签名映射为 storage-facing `GraphAccessSignature`，并在 L0 段上记录 source label、edge type、degree class、property summary 和 schema epoch。
2. exact-proof pruning。SemL0 只在元数据能够证明 disjointness 或 exact absence 时跳过段；unknown/mixed/legacy/tombstone/schema-uncertain 段保守读取。
3. budgeted L0 materialization。SemL0 以 schema-style 为安全低成本基线，以 full semantic 为设计端点，通过预算选择高价值语义分区，避免过度分段。
4. evidence-backed dynamic boundaries。W7/W8/W9/W13 证明 workload-shift feedback、property/2-hop、mixed read/write 和 schema evolution 的实现边界；W7 feedback 以 real-SF30-derived formal evidence 呈现。

## Related-Work Positioning: BACH vs SemL0

BACH 可以被写成 SemL0 的清晰动机，而不是直接竞争同一个机制层。BACH 看到的是 AL vs CSR 的物理连续性问题：当查询已经落到相关 CSR 区域后，CSR 让类似 `Like.time` 的访问更容易顺序扫。SemL0 处理的是更早一层的 LSM L0 segment admission/pruning 问题：这些 CSR-like segment 里，哪些由于 source/destination label、edge type、degree class、property summary 或 schema epoch，能够被证明一定不可能满足当前 property-graph query signature，因此根本不应被读。

安全写法：BACH improves physical scan locality; SemL0 adds storage-level semantic pruning before the scan. 这能把 SemL0 的创新点落在 `query-signature-driven semantic compaction/pruning` 上，而不是泛泛说“布局更好”。

当前实证边界要写清楚：

- 已有强证据：W6 覆盖 label/edge-type/budgeted L0 pruning；W7 覆盖 query-signature-driven feedback compaction；W8 覆盖 property presence/equality/absent-default 和 typed 2-hop；W9 覆盖 mixed read/write；W13 覆盖 schema epoch 和旧段可读。
- 不能当已证结果：`Like.time` 这类 numeric property-range metadata pruning。当前 W8 支持 property presence/equality/absence，不支持把 range pruning 写成 measured win。
- 推荐 paper 口径：property range pruning 可以作为 SemL0 metadata framework 的自然扩展或 future work；除非后续专门补 range-metadata runner，否则不要写进主实验 claim。

## 实验事实源

| Block | Status | Evidence type | Raw root | Regenerated report |
|---|---|---|---|---|
| W6 SF100 matrix | measured | SF100 matrix, Gate 1/2, correctness, RSS | `remote-logs/w6-sf100-matrix-20260613-132325` | `baseline/sf100-matrix-20260613-cn.md` |
| W7 workload shift | measured with caveat | real-SF30-derived workload shift, three variants, feedback compaction telemetry | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536` | `baseline/w7-sf30-workload-shift-summary-20260615-cn.md` |
| W8 property + 2-hop | measured | SF30 property predicates and typed 2-hop | `remote-logs/w8-property-2hop-20260614-2025` | `baseline/w8-property-2hop-summary-20260615-cn.md` |
| W9 mixed read/write | measured | SF30 steady-state checkpoints | `remote-logs/w9-steady-state-formal-20260615-1200` | `baseline/w9-steady-state-summary-20260615-cn.md` |
| W13 schema evolution | measured tests | schema epoch, alias/drop, property encoding, mixed metadata | `remote-logs/w13-schema-evolution-20260614-0004` | `remote-logs/w13-schema-evolution-20260614-0004/summary.md` |

Every main numeric row below is measured or explicitly controlled. No simulated row is promoted to a main result.

## Evaluation Tables

### E1. SF100 L0 pruning and latency

Source: `baseline/sf100-matrix-20260613-cn.md`, regenerated from W6 raw JSON/TSV.

| variant | status | store GiB | L0 files | ops/repeat | candidate L0 mean | read bytes mean | avg us mean+/-std | compare |
|---|---|---:|---:|---:|---:|---:|---:|---|
| schema | measured | 133.9 | 3,444 | 45,000 | 5,929,197 | 859,764,709 | 9,816.8 +/- 1,585.5 | PASS 45,000 |
| naive | measured | 134.6 | 1,703 | 45,000 | 49,257,601 | 3,629,768,504 | 53,513.8 +/- 189.7 | anchor |
| kv-lsm | measured | 134.6 | 1,703 | 45,000 | 49,257,601 | 3,629,768,504 | 53,892.0 +/- 139.3 | PASS 45,000 |
| edge-type-only | measured | 133.9 | 3,446 | 45,000 | 5,899,015 | 859,688,909 | 8,676.5 +/- 16.3 | PASS 45,000 |
| semantic | measured | 146.4 | 6,615 | 45,000 | 7,782,877 | 673,877,573 | 8,250.9 +/- 37.1 | PASS 45,000 |
| budg-b64 | measured | 133.9 | 3,483 | 45,000 | 6,022,507 | 859,490,557 | 8,722.1 +/- 37.1 | PASS 45,000 |
| budg-b256 | measured | 133.9 | 3,675 | 45,000 | 6,480,991 | 858,117,440 | 8,588.0 +/- 13.8 | PASS 45,000 |
| budg-b1024 | measured | 134.0 | 4,451 | 45,000 | 7,929,225 | 856,071,877 | 8,564.5 +/- 27.1 | PASS 45,000 |
| oracle | measured | 134.6 | 1,703 | 45,000 | 532,193 | 1,318,896,776 | 9,723.8 +/- 1,641.3 | PASS 45,000 |

Safe text: SF100 shows large candidate-L0 reduction for schema/edge-type/budgeted layouts relative to naive, with zero mismatches in sampled compare. Do not write that `budg-b64` has stable latency superiority over schema; Gate 1 is FALLBACK because the mean is lower but 1-stddev intervals overlap.

### E2. SF100 RSS and budgeted cost

Source: `baseline/sf100-matrix-20260613-cn.md`.

| variant | status | max RSS GiB | interpretation |
|---|---:|---:|---|
| naive | measured | 2.43 | baseline |
| schema | measured | 2.42 | 0.99x naive |
| kv-lsm | measured | 2.49 | measured kv-style row |
| edge-type-only | measured | 2.53 | same order as naive |
| semantic | measured | 118.03 | full semantic cost cliff |
| budg-b64 | measured | 2.21 | 0.91x naive |
| budg-b256 | measured | 2.49 | same order as naive |
| budg-b1024 | measured | 2.68 | same order as naive |
| oracle | measured | 2.67 | oracle/reference |

Safe text: Gate 2 is GO. RSS may appear in the main paper, phrased as measured RSS rather than a theoretical memory guarantee.

### E3. Property predicates

Source: `baseline/w8-property-2hop-summary-20260615-cn.md`.

| variant | status | predicate family | candidate L0 delta vs schema | body reads delta | read bytes delta | elapsed delta |
|---|---|---|---:|---:|---:|---:|
| budg-b64 | measured | presence/equality/absent-default | -22.0% | -22.2% | -14.5% | -23.2% |
| semantic | measured | presence/equality/absent-default | -21.8% | -22.2% | -14.4% | -23.9% |

Safe text: property predicates are covered. The required-property mode is an exact-prune sanity case with zero candidates/body reads for all three variants.

### E4. Typed 2-hop

Source: `baseline/w8-property-2hop-summary-20260615-cn.md`.

| variant | status | candidate L0 delta vs schema | body reads delta | read bytes delta | elapsed delta |
|---|---|---:|---:|---:|---:|
| budg-b64 | measured with caveat | +28.5% | -20.8% | -7.3% | -24.8% |
| semantic | measured with caveat | +29.4% | -20.8% | -7.4% | -24.8% |

Safe text: typed 2-hop improves body reads/read bytes/elapsed, but candidate L0 increases relative to schema. Do not claim universal candidate reduction for 2-hop.

### E5. Mixed read/write steady-state

Source: `baseline/w9-steady-state-summary-20260615-cn.md`.

| variant | status | total queries | mean candidate L0 | mean p50 us | mean p99 us | last L0 files | writer errors | slow ops |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| schema | measured | 293,502 | 169.26 | 409.20 | 4,858.40 | 1,365 | 0 | 0 |
| budg-b64 | measured | 293,411 | 162.25 | 416.74 | 4,801.19 | 1,432 | 0 | 0 |
| semantic | measured | 295,251 | 315.30 | 147.67 | 848.90 | 2,640 | 0 | 0 |

Safe text: the mixed workload completed for all three variants with no writer errors or slow ops. `budg-b64` has mean candidate L0 -4.1% and p99 -1.2% vs schema, but p50 +1.8%. `semantic` has lower p50/p99 but higher candidate L0 and more L0 files. Claim only these delta-supported effects.

### E6. Schema evolution correctness

Source: `remote-logs/w13-schema-evolution-20260614-0004/summary.md`.

| status | tests | claim boundary |
|---|---:|---|
| measured tests | 10/10 pass | schema epoch advancement, old segment readability, alias/drop behavior, fixed-width property encoding, conservative exact pruning under new edge labels and mixed metadata |

Safe text: W13 supports additive/fixed-width/schema-epoch correctness. It does not support claims about full physical migration, reclamation, or unrestricted type changes.

## Self-Tuning / W7 Placement

Current W7 evidence should be written as real-SF30-derived formal evidence with a production-trace caveat:

- `remote-logs/w7-sf30-workload-shift-formal-20260615-1536`: formal run, `DONE` exists, `FAILED` absent.
- `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`: Gate=GO.
- formal parameters: phase_flushes=8, hot_sources=64, edges_per_source_per_flush=1, queries_per_source=8.
- feedback-only selected a hot semantic partition in both phase A and phase B; the selected hot range changed after workload shift.
- no-feedback had no hot compaction and retained last candidate L0/query = 8.0 in both phases.

Safe text: SemL0 has a query-signature-driven feedback path that can move semantic compaction priority in a real-SF30-derived workload shift. Feedback-only reached hot compaction in phase A and phase B at flush 1; candidate L0/query dropped from 2.0 before compaction to 0.0 after compaction in the last flush of both phases. Rewrite cost was 59.25 KiB input, 47.00 KiB output, and 47.00 KiB IO write bytes per phase, with write amplification 0.793. This supports the self-tuning claim, but it is not a full-store production trace.

## Final Caveat Table For Paper

| caveat | paper placement | required wording |
|---|---|---|
| Gate 1 latency fallback | main evaluation | `budg-b64` mean latency is lower than schema, but 1-stddev intervals overlap; no stable latency-superiority claim. |
| Read bytes caveat | evaluation footnote | read bytes are measured, but remain secondary where reader-overread can affect interpretation. |
| W7 derived-workload boundary | self-tuning subsection / limitation | W7 is real-SF30-derived formal evidence, not a full-store production trace. |
| W8 2-hop candidates | W8 text | 2-hop improves body/read/elapsed, but candidate L0 increases. |
| W9 semantic cost | W9 text | semantic improves p50/p99 but increases candidate L0 and L0 files. |
| W13 boundary | schema section / limitation | only additive/fixed-width/schema-epoch correctness is claimed. |
| BACH/property range | motivation / related work / limitation | BACH motivates the physical-locality gap; SemL0 adds exact segment-level semantic pruning. Numeric property-range pruning such as `Like.time` ranges is future work unless measured separately. |
| External baseline | limitation / related work | LiveGraph SF100 did not produce a complete numeric main-table result; external systems stay qualitative or bounded unless same-workload gates are met. |

## Artifact Checklist Insert

Minimum artifact package should include:

| item | required path |
|---|---|
| W6 raw and report | `remote-logs/w6-sf100-matrix-20260613-132325`; `baseline/sf100-matrix-20260613-cn.md`; `baseline/summarize_w6_sf100_matrix_20260613.py` |
| W8 raw and report | `remote-logs/w8-property-2hop-20260614-2025`; `baseline/w8-property-2hop-summary-20260615-cn.md`; `baseline/summarize_w8_property_2hop_20260615.py` |
| W9 raw and report | `remote-logs/w9-steady-state-formal-20260615-1200`; `baseline/w9-steady-state-summary-20260615-cn.md`; `baseline/summarize_w9_steady_state_20260615.py` |
| W13 raw and report | `remote-logs/w13-schema-evolution-20260614-0004`; `remote-logs/w13-schema-evolution-20260614-0004/summary.md`; `baseline/run_w13_schema_evolution_20260613.sh` |
| W7 formal evidence | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536`; `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`; `baseline/run_w7_sf30_workload_shift_20260615.sh`; `baseline/summarize_w7_sf30_workload_shift_20260615.py` |
| W10 audit materials | `baseline/w10-evidence-inventory-20260615-cn.md`; `baseline/w10-final-caveat-table-20260615-cn.md`; `baseline/w10-artifact-checklist-20260615-cn.md` |

## Submission Readiness Verdict

Minimum-line verdict: `submission-ready with narrowed claims` after final paper/appendix assembly, provided the main paper uses this draft's title, tables, caveats, W6 Gate 1 fallback wording, and W7 derived-workload caveat.

Recommended-line evidence verdict: `evidence-ready with caveats`.

W6/W7/W8/W9/W13 evidence blocks are now complete. W7 is real-SF30-derived, not a full-store production trace; W6 latency remains Gate 1 FALLBACK. The remaining work is W10 final SIGMOD-format paper/appendix assembly, not more experiments.

Do not start W11/W12 or new external-baseline experiments. The next convergent action is final SIGMOD PDF/appendix assembly from this W10-safe draft.
