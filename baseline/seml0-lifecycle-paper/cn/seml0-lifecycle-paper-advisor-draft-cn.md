# SemL0：面向 LSM 动态属性图的查询语义生命周期管理

> 导师审阅版中文 draft，生成时间：2026-06-23。  
> 用途：帮助导师快速判断论文主线、技术贡献、实验支撑和 SIGMOD 风险边界。  
> 说明：本文不是逐字翻译版，也不是最终投稿稿；英文投稿稿仍以 `en/seml0-lifecycle-paper-draft-md.md` 为准。

## 0. 一句话概括

SemL0 的核心观点是：在 LSM-based dynamic property graph store 中，property-graph query signature 不应只存在于查询层，而应成为贯穿 flush、read、feedback、compaction、schema/snapshot correctness 的 lifecycle control signal。系统在写入时生成 segment-level semantic pruning surface，在读取时只基于 exact proof 做安全剪枝，在 compaction 时保留或重建该 surface，并在 schema evolution 与 snapshot-visible delta 下保证 no false negative。

这篇论文的主 claim 不应写成“统一降低端到端 latency”，而应写成：

> SemL0 通过 query-semantic pruning surface 降低 read amplification，并证明该 surface 可以跨 LSM 生命周期保持，同时给出 schema/snapshot 下的保守正确性边界和可解释的维护成本。

## 1. 研究问题

动态图存储同时面对两个压力：

1. 写入持续到来，LSM layout 适合把更新先写入 L0，再通过 compaction 下推。
2. 查询通常很有选择性，例如 source label、edge type、direction、degree、property predicate 已经能排除大量 segment，但传统 LSM read path 仍然按 overlap / key range 探测很多不可能命中的 segment。

已有 LSM/KV 系统主要用 size、level、frequency、Bloom filter 等信号优化 compaction 或 lookup；已有图存储主要优化 adjacency locality、CSR layout 或事务更新。二者都较少把 property-graph query signature 作为一等 metadata 维护到 LSM segment lifecycle 中。

SemL0 试图回答三个问题：

1. 查询语义能否在不破坏正确性的前提下降低 LSM 图读放大？
2. 这种 semantic pruning surface 在 flush 之后、跨层 compaction 之后还能不能保留，而不是被 naive merge 重新混成 coarse segment？
3. 在 schema evolution、tombstone、degree update 和 snapshot delta 下，semantic pruning 是否仍然不会漏读？

## 2. 核心设计

SemL0 将一次图访问抽象成 `GraphAccessSignature`，其中包含 source label、edge type、direction、degree class、destination label、property predicate、snapshot 和 schema epoch 等信息。Flush 生成 CSR-like immutable segment 时，系统同时产生 `CsrSegmentMeta` / `SegmentSemanticState`，描述该 segment 的 label、edge type、degree、property、tombstone、schema completeness 等信息。

读路径调用 `signature_pruning_decision`，将 query signature 与 segment metadata 对齐。剪枝规则非常保守：

- 只有 metadata 证明 segment 与查询 disjoint，或者证明所需 property 不存在，才可以 prune。
- 如果 metadata 是 `Unknown`、legacy、mixed、tombstone-sensitive、schema epoch 不可精确解释，则必须 keep 并读取。
- 因此 semantic pruning 的失败模式是多读，而不是漏读。

Compaction 是本文的关键 lifecycle 问题。Naive merge 会把多个 exact `(src_label, edge_type)` partitions 合成 mixed segment，使得后续 typed-neighbor query 不能再靠 segment metadata 排除无关数据。SemL0 的 semantic-aware merge 按语义分区输出，跨层保留或重建 pruning surface。Feedback loop 负责把有限 materialization / rewrite budget 用在高收益分区上，避免 full semantic materialization 的 fanout/RSS cliff。

系统可以概括为四个 loop：

1. **Write / flush loop**：写入生成 L0 segment，并推导 semantic metadata。
2. **Read / prune loop**：将查询编译成 signature，用 exact evidence 剪枝。
3. **Feedback / compaction loop**：根据 runtime cost 选择 hot partitions，用 semantic merge 保持 surface。
4. **Schema / snapshot loop**：用 per-segment schema epoch 与 conservative fallback 保证旧数据可解释且不漏读。

