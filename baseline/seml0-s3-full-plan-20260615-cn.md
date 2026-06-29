# SemL0/SemLSM S3 完整计划留档

Last update: 2026-06-15 CST

## 定位

S3 是同一篇 SemL0/SemLSM 论文的远期增强路线和 optional-strengthening backlog，不是第二篇文章，也不是当前立即施工项。

当前论文不能继续硬撑成纯 L0 filter paper；但也不应该马上扩成完整多级 SemLSM 大工程。当前执行主线仍然是：

```text
W14 -> W14 summary verdict -> 决定是否只做 S0/S1/S2 的最小增强 -> 写作收敛
```

不推荐当前直接执行：

```text
W14 未完成 -> 直接开完整 S3 多级容器段 / 新磁盘格式 / L2-L3 scheduler
```

## S3 启动 Gate

只有以下 gate 成立，才考虑把 S0/S1/S2 纳入当前投稿版本；完整 S3 仍只作为 backlog。

| Gate | 含义 | 不成立时的处理 |
|---|---|---|
| G1: W14 必要性成立 | edge-type-only 是 SemL0 的一维特例；src/dst/property/degree/schema semantics 能进一步解释或降低 candidate/body/read | 不启动 S3；论文 claim 收窄为 L0 semantic admission/pruning |
| G2: C10 延迟归因清楚 | 能把总延迟拆成 filter/offset/body/decode/merge/property eval，解释为什么 read-amp 不总转化为 latency | 不写强 latency claim |
| G3: S0 semantic dilution 真实存在 | 普通 compaction 会稀释 exact semantic summary，导致 pruning surface loss | 不做 S1/S2；避免为了理论美感做无必要工程 |

## 最小强投稿线

如果 W14 是 GO，且 C10/S0 有正向信号，可以考虑把以下三项作为同一篇论文的增强，而不是开新文章：

| 项 | 目标 | 产出边界 |
|---|---|---|
| S0: semantic dilution 诊断 | 只测普通 compaction 后 exactness retention / pruning surface loss，不改磁盘格式 | 一个诊断表 + caveat |
| S1: semantic summary lattice | 给出 proof-preserving merge theorem，说明 join/unknown/fallback 不会 false negative | 论文方法和正确性小节为主 |
| S2: L0->L1 split-hot / merge-cold prototype | 最小 query-signature-driven compaction prototype，证明 merge policy 不只是 L0 admission | SF30 级 prototype + bounded-cost 表 |

这条线的目标是把论文从“L0 filter evidence”升级为“query semantics guides bounded-cost physical design”，但仍保持实现可控。

## 完整 S3 蓝图

完整 S3 是多级 query-semantic LSM，不在当前主线施工。

```text
L0  : 细粒度 exact semantic segments，当前 SemL0 已覆盖主要机制
L1  : 按 hot signature + src bucket 保留 exact semantic runs
L2  : 大容器段 + 内部 semantic row groups
L3+ : 更粗粒度 semantic zones + block/group filters
```

核心原则：

- level 越高，文件数量越少、段越大。
- 段内部用 semantic row group 保留 pruning surface。
- 旧单语义段可以视为 single-group container，保证向后兼容。
- 所有 pruning 必须是 exact-proof 或 conservative fallback，不能引入 false negative。

## 设计模块

| 模块 | 目标 | 当前锚点 | 风险 |
|---|---|---|---|
| L1+ container segment | 在 L1+ 支持 segment 内 semantic row group | `src/csr/format.rs`, `src/csr/writer.rs`, `src/csr/reader.rs` | 新磁盘格式和兼容性风险高 |
| semantic summary lattice | src/dst/edge/direction/degree/property/schema/tombstone 逐维 join | `src/semantic.rs`, `CsrSegmentMeta` | 定理若写不清会被质疑只是工程 patch |
| hierarchical filter cascade | level index -> segment summary -> group summary -> bloom/offset/body | `src/index.rs`, `src/graph.rs`, `CsrReader` | 容易增加 metadata 和 cache miss |
| query-signature-driven compaction | split-hot / merge-cold，按 benefit/rewrite cost 选择重写 | `score_l0_partition`, `l0_partition_snapshots` | scheduler 和写放大控制容易失控 |
| schema/tombstone correctness | old epoch readable、stable id exact pruning、unsafe state fallback | W13 schema work | online migration/reclamation 不能过度承诺 |
| numeric range pruning | property min/max zone map 或 range-bloom | `property_encoding.rs` | 当前只列 future work，不纳入 measured claim |

## Merge Policy 草图

S3 的 merge action 集合：

| Action | 描述 | 论文里可写的安全边界 |
|---|---|---|
| coarse merge | 全合并成 mixed 段，低写放大，低 pruning | baseline |
| exact-compatible merge | 只合并 semantic-compatible 输入，输出仍 exact | proof-preserving |
| split-hot / merge-cold | hot query signature 保留 exact group，cold remainder 合并 | S2 最小 prototype |
| tombstone-aware merge | safe snapshot 后 apply/GC，否则 tombstone-sensitive fallback | correctness |
| schema-repair merge | stable canonical id 可证明时重算 exact，否则 schema-uncertain fallback | correctness |

Score 仍使用 bounded-cost 思路：

```text
benefit = query_count * (saved_probes*c_probe + saved_offset*c_off
                         + saved_body*c_body + saved_bytes*c_io)
cost    = rewrite_bytes*c_write + output_files*c_file + metadata_bytes*c_mem
          + future_compaction_bytes*c_future + stall_ms*c_stall
score   = benefit / cost
```

主 claim 不应该是“总能降低 latency”，而应是：

```text
在有界 write/space/file-fanout 成本下，query-signature-driven semantic compaction
能保留更多 exact pruning surface，从而降低 candidate/body/read amplification；
latency 只在 C10 证明 read-amp dominated 的场景里作为 measured result 报告。
```

## 实验矩阵留档

完整 S3 若未来施工，需要这些实验，但当前不启动：

| Exp | 目标 | 规模 |
|---|---|---|
| A: semantic dilution | compaction 后 exactness retention / pruning surface loss | SF30 全矩阵 + SF100 关键点 |
| B: hierarchical filters | L0-only -> +L1 summary -> +L2 row groups -> full cascade | SF30 |
| C: merge policy trade-off | size-tiered / leveled / edge-type-only / SemMerge-static / SemMerge-feedback | SF30 + SF100 key |
| D: long-running writes | throughput, stall, rewrite bytes/sec, space amp, read p99 | 继承 W9 |
| E: schema evolution + merge | schema change before/after compaction, mixed epoch fallback | 继承 W13 |

## 风险评估

- 工程量约 3-6 个月，不适合当前直接开跑。
- 新磁盘格式、manifest、reader/writer、scheduler 都会扩大 correctness surface。
- C10 未解释前，latency claim 仍可能弱。
- schema migration 和 tombstone reclaim 很容易失控，当前只能写 conservative boundary。
- numeric range pruning 暂列 future work，不纳入当前 measured claim。

## 当前决策

当前只执行 W14。S3 留档用于指导同一篇论文后续是否增强，不作为当前任务，不开第二篇文章。

W14 后的决策规则：

| W14 结果 | 下一步 |
|---|---|
| GO | 再评估是否做 S0/S1/S2 最小增强；完整 S3 仍不施工 |
| FALLBACK | 收窄 claim，写 limitation/related work，不做 S3 |
| NO-GO | 不写 SemL0 必要性强 claim，回到 W10 claims safety |
