# SemL0 当前证据表与 Caveat 汇总

本文档整理当前 milestone 使用的 evidence。所有数字来自现有 markdown、TSV、JSON 摘要或远端日志表；未找到的数字写为 TODO，不补造。

## 1. Source Files

| 类别 | 路径 | 状态 |
|---|---|---|
| Linux repo | `/data/WorkSpace/lsmgraph-rs` | found |
| branch | `codex/sf100-basegraph-bench` | found |
| HEAD | `ab24ce4e2a40e7d8d342a1498ae6ed1698bc9e36` | found |
| main draft | `baseline/seml0-linux-main-paper-draft-cn-20260611.md` | found |
| rerun plan | `baseline/seml0-linux-rerun-plan-20260611-cn.md` | found |
| SF100 summary | `baseline/sf100-results-s5000.tsv` | found |
| SF100 trace summary | `baseline/sf100-strong-baseline-traces/summary-sf100.tsv` | found |
| SF100 remote log summary | `remote-logs/qslsm-sf100-strong-baseline-20260610/summary-sf100.tsv` | found |
| gap analysis | `baseline/seml0-gap-analysis-20260612/seml0-top-conf-gap-analysis-cn.md` | found |
| gap analysis tables | `baseline/seml0-gap-analysis-20260612/*.tsv` | found |
| sustained feedback | `remote-logs/p6-sustained-feedback-20260612/sustained.tsv` | found |
| SF100 maintenance | `remote-logs/qslsm-sf100-maintenance-table-20260612/maintenance-table.tsv` | found |

## 2. SF30 Layout Ablation

Source: `baseline/seml0-linux-main-paper-draft-cn-20260611.md`.

| Variant | L0 signal | candidate L0 | read bytes | vs naive | correctness |
|---|---|---:|---:|---|---|
| naive | none | 602,946 | 8.04 GB | 0% | N/A |
| lsmgraph-style | key/range only | 602,946 | 8.04 GB | 0% | N/A |
| label-only | source label | 429,753 | 8.00 GB | 0.4% read reduction | N/A |
| degree-only | degree class | 708,853 | 8.03 GB | 0.1% read reduction | N/A |
| edge-type-only | edge type | 72,413 | 4.70 MB | 99.9% read reduction | 0 mismatch |
| schema | label + edge type | 75,649 | 13.1 MB | 99.8% read reduction | 0 mismatch baseline |
| full semantic | label + edge type + degree | 94,183 | 4.70 MB | 99.9% read reduction | 0 mismatch |
| budgeted SemL0 | benefit-scored | 98,027 | 13.1 MB | 99.8% read reduction | 0 mismatch |
| full-compact | L0 eliminated | 1,196 | 38.6 MB | 99.5% read reduction | SF30 complete; SF100 resource boundary |

## 3. SF100 Main Table

Sources: `baseline/sf100-results-s5000.tsv`, `remote-logs/qslsm-sf100-strong-baseline-20260610/summary-sf100.tsv`, `baseline/seml0-gap-analysis-20260612/summary-sf100.tsv`.

| Variant | source | read MiB | candidate L0 | body reads | header reads | avg us | L0 files |
|---|---|---:|---:|---:|---:|---:|---:|
| naive@s5000 | measured | 26052.88 | 49,257,601 | 3,122,166 | 1,703 | 5382.8 | 1,703 |
| schema | measured | 149.71 | 5,929,197 | 532,227 | 4 | 4906.2 | 3,444 |
| edge-type-only | measured | 146.70 | 5,899,015 | 532,193 | 0 | 4288.8 | 3,446 |
| full semantic | measured, read_bytes caveat | 9994.25 | 7,782,877 | 532,193 | 1,222 | 4731.1 | 6,615 |
| budg-b64 | measured | 146.70 | 6,022,519 | 532,193 | 0 | 3658.3 | 3,483 |
| budg-b256 | measured | 146.70 | 6,486,337 | 532,193 | 0 | 3681.1 | 3,675 |
| budg-b1024 | measured, read_bytes caveat | 700.07 | 7,676,875 | 532,193 | 258 | 4060.6 | 4,451 |
| kv-style | simulated(model), not measured | 167.79 | 45,000 | 4,807,169 | 45,000 | 4906.2 | TODO |