## 3. 贡献组织

### C1：Query-semantic pruning surface

SemL0 将 property-graph query signature 暴露给 LSM read path，并维护 segment-level semantic metadata。与普通 Bloom / range filtering 不同，SemL0 的 pruning predicate 包含图语义：source label、edge type、direction、degree、destination label 和 property presence/value。

安全边界是 `Exact / Conservative / Unknown` completeness model。只有 exact 或可证明安全的 conservative evidence 才能剪枝；unknown 只会降低 pruning precision。

### C2：Lifecycle retention of the pruning surface

这是当前论文的承重贡献。SemL0 不只是在 flush 后生成 pruning surface，而是把该 surface 当成必须跨 LSM lifecycle 存活的状态。Naive compaction 会摧毁 surface；semantic-aware compaction 保留或重建 surface，并把读收益与 rewrite/write-amplification cost 放在同一张表中报告。

这让论文从“查询语义物理布局”升级为“查询语义生命周期管理”。

### C3：Snapshot- and schema-correct semantic pruning

SemL0 在 tombstones、degree change、additive schema evolution、snapshot-visible delta 下保持 no-false-negative invariant。Add label / add property 只改变未来写入和 catalog 解释，不使旧 segment 失效；旧 segment 仍按自己的 schema epoch 被解释。任何无法证明安全的情况，都回退为 conservative read。

## 4. 正确性论点

SemL0 的正确性核心是 no-false-negative theorem：

> 如果某个 segment 包含当前 snapshot 和 schema 解释下可见的匹配结果，则 `signature_pruning_decision` 不会剪掉该 segment。

证明依赖三条 invariant：

1. **Segment Schema Invariant**：每个 segment 记录自己的 `schema_epoch` / `property_encoding_epoch`。
2. **Epoch-Aware Resolution Invariant**：读路径按 segment 自己的 epoch 解释 label、edge type 和 property encoding。
3. **Conservative Pruning Invariant**：只有 exact disjointness / exact absence 才能 prune；任何 mixed / unknown / epoch-uncertain state 都必须 keep。

因此 schema 变化、tombstone、snapshot delta 只会降低剪枝率，不会制造漏读。

## 5. 实验支撑

评测覆盖 LDBC SNB 的三个尺度：SF1 用于 smoke/correctness，SF30 作为主要动态 workload，SF100 用于 scale evidence。除特殊说明外，correctness 都与 semantics-blind `naive` anchor 对比，要求 `mismatches = 0`。

### 5.1 W6 SF100：read amplification 与 correctness

W6 是主规模实验，45k ops/repeat，所有 variant 都是 `mismatches = 0`。

| variant | candidate L0 | read bytes (MiB) | avg us | p99 us | RSS GiB |
|---|---:|---:|---:|---:|---:|
| naive | 49,257,601 | 3461.6 | 53,513.8 | 179,259 | 2.43 |
| schema | 5,929,197 | 820.0 | 9,816.8 | 37,333 | 2.42 |
| edge-type-only | 5,899,015 | 819.9 | 8,676.5 | 31,074 | 2.53 |
| budg-b64 | 6,022,507 | 819.7 | 8,722.1 | 30,981 | 2.21 |
| semantic | 7,782,877 | 642.7 | 8,250.9 | 30,415 | 118.03 |
| oracle | 532,193 | 1257.8 | 9,723.8 | 36,704 | 2.67 |

应如何解读：

- SemL0 variants 将 candidate L0 从约 49.3M 降到约 5.9M-7.8M，read bytes 从 3461.6 MiB 降到 642.7-820.0 MiB。
- `semantic` full variant 的 read bytes 最低，但 RSS = 118.03 GiB，是 unbudgeted stress point，不应作为推荐配置。
- `schema` / `budg-b64` / `edge-type-only` 的 RSS 与 naive 基本同量级，说明 budgeted lifecycle control 是主 operating point。
- Latency 在这张表里可作为 supporting evidence，但论文不能把 uniform latency speedup 当 headline。

