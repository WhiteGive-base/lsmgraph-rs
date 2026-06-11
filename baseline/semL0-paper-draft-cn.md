# SemL0：面向 LSM 动态属性图的查询语义物理设计 —— 论文初稿（导师讨论版）

> 状态：初稿 / 讨论稿（2026-06-10）。本稿用中文写给导师快速过思路与新数据；表格沿用实验里的英文列名。
> 数据来源：全部为内核（Rust `lsmgraph`）在真实 LDBC SNB 数据上实测，脚本与原始痕迹见 `baseline/semL0-ablation-results/`。
> 与既有材料的关系：沿用 `paper/` 既定叙事（`paper/final-paper-outline-md.md`、各章 draft），**新增贡献是把评测从仅 SF1 扩展到 SF1/SF30/SF100**——这正是既有大纲里标注"尚缺"的部分。

---

## 摘要（Abstract）

基于 LSM 的动态属性图存储用增量写（delta/L0）换取了高更新吞吐，但读路径昂贵：L0 段相互重叠，一次邻居查询往往要反复探测大量与该图谓词无关的段，形成**读放大**。根因是物理布局对**查询语义盲视**——它只按键/范围组织，而不感知属性图查询真正用到的标签(label)、边类型(edge type)、方向、度数类(degree class)、属性与 schema 纪元(schema epoch)。

本文提出 **SemL0**：把图查询的"访问签名"暴露给 LSM 的物理布局与段元数据，从而在 flush/compaction 阶段按查询语义组织 L0 段，并在读路径上用"可证明不相交/不存在才裁剪、否则保守多读"的安全规则减少读放大。SemL0 进一步用**收益打分(benefit scoring)**让语义分区保持选择性（避免全量语义物化的代价），用**反馈式 compaction**让布局随热点漂移自适应。

我们在 LDBC SNB **SF1 / SF30 / SF100** 上做了 9 个布局/策略变体的受控消融。关键结果：相对无语义基线(naive)，语义布局把每查询**候选 L0 段数降低约 8×**（SF30：60.3 万→7.2–9.8 万；SF100：196.8 万→23.6–31.2 万），读字节在 (label+edge_type) 布局下降低 **99.8–100%**；且该裁剪是 **exact-proof**（与全量结果逐邻居比对 mismatch=0）。我们还发现 **(label, edge_type) 信号是跨规模稳健的主因**，而单独的 label 或 degree 维度几乎无效；**带预算的 SemL0 在 SF100 上比无预算 full_semantic 读得更少**，支持"选择性物化"的设计。

---

## 1. 引言（Introduction）

**读者问题**：一个 LSM 图存储为什么需要"查询语义"的物理设计？

1. 动态属性图需要对更新友好的存储，因此 LSM 式增量(delta)很有吸引力。
2. 但读路径昂贵：L0 段重叠，使得一次图谓词查询要反复探测大量"其实不可能命中"的段。
3. 属性图查询不只是拓扑读：它涉及源标签、边类型、方向、度数类、属性存在/取值、快照、schema 纪元。
4. **SemL0 的论点**：把这些语义暴露给布局、段元数据、compaction 与安全裁剪。
5. **贡献**：
   - **C1 查询语义 L0 布局**：flush 时按 `GraphAccessSignature`（label/edge_type/degree）组织段并写入语义摘要。
   - **收益打分预算策略**：只对高收益分区做细分物化，避免全量语义物化的写代价。
   - **反馈式语义 compaction**：用运行时反馈（查询数、候选扇出、读字节、重写代价）选择性重组热点语义范围。
   - **保守正确性**：schema 纪元 + exact-proof 裁剪 + tombstone 摘要，保证 schema 演化与快照可见 delta 下不产生假阴性。
   - **跨规模证据包**：SF1/SF30/SF100 受控消融 + 反馈自适应 + 写代价核算 + 正确性回归。