`kv-style` 是 model-derived/simulated 行，不得写成 measured。

## 4. SF100 Pruning Funnel

Source: `baseline/seml0-gap-analysis-20260612/funnel-sf100.tsv`.

| Variant | source | ops | candidate | bloom filtered | filter passed | body reads | matched | read bytes | body bytes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| naive | measured | 45,000 | 49,257,601 | 46,070,376 | 3,188,799 | 3,122,166 | 3,122,166 | 27,318,428,688 | 1,000,730,528 |
| schema | measured | 45,000 | 5,929,197 | 5,380,648 | 548,552 | 532,227 | 532,227 | 156,981,696 | 153,835,168 |
| edge-type-only | measured | 45,000 | 5,899,015 | 5,350,611 | 548,404 | 532,193 | 532,193 | 153,829,408 | 153,829,408 |
| semantic | measured | 45,000 | 7,782,877 | 7,230,778 | 553,162 | 532,193 | 532,193 | 10,479,735,312 | 153,829,408 |
| budg-b64 | measured | 45,000 | 6,022,519 | 5,474,005 | 548,514 | 532,193 | 532,193 | 153,829,408 | 153,829,408 |
| budg-b256 | measured | 45,000 | 6,486,337 | 5,937,089 | 549,248 | 532,193 | 532,193 | 153,829,408 | 153,829,408 |
| budg-b1024 | measured | 45,000 | 7,676,875 | 7,125,616 | 551,437 | 532,193 | 532,193 | 734,072,656 | 153,829,408 |
| kv-style | simulated(model) | 45,000 | 45,000 | TODO | 45,000 | 4,807,169 | 45,000 | 175,938,084 | TODO |

## 5. SF100 Latency Percentiles

Source: `baseline/seml0-gap-analysis-20260612/latency-percentiles-sf100.tsv`. 当前 histogram bucket 较粗，p99 只能作为形状观察。

| Variant | source | avg us | p50 interp us | p90 interp us | p99 interp us |
|---|---|---:|---:|---:|---:|
| naive | measured | 5382.78 | 1529.85 | 13577.25 | 42698.64 |
| schema | measured | 4906.22 | 447.08 | 13716.60 | 77457.63 |
| edge-type-only | measured | 4288.78 | 413.79 | 13937.14 | 48960.21 |
| semantic | measured | 4731.11 | 365.43 | 14313.53 | 77202.07 |
| budg-b64 | measured | 3658.33 | 245.22 | 11990.89 | 47506.49 |
| budg-b256 | measured | 3681.11 | 268.25 | 11968.03 | 47312.98 |
| budg-b1024 | measured | 4060.56 | 269.45 | 12416.70 | 50641.03 |
| kv-style | simulated(model) | TODO | TODO | TODO | TODO |

## 6. Budget Sweep

Source: `baseline/seml0-linux-main-paper-draft-cn-20260611.md` and SF100 summary TSV.

| Budget | read MiB | L0 files | avg us | 当前解读 |
|---|---:|---:|---:|---|
| 0, schema-style | 149.71 | 3,444 | 4906.2 | 稳定强内部基线 |
| 64 | 146.70 | 3,483 | 3658.3 | 小预算安全区，latency 待重复确认 |
| 256 | 146.70 | 3,675 | 3681.1 | 小预算安全区 |
| 1024 | 700.07 | 4,451 | 4060.6 | 开始滑向过分段，read_bytes 有 caveat |
| infinity, full semantic | 9994.25 | 6,615 | 4731.1 | 过度分段/metadata-cache 抖动，read_bytes 有 caveat |

## 7. Feedback Evidence

Source: `baseline/seml0-linux-main-paper-draft-cn-20260611.md`.

| Phase | before feedback | after feedback | no-feedback |
|---|---:|---:|---:|
| phase A | 30 | 0 | 6 |
| phase B | 30 | 0 | 6 |

Source: `remote-logs/p6-sustained-feedback-20260612/sustained.tsv`.