### 5.2 W9 SF30：动态 mixed read/write 下的 tail latency

W9 是当前最强的 latency 证据：SF30 mixed read/write，约 163 q/s，30 分钟，6 个 checkpoint，0 writer errors。

| t (s) | schema p99 us | budg-b64 p99 us | semantic p99 us |
|---:|---:|---:|---:|
| 300 | 1,270.6 | 1,275.7 | 414.6 |
| 900 | 4,070.5 | 4,019.8 | 792.1 |
| 1800 | 8,740.2 | 8,255.8 | 1,274.0 |

Semantic 在动态 churn 下 p99 相对 schema 平均降低 82.5%，p50 降低 63.9%。代价是 candidate L0 和 L0 files 增加。这个结果可以作为 lifecycle/degradation story 的强证据：semantics-blind layout 随 churn 退化更快，而 semantic surface 更稳定。

但这个实验也要保守表述：它证明某类动态 workload 下 tail latency 明显改善，不证明所有 workload 都加速。

### 5.3 C2：semantic-aware compaction 是否保留 pruning surface

C2 分三层证据，必须在论文中明确区分。

**Layer A：controlled synthetic full-read workload**

| scale | policy | retention | read after vs before | output segs | write_amp | mismatches |
|---|---|---:|---|---:|---:|---:|
| SF1 | naive | 0.0 | 4x blow-up | 1 | 1.22 | 0 |
| SF1 | semantic | 1.0 | flat | 4 | 1.87 | 0 |
| SF10c | naive | 0.0 | 6x blow-up | 1 | 1.14 | 0 |
| SF10c | semantic | 1.0 | flat | 6 | 1.85 | 0 |

这一层证明：在固定 partition structure 下，naive merge 会把 exact keys 塌成 mixed segment，使 read after merge 按 edge-type 数放大；semantic merge 保持 retention = 1.0，读成本保持 flat。

**Layer B：real LDBC SF30 retention + write cost**

真实 SF30 包含 1.09B directed edges，40 个 `(src_label, edge_type)` partitions，528 个 exact L1 segments。

| scale | policy | retention | exact surface after | output segs | output bytes | logical bytes | write_amp |
|---|---|---:|---:|---:|---:|---:|---:|
| SF30 real | naive | 0.0 | 0.0 | 503 from 528 | 37.20 GB | 32.4 GB | 1.07 |
| SF30 real | semantic | 1.0 | 1.0 | 528 from 528 | 43.25 GB | 32.4 GB | 1.24 |

这一层证明：retention 1.0 vs 0.0 在真实 1.09B-edge 数据上成立，semantic write_amp 1.24 vs naive 1.07，增加约 16%，成本可解释。

**Layer C：real SF30 metadata-level read-amplification proxy**

使用真实 SF30 post-merge segment metadata 和 40 个 typed-neighbor partitions 做 replay，不做 full body decode。

| policy | query partitions | avg candidate segs/query | candidate bytes total | weighted read-amp proxy |
|---|---:|---:|---:|---:|
| naive | 40 | 93.6 | 282.16 GB | 6.52x |
| semantic | 40 | 13.2 | 43.25 GB | 1.00x |

这一层把真实 SF30 segment distribution 与 Layer A 的 read-amp 机制连接起来：naive mixed outputs 迫使 typed query 读取很多无关 segment；semantic merge 使 candidate bytes 等于 exact pre-merge partition bytes。

论文应 claim：

> Semantic-aware compaction retains or rebuilds the pruning surface and avoids naive merge's read-amplification mechanism at bounded, explainable write cost.

论文不应 claim：

> 无开销、全局最优、生产级 scheduler、完整 SF30 body-read workload 已完成。

### 5.4 C3：schema/snapshot correctness

W13 包含 10 个 schema-evolution tests，覆盖 old-segment readability、mixed delta across compaction/reopen、alias/drop、encoding epoch、new label exact-vs-mixed pruning 等场景。

结果说明：旧 segment 在 schema 变化后仍可读；新增 label/property 不会使旧 segment 失效；metadata 不确定时保守读取。C3 的作用是把 SemL0 从“实验性优化”提升为“数据库系统可以接受的安全剪枝机制”。