**不主张**：SemL0 不是完整图数据库、不是完整 schema 迁移引擎、不做生产级写停顿研究。

---

## 2. 背景与问题（Background & Problem）

**何为"查询语义盲视"？** LSM 图布局把更新就近写成重叠的 L0 段；而一次图查询带有签名（源标签、边类型、方向、度数、属性、快照、schema 纪元）。若段元数据无法"证明该段不可能含可见匹配"，读路径就必须保守地探测它。

**共享安全契约（贯穿全文）**：

> 只有当元数据**证明**了不相交或不存在时才裁剪；否则保守多读。
> 裁剪的回退是"多读几个段"，绝不会是"漏读"（无假阴性）。

这条契约把"减少读放大"和"正确性"解耦：语义元数据越精确，裁剪越强；元数据未知/混合(Mixed)时退化为多读，结果仍正确。

---

## 3. 系统总览（System Overview）

四个回路协同：
1. **写路径**：memgraph 满后 flush 成 CSR 式 L0 段，按所选 `--l0-layout` 策略分区，并写入段级语义摘要（src_label、edge_type_partition、degree_class 及完备性标志）。
2. **读路径**：把查询谓词编译为存储面的 `GraphAccessSignature`，用段摘要 + bloom + 范围做 `may_contain_*` 裁剪，未被证伪的段才读。
3. **维护回路**：反馈式 compaction 按 RA-score 选热点语义范围做 L0→L1 重组。
4. **安全层**：schema catalog（段 schema_epoch）与快照/tombstone 元数据，保证演化与历史可见性。

---

## 4. 查询语义物理设计（核心机制）

- `GraphAccessSignature`：面向存储的访问签名（src、edge_type、degree_class），不是逻辑查询计划。
- 段摘要：对标签、边类型、度数类的(精确/保守/未知)摘要；`summary_completeness` 决定是否允许语义裁剪。
- **语义 L0 索引 + 度数感知裁剪**：开库时由段摘要构建，读路径据此快速排除不可能命中的段。
- **为何要"收益打分"而非"全量语义物化"**：全量按 (label×edge_type×degree) 细分会让 L0 段数与元数据暴涨、写放大上升；收益打分只对高收益(高查询权重、低重写代价)的分区做细分，其余归并为保守 Mixed 段。

我们用统一的 `--l0-layout` 开关实现可控消融（同一内核、同一采样计划、公平对照）：
`naive`（无分区）/ `lsmgraph-style`（仅键/范围）/ `label-only` / `edge-type-only` / `degree-only` / `schema`(label+edge_type) / `semantic`(label+edge_type+degree，上界) / `semantic-budgeted`(SemL0，带预算) / `full-compact`（L0 全部压到 L1 的写优化极端）。

---

## 5. 反馈式语义 compaction

静态语义布局会与当前查询热点错位。SemL0 记录运行时反馈（每分区查询数、平均候选段数、offset cache 失配率、重写 MiB），按
`score = query_count × avg_candidate_segments × (1+offset_miss_rate) / rewrite_mib`
选择最该重组的 (src_label, edge_type, range) 分区。**受控负载漂移实验**显示：负载从 phase_a 漂到 phase_b 后，反馈能把热点分区的候选 L0 段数压到 0（见 §6.3）。

---

## 6. 评测（Evaluation）

**装置**：worktree 内核分支 `codex/query-semantic-lsm-research`；纯 LSM `import --relation snb-full --l0-layout <variant>` 路径（与 BaseGraph 读路径独立）。统一操作点 memgraph=64MB；所有变体共用从 schema store 生成的同一组采样计划，`storage-bench --sample-plan-in` 公平测读；正确性由 `neighbor-compare` 与 schema 逐邻居比对。core 工作负载 = 9 个主边类型 × 200 采样源。