| checkpoint | elapsed s | phase | feedback before avg candidate L0 | feedback after avg candidate L0 | no-feedback avg candidate L0 | compaction input bytes | compaction output bytes |
|---:|---:|---|---:|---:|---:|---:|---:|
| 0 | 0 | A | 1.000 | 0.000 | 1.000 | 184 | 184 |
| 1 | 300 | A | 1.000 | 0.000 | 2.000 | 368 | 216 |
| 2 | 600 | B | 1.000 | 0.000 | 1.000 | 184 | 184 |
| 3 | 900 | B | 1.000 | 0.000 | 2.000 | 368 | 216 |
| 4 | 1200 | C | 1.000 | 0.000 | 1.000 | 184 | 184 |
| 5 | 1500 | C | 1.000 | 0.000 | 2.000 | 368 | 216 |
| 6 | 1800 | C | 1.000 | 0.000 | 3.000 | 400 | 248 |

Sustained summary: `selected_ranges_changed=true`，`feedback_after_max_avg_candidate_l0_segments=0.0`，`no_feedback_max_avg_candidate_l0_segments=3.0`，`compactions=7`，`compaction_input_bytes=2056`，`compaction_output_bytes=1448`，`io_write_bytes=1448`。

## 8. Maintenance-Cost Tables

### SF30

Source: `baseline/seml0-linux-main-paper-draft-cn-20260611.md`.

| Variant | import s | throughput e/s | store bytes | L0 files | manifest bytes | max RSS KB |
|---|---:|---:|---:|---:|---:|---:|
| naive | 1194.17 | 910,966 | 42,818,497,255 | 519 | 355,861 | 2,436,484 |
| schema | 1441.73 | 754,544 | 42,994,664,727 | 1,076 | 736,157 | 54,858,952 |
| edge-type-only | 1404.04 | 774,799 | 42,994,700,749 | 1,078 | 738,467 | 54,168,148 |
| full semantic | 1373.47 | 792,044 | 42,995,492,789 | 2,062 | 1,404,555 | 56,118,188 |
| budgeted SemL0 | 1390.73 | 782,214 | 42,995,280,813 | 1,732 | 1,181,944 | 54,158,872 |
| full-compact | 1961.04 | 554,730 | 79,693,512,242 | 519 | 374,056 | 167,250,336 |

### SF100

Source: `remote-logs/qslsm-sf100-maintenance-table-20260612/maintenance-table.tsv`.

| Variant | layout | store bytes | manifest bytes | L0 files | import wall | max RSS KB | exit |
|---|---|---:|---:|---:|---:|---:|---:|
| schema | schema | 141,242,590,040 | 2,364,030 | 3,444 | 1:20:15 | 217,254,732 | 0 |
| edge-type-only | edge-type-only | 141,242,628,041 | 2,368,319 | 3,446 | 1:18:41 | 217,319,028 | 0 |
| semantic | semantic | 141,245,314,584 | 4,518,158 | 6,615 | 1:19:08 | 216,672,136 | 0 |
| budg-b64 | semantic-budgeted | 141,242,986,434 | 2,393,463 | 3,483 | 1:18:05 | 216,854,320 | 0 |
| budg-b256 | semantic-budgeted | 141,243,140,540 | 2,524,145 | 3,675 | 1:17:38 | 216,335,536 | 0 |
| budg-b1024 | semantic-budgeted | 141,243,805,064 | 3,052,991 | 4,451 | 1:21:27 | 215,418,948 | 0 |

## 9. Caveats

1. SF100 latency 当前需要 warm-up、>=3 repeats、cache-state recording 和更细 histogram 后再强写。
2. Full semantic 和 budg-b1024 的 read_bytes 受 reader over-read/offset-array 读粒度影响，当前只作为 caveated secondary metric。
3. `kv-style` 是 simulated/model-derived，不是 measured baseline。真实 RocksDB/KV-style layout 仍待运行。
4. RSS 当前受 degree_directory 问题影响，尤其 SF30 维护表中 schema/edge-type/budgeted 的 RSS 明显偏高；修复后需要重新测量。
5. External baseline 尚未闭环。LiveGraph/Teseo/GraphOne/LLAMA 不能进入主数值表，除非通过同一 workload、correctness、load/RSS/footprint/latency gate。
6. Property-aware workload 仍不足，当前主要证据来自 typed-neighbor/edge-type。
7. Mixed read/write steady-state 缺失。
8. SF100 full-compact 当前是资源边界，不应写成完成主基线。
9. Correctness compare 需要补 naive/oracle anchor，而不只是 schema-relative compare。