### 5.5 External baseline 状态

外部系统 baseline 目前不能写成已完成数值对比。已有 artifact reports 显示：

- LiveGraph 最适合作 typed-neighbor scan 对比，但仍需要可复现 build、loader、query driver 和 correctness gate。
- Teseo / GraphOne 相关，但当前 artifact 状态还不足以进入主数值表，且 workload 与 SemL0 不完全匹配。
- LLAMA 可 build，但主要面向 BFS/PageRank/SSSP 等 analytics，不是 property-graph typed-neighbor workload。
- LSMGraph / Aster / BACH 与 LSM/CSR 图存储设计最相关，当前更适合 qualitative comparison。

因此 CIDR 版应写成：外部系统提供重要设计对照，但目前没有通过同一 LDBC SNB typed-neighbor workload、correctness check 和可复现运行日志的 apples-to-apples baseline。不要把 LiveGraph 写成已完成外部数值 baseline。

## 6. Claim 与证据对齐

| claim | 当前证据 | 可写强度 |
|---|---|---|
| Query semantics can reduce read amplification in LSM graph reads | W6 SF100，W8 SF30，0 mismatch | 强 |
| Budgeted semantic materialization avoids full-semantic memory cliff | W6 budget sweep，118.03 GiB full-semantic outlier | 中强 |
| Semantic surface can survive compaction | C2 synthetic + real SF30 retention | 强 |
| Retention cost is bounded and explainable | C2 write_amp 1.24 vs 1.07 real SF30；synthetic <2x | 中强 |
| Dynamic workload tail latency can improve significantly | W9 SF30 30min mixed read/write | 中，必须限定 workload |
| Schema/snapshot pruning is DB-safe | W13 tests + invariants/theorem | 中强，需论文写严 |
| External SOTA defeated at scale | 当前外部 baseline 未完成；只有 artifact attempts 和 qualitative comparison | 不应写 |

## 7. 我认为导师需要重点判断的点

第一，论文的 novelty 不宜继续放在 lifecycle management 这个标题中心。更适合 CIDR 的说法是：**query signatures as a storage control plane**。仅说“用查询语义剪枝 segment”可能被认为是工程优化；但说“query signature becomes a storage-level control plane that shapes segment metadata, pruning, physical rewrite, and correctness fallback”，贡献就更像系统架构思想。

第二，C2 是决定论文厚度的关键。现在 C2 已经有 controlled read-amp、真实 SF30 retention/write-cost、真实 SF30 metadata proxy 三层证据，足以支撑“naive compaction destroys semantic surface; semantic merge retains it”的论点。正文必须把这三层讲清楚，避免审稿人误以为真实 SF30 只测了 metadata 没有意义，也避免我们过度声称完整 SF30 body-read workload。

第三，latency 要降级为 supporting evidence。W9 很强，但 W6 static read-only 的 latency story 不应被写成统一结论。主线应是 read amplification / candidate bytes / retention / correctness / bounded cost。

第四，external baseline 是当前短板。LiveGraph/Teseo/GraphOne/LLAMA 只能先做 qualitative comparison 或待跑候选，不能写成已完成数值 baseline。CIDR 可以接受“没有合适 apples-to-apples baseline”，但必须用 artifact attempt、reproducibility gate 和 design matrix 说明原因。

第五，full semantic 的 118.03 GiB RSS 是风险，也可以转化为论点：正因为 full semantic 会有 fanout cliff，SemL0 需要 budgeted lifecycle control。正文不能推荐 full semantic 作为默认配置。

## 8. 当前 CIDR 风险评估

以现在的 evidence package 看，这篇稿子比 SIGMOD 更适合先转 CIDR：它有一个清晰的系统架构观点、远端原型部署、关键反例和强边界，但外部 baseline 和生产部署不足以支撑 SIGMOD/industry-track 的完整实证要求。

优势：