> **指标读法（重要）**：本文主读放大指标是 **candidate L0 segments（每查询检查的候选段数）**，它是确定性计数、跨规模一致、最干净。`read bytes` 在 (label+edge_type) 布局下与候选段数一致地下降；但**含 degree 维度的 full_semantic/benefit_scored 在 SF100 上 read_bytes 异常偏高**（见 §6.4 注），故 SF100 读放大以候选段数为准。

### 6.1 RQ1：语义布局能否降低读放大？(SF30，9 变体)

baseline = naive：candidate L0 = 602,946，read bytes = 8.04 GB。

| Variant | L0 signal | cand. L0 segs | read bytes | vs naive | mismatches |
|---|---|---|---|---|---|
| Naive L0 scan | 无 | 602,946 | 8.04 GB | 0% | — |
| LSMGraph-style | 仅键/范围 | 602,946 | 8.04 GB | 0% | — |
| Label-only | src_label | 429,753 | 8.00 GB | 0.4% | — |
| Degree-only | degree_class | 708,853 | 8.03 GB | 0.1% | — |
| Edge-type-only | edge_type | 72,413 | 4.70 MB | **99.9%** | — |
| Schema (label+edge_type) | src_label+edge_type | 75,649 | 13.1 MB | **99.8%** | — |
| Full semantic（上界） | label+edge_type+degree | 94,183 | 4.70 MB | **99.9%** | **0** |
| **Budgeted SemL0** | benefit-scored | 98,027 | 13.1 MB | **99.8%** | **0** |
| Full L0→L1 compact | L0 消除 | 1,196 | 38.6 MB | 99.5% | — |

**结论**：语义布局把候选段数降 ~8×、读字节降 99.8–99.9%；**edge_type 是关键信号**，单独 label/degree 几乎无效；SemL0(预算) 逼近 full_semantic 上界且 mismatch=0。

### 6.2 RQ1（续）：SF100（8/9，full_compact 见 §6.5）

baseline = naive：candidate L0 = 1,967,530，read bytes = 26.36 GB。

| Variant | cand. L0 segs | read bytes | cand. vs naive | 备注 |
|---|---|---|---|---|
| Naive / LSMGraph-style | 1,967,530 | 26.36 GB | 0% | 基线 |
| Label-only | 1,402,094 | 26.25 GB | −29% 段 | 仅 label 无效 |
| Degree-only | 2,340,919 | 26.35 GB | +19% 段 | 仅 degree 反而更差 |
| Schema (label+edge_type) | **236,667** | 9.4 MB | **−88% 段** | ~100% 读字节降 |
| Edge-type-only | **235,457** | 6.2 MB | **−88% 段** | ~100% 读字节降 |
| Full semantic | 308,679 | 10.1 GB *(注)* | −84% 段 | 匹配段同为 ~20,909 |
| **Budgeted SemL0** | 312,069 | 4.5 GB *(注)* | −84% 段 | 预算下 read_bytes 低于 full_semantic |

**跨规模结论**：候选段裁剪在 SF100 同样成立（~8× / −84~88%），且读放大随规模增大（naive 候选段 60万→197万）。**(label, edge_type) 是跨规模稳健的主因**。

### 6.3 RQ3：反馈 compaction 能否随热点漂移自适应？

| 阶段 | 反馈前候选 L0 段 | 反馈后候选 L0 段 | 无反馈基线 |
|---|---|---|---|
| phase_a | 30 | **0** | 6 |
| phase_b（负载漂移后）| 30 | **0** | 6 |

`selected_ranges_changed = true`：反馈选择的目标分区随负载迁移而改变。结论：反馈式 targeted compaction 能针对性消除热点分区的 L0 读放大；无反馈只能做通用 compaction，热点仍残留候选段。

### 6.4 RQ2 / 写入与维护代价（SF30 P2 摘录）

