# SemL0 下一步强化计划

## Milestone-0: Current Package

当前里程碑目标是冻结故事、归档证据并收紧 claim。产物包括：

1. `seml0-paper-milestone-cn.md`：当前阶段论文草稿。
2. `seml0-current-evidence-tables.md`：当前证据表和 caveat 汇总。
3. `seml0-next-strengthening-plan-cn.md`：下一阶段强化计划。
4. `README.md`：输入、命令、缺失项和安全说明。

当前版本只做文档整理和只读证据提取，不运行重实验，不打开 SF100 store，不覆盖已有结果。

## P0: 数据可信度和论文口径修复

1. 移除或重新标注 kv-style simulated results；任何 Python/model-derived 结果都不能称为 measured。
2. 从现有 JSON/TSV 中提取 p50/p90/p99 和 pruning funnel，并在主文中说明 histogram 粒度限制。
3. 为 budg-b64/b256 添加 against naive 或 brute-force/oracle anchor 的 correctness compare；schema-relative compare 不能作为唯一正确性锚点。
4. 重写 claim：primary metrics 是 candidate L0、read amplification、body reads、L0 files；latency 在 repeats/warm-up/cache-control 完成前只写 preliminary。
5. 保证所有论文表格都能从 raw JSON/TSV 重新生成，并保留 measured/simulated/TODO/source 字段。

## P1: SF100 repeatability and reader caveat

1. 为 storage-bench 增加或启用 warm-up、>=3 repeats、cache-state recording、round-level output 和 p50/p90/p99。
2. 等当前用户实验结束后，重跑关键 SF100 变体：schema、edge-type-only、budg-b64、budg-b256、semantic、budg-b1024、naive@s5000。
3. 实现或验证 precise offset read，修复 full semantic/budg-b1024 的 read_bytes caveat；修复后只重跑受影响变体。
4. Profile CSR `get_neighbors` 固定开销，解释 read_bytes 大幅下降但 latency 收益较小的现象。

## P2: Baseline and fairness

1. 运行真实 RocksDB/KV-style layout baseline，不能使用 Python-simulated result 代替 measured baseline。
2. LiveGraph 先跑 SF10 smoke 和同一 workload/correctness gate，再决定是否推进 SF100。
3. Server end-to-end 只作为 optional smoke；主文不要声明 end-to-end schema vs budg-b64，除非 server store switching 数据实际存在。
4. 外部系统进入主表前必须满足同一边集、同一 sampled typed-neighbor workload、correctness check、load/RSS/footprint/latency 记录。

## P3: System hardening

1. 修复并复测 degree_directory RSS：只有 exact degree files 应建立 degree directory；Mixed degree 文件不应支付全量内存成本。
2. 若 metadata LRU/cache issue 仍存在，修复后用单元测试和 SF30/SF100 open/bench 复核。
3. 若 oracle index 仍有 correctness/perf 问题，修复后加入 oracle-vs-bruteforce 单测，并考虑 oracle pruning upper-bound 表。
4. 增加 safe pruning invariants 测试：mixed/unknown/legacy/tombstone/schema-uncertain 必须保守保留。

## P4: Dynamic and property evidence

1. 增加 mixed read/write steady-state workload：前台 sampled typed-neighbor query，后台持续 ingest/delete/tombstone。
2. 增加 property-aware workload：`required_property`、property presence、property equality、absent/default。
3. 增加 sustained feedback run：记录 candidate L0、latency、rewrite bytes、L0 files over time，并覆盖 workload shift。
4. 增加 tombstone/schema-epoch adversarial microbench，证明安全回退不会漏读。

## P5: Writing freeze

1. 最终标题必须匹配实际证据。如果 property/dynamic 证据不足，使用 `Query-Semantic L0 Design`，不要扩大到完整 `Physical Design`。
2. 如果 property workload 仍弱，贡献表述应明确 edge type 是当前最强证据，property/schema/tombstone 是机制和正确性范围，不是完整性能覆盖。
3. 保持 limitations 显式：external baseline、mixed read/write、RSS、kv-style、tail latency、property workload 都不能隐藏。
4. Related work 要清楚区分 BACH/LSMGraph、LiveGraph/Teseo/GraphOne、RocksDB/KV-style 和 adaptive physical design。

## Go / No-Go Gates

| Gate | 问题 | Go 条件 |
|---|---|---|
| Gate 1 | SF100 latency 是否稳定？ | warm-up + >=3 repeats 后 mean/stddev/p50/p90/p99 支持主张 |
| Gate 2 | RSS 是否降到可接受范围？ | degree_directory 修复后 SF30/SF100 RSS 复测，不再出现无法解释的 20x 级差距 |
| Gate 3 | Real kv-style baseline 是否完成？ | 真实 import/bench/correctness/footprint 完成，并明确 measured |
| Gate 4 | Property-aware workload 是否完成？ | 至少 SF30 property presence/equality/absent-default 有可追踪结果；否则收窄 claim |
| Gate 5 | Mixed read/write 是否完成？ | schema/budg-b64/full semantic/full-compact 有 steady-state time-series |
| Gate 6 | Correctness 是否有 naive/oracle anchor？ | budg-b64/b256 至少 sampled compare against naive 或 oracle/bruteforce |

如果 Gate 1、Gate 3、Gate 6 不能通过，论文不能强写 latency superiority、KV fairness 或 exact correctness closure。如果 Gate 4、Gate 5 不能通过，标题和贡献必须收窄到 L0 typed-neighbor kernel。