- 系统观点明确：query signatures as a storage control plane。
- 证据链比单纯 microbenchmark 更强：SF100 scale、SF30 dynamic、real SF30 compaction retention、schema/snapshot correctness 都有。
- Claim 可以写得克制且防守性强：不是“我们做了一个更快图数据库”，而是“我们提出了一种让 query semantics 进入 mutable LSM graph storage control plane 的设计原则，并用原型证明它有效、有边界”。

主要风险：

- 相关工作必须写精准，尤其是 LSMGraph、Aster、BACH、LiveGraph、Teseo、GraphOne、LLAMA、adaptive indexing、LSM tuning。否则容易被认为 novelty 不够。
- CIDR 6 页稿还没成型，当前仍是 md evidence package。
- 当前没有把 real SF30 full body-read execution with body decode 作为已完成证据；C2 claim 已用三层证据闭合，并在正文中明确这个边界。
- External baseline 当前未完成，无法支撑“scale SOTA win”。
- 生产部署、production write-stall、background scheduler、arbitrary schema migration 都不是本文完成范围。

我的建议是：按 CIDR 优先重写，主题从 lifecycle management 改为 storage control plane。不要走“benchmark 全面击败现有系统”的路线，而是走“提出系统设计原则 + 原型部署证据 + 外部系统不可比性分析 + 未来研究议程”的路线。

## 9. 投稿前最需要补的工作

1. 先读 `../cidr/` 材料包，按 CIDR 标准重写 6 页中文草稿。
2. 补齐 Related Work 的 bibkeys 和准确对位，尤其 LSMGraph、Aster、BACH、LiveGraph、Teseo、GraphOne、LLAMA、RocksDB/LSM tuning、adaptive indexing、graph storage、schema evolution。
3. 画三张核心图：
   - SemL0 lifecycle pipeline：flush -> read/prune -> feedback/compaction -> schema/snapshot safety。
   - Schema/snapshot timeline：old epoch segment、新 label、新 write、conservative fallback。
   - C2 retention/cost figure：retention 1.0 vs 0.0，write_amp 1.24 vs 1.07，metadata proxy 1.00x vs 6.52x。
4. 在正文中统一 claim wording：latency supporting，read amplification/retention/correctness headline。
5. 清理 external baseline 旧说法，所有外部系统必须通过 reproducibility gate 才能进入主数值表。

## 10. 希望导师重点给意见的问题

1. “Query signatures as a storage control plane” 是否比 lifecycle management 更适合作 CIDR 主题？
2. C2 三层证据是否足以支撑“physical rewrite can destroy or preserve semantic control state”这个主亮点？
3. 如果外部系统都不能 apples-to-apples 对比，CIDR 版是否可以接受 qualitative comparison + artifact attempts？
4. Related Work 应把 SemL0 放在 graph storage、LSM tuning、adaptive indexing 还是 database control-plane design 下主打？
5. 当前 claim 强度是否合适：read amplification / semantic surface retention / correctness 为主，latency 为 supporting？

## 11. 证据路径

- 主英文稿：`baseline/seml0-lifecycle-paper/en/seml0-lifecycle-paper-draft-md.md`
- 中文阅读版：`baseline/seml0-lifecycle-paper/cn/seml0-lifecycle-paper-draft-cn.md`
- W6 SF100：`baseline/sf100-matrix-20260613-cn.md`
- W8 SF30 property / 2-hop：`baseline/w8-property-2hop-summary-20260615-cn.md`
- W9 SF30 dynamic：`baseline/w9-steady-state-summary-20260615-cn.md`
- C2 controlled + real SF30：`baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`，`baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md`
- C2 raw logs：`remote-logs/c2-sf30-real-20260619/`，`remote-logs/c2-sf30-readamp-proxy-20260621/`
- C2 tables：`baseline/seml0-lifecycle-paper/tables/table-c2-retention.md`
- CIDR 材料包：`baseline/seml0-lifecycle-paper/cidr/`
- External baseline artifact attempts：`external-systems-artifact-summary.md`，`external-system-{LiveGraph,Teseo,GraphOne,LLAMA}-artifact-report.md`，`baseline/external-baseline-comparison.md`
- Reviewer Q&A：`baseline/seml0-lifecycle-paper/cn/reviewer-question-bank-cn.md`