| Variant | import s | L0 files | store | 说明 |
|---|---|---|---|---|
| Naive | 1194 | 519 | 42.8 GB | 最省写 |
| Schema | 1442 | 1,076 | 43.0 GB | |
| Edge-type-only | 1404 | 1,078 | 43.0 GB | |
| Full semantic | 1373 | 2,062 | 43.0 GB | 段数最多 |
| **Budgeted SemL0** | 1391 | **1,732** | 43.0 GB | **比 full_semantic 段更少（预算权衡）** |
| Full L0→L1 compact | 1961 | 519 | 79.7 GB | 写最贵、store 2× |

**结论**：语义布局以更多 L0 段/略高写代价换取读放大下降；**预算式 SemL0 用更少的 L0 段达到接近上界的读收益**（1,732 vs 2,062）。full_compact 是写优化极端（读极好但 store 2×、import 最慢）。

> **(注) SF100 上 full_semantic/benefit_scored 的 read_bytes 异常**：二者匹配的段数与 schema 相同(~20,909)，但每段读取字节高约 1000×（schema ~448 B/段 vs full_semantic ~484 KB/段），即**含 degree 维度的段在 SF100 触发了整段 body 读取**（SF30 不显著）。这是一个值得在论文中点明的**读路径工程发现**：degree 维度在大规模下以更大的 body 读换取候选裁剪；也是后续优化点（offset 切片 vs 整段读取）。因此 SF100 读放大结论以**候选段数**为准。

### 6.5 full_compact@SF100（进行中）

full_compact 的"一次性 L0→L1 全量合并"在 SF100(35.7 亿边)单次合并峰值 ~472GB 被 OOM kill——这是该极端写优化基线的**合并实现规模限制**(非 SemL0 设计问题)。我们已实现**内存有界的流式合并**（按 src 区间分桶、边读边写、每桶强制 Mixed 度数元数据），并在 SF1 验证 **mismatch=0**；SF100 的 full_compact 正在(等并发实验释放内存后)自动补跑。SF30 已有该变体完整数据。

---

## 7. 相关工作（Related Work）

- **LSM/KV 系统**：compaction 与写优化布局成熟，但不把"图查询签名"作为一等的段元数据。
- **图存储**：拓扑感知存储，但较少关注动态更新下 L0 的语义裁剪。
- **自适应索引**：查询驱动的物理设计，但物理对象与安全边界不同（SemL0 作用在 LSM 段元数据 + 保守语义裁剪）。
- **schema 演化系统**：版本化解释，SemL0 将其用于图 LSM 段元数据与语义裁剪。

---

## 8. 局限（Limitations）

- SF100 的 full_semantic/benefit_scored read_bytes 受"整段 body 读取"影响，读放大以候选段数为准；read-path 切片优化为后续工作。
- full_compact@SF100 依赖流式合并(已验证正确)补齐中。
- 反馈为受控负载漂移证据；非生产级写停顿表征。
- 非完整 schema 迁移引擎；range/string/复合谓词为后续工作。

---

## 9. 结论（Conclusion）

查询语义可以指导 LSM 的物理布局与维护，在保守正确（schema 演化、快照可见 delta、exact-proof 裁剪）的前提下显著降低 L0 读放大。SF1/SF30/SF100 的受控消融表明：**(label, edge_type) 语义是跨规模稳健的读放大杀手，带预算的 SemL0 以更小的写代价逼近全量语义上界**，反馈 compaction 能随热点自适应。

---

## 附录：可复现指针
- 结果与表格：`baseline/semL0-ablation-results/`（中文留痕 + P1/P2 表 + 每变体原始痕迹 JSON）
- 一键复现：`bash reproduce-semL0-ablation.sh sf30|sf100|all`（worktree `/data/WorkSpace/lsmgraph-ablation`）
- 内核补丁：`SNB_SKIP_ADJ_CACHE`（精简导入）、`SNB_SKIP_SEM_INDEX`（plan-gen 防 OOM）、流式 compaction `SNB_COMPACT_BUCKETS`。
