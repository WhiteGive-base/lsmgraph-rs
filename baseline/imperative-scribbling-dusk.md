# SemLSM **S3** 完整计划 — full multi-level query-semantic LSM（future-work / 第二篇蓝图）

> 定位:本文件是 **S3 的完整工程蓝图**,应用户要求单独成文。
> **S3 当前不施工。** 实际下一步是 G1/G2/G3 三个 gate + S0/S1/S2 最小强投稿线(见 §0.2)。
> S3 是"最小强投稿线"跑通、且 gate 全成立后的第二篇 / major journal extension 目标。
> 主指标(继承用户定稿):**read amplification reduction under bounded write/space/file-fanout cost**;
> 读延迟继续 report,但**不作唯一胜负指标**(W6 oracle 行已证剪枝不是延迟瓶颈,见 §0.3)。

---

## §0 前置(把 S3 放回全局)

### 0.1 一句话升级
把论文从「query semantics 能 prune **L0**」升级为
**「query semantics 应当决定 dynamic graph LSM 在 flush→compaction→L1/L2+ layout→hierarchical filtering→schema/tombstone correctness 全链路里的物理输出形态,并在有限 write/space/file-fanout 代价下长期*保留*剪枝能力」**。

### 0.2 S3 之前必须先做的(非 S3,这里只列依赖)
- **G1 = W14**:打穿 edge-type-only —— 证明当查询含真实 property-graph semantics(src/dst label、property presence/equality、degree class、schema epoch)时 edge-type partition 不够。打不出来则 S2/S3 必要性不稳,**不启动**。
- **G2 = C10 延迟归因**:把 `T_total` 拆成 filter/offset/body_read/decode/merge/property_eval 等,定位 ~391µs/probe 固定成本与 body/decode 占比。决定能否讲任何 latency claim。
- **G3 = S0 semantic dilution 量化**:测普通 compaction 后 exactness retention / pruning surface loss(只测量,不实现新 policy)。dilution 不显著则 S2/S3 价值弱。
- **S1**:semantic summary lattice + proof-preserving merge theorem(写作为主)。
- **S2**:L0→L1 的 split-hot / merge-cold、query-signature-driven compaction 原型;主表用 bounded-cost read-amp 框架。
- 以上构成「最小强投稿线」(6–10 周)。**S3 是它的超集,只有它跑通才值得开。**

### 0.3 为什么主指标是 bounded-cost read-amp(S3 全程遵守)
W6 Table 2:oracle candidate=532,193(schema 的 1/11),但 avg latency 9,724µs ≈ schema 9,817µs,read_bytes 反更高。
→ 单纯堆剪枝撞 C10/body/decode 天花板。S3 的胜负条件因此定为:

> 在 **同等或可控** 的 write-amp / space-amp / file-fanout 下,SemMerge 比普通 compaction
> **保留更多 semantic pruning surface**,从而降低 candidate / read / body amplification;
> latency 仅在 read-amp-dominated workload 上改善,且必须用 G2 的归因解释清楚。

---

## §1 S3 架构:四级语义 LSM

```text
L0  : 多个细粒度 exact semantic segments(现状,flush 直出)
L1  : 按 hot signature + src bucket 的 exact semantic runs(中等大小)
L2  : 大容器段 + 内部 semantic row groups(group 级 metadata 可 prune)
L3+ : 更大、更稳定的 coarse semantic zones + block-level filters
```

设计原则:**level 越高文件越少、段越大,但段内用 semantic row group 保住剪枝**(Parquet row-group 思想)。
不能所有 level 沿用 L0 的"小文件单语义",否则 file fanout 爆炸。

### 现状差距(为何 S3 是大工程)
- `src/levels.rs`:目前仅 `L0=0 / L1=1`,无 L2+。
- `compact_l0_target_to_l1`(graph.rs:2743)输出侧 `split_property_compaction_segments` **只按 size 切**,不重算每段 exact summary —— 这正是 dilution 源头。
- 一个段 = 单 `(src_label, edge_type_partition)` 单 CSR(`CsrSegmentMeta`),**没有**"段内多 group"概念。
- `merge_l0_flush_segments_to_cap`(graph.rs:3495)超标时直接 `L0FlushSegment::fully_mixed(...)` 塌缩。

---

## §2 多级容器段 on-disk 格式(S3 最大单项)

### 2.1 现状格式(要扩展的起点)
- `src/csr/format.rs`:段 footer 持久化 `schema_epoch` / `property_encoding_epoch`(~:117/125)+ magic。
- `src/csr/writer.rs`:`write_segment_with_properties(level, file_id, edges)`;SourceBloom 落段尾/manifest(~:261-265)。
- `src/csr/reader.rs`:`read_offsets` / `find_offset_on_disk()`(24B pread 二分,~:565-599) / `read_all_edges_with_properties`。
- `src/csr/manifest.rs`:`ManifestRecord::{CreateFile,DeleteFile}`,SourceBloom 入 manifest。
- `CsrSegmentMeta`:level / file_id / min_src / max_src / src_label / edge_type_partition(`MIXED_EDGE_TYPE`)/ schema_epoch / DegreeClassMask / SourceBloom。

### 2.2 S3 L1+ 容器段格式(新增,版本化、向后兼容)
```text
ContainerSegment (level >= 1)
├── SegmentHeader { level, segment_id, min_src, max_src, min_ts, max_ts,
│                   schema_epoch_range, tombstone_state, global_summary, magic=L1CONT1 }
├── SemanticDirectory { group_id -> GroupMeta }
├── GroupOffsetBlocks { per-group CSR offsets }
├── GroupBodyBlocks   { per-group edge bodies }
├── PropertySidecars  { presence bitsets; fixed-width equality dict/bloom;
│                       (S3-B) numeric min/max zone maps }
└── TombstoneBlocks   { tombstone keys; ts ranges; affected group summaries }

GroupMeta {
  summary: SegmentSummaryLattice,        // §3 各维 lattice 状态的 product
  src_range: (min_src, max_src),
  offset_block_range, body_block_range,
  property_summary_ptr, tombstone_summary_ptr, filters_ptr,
  source_bloom_ptr,
}
```
- `CsrSegmentMeta` → 升级/新增 `ContainerSegmentMeta { global_summary, groups: Vec<GroupMeta>, ... }`;
  旧单语义段视为 **single-group 容器**,读路径统一,保证 W6/W13 旧 store 仍可读(向后兼容硬约束)。
- 改动文件:`csr/format.rs`(新块编解码 + magic/version)、`csr/writer.rs`(多 group 写)、
  `csr/reader.rs`(group-local 读)、`csr/manifest.rs`(容器 meta 记录)、`csr/mod.rs`(meta 类型)。

---

## §3 semantic summary lattice(S1 的 S3 全量化)

每个语义维度的 summary 状态:
```text
Exact(v)  <  ExactSet(S)  <  MixedKnown(S | ⊤)  <  Unknown
              并存正交标记:TombstoneSensitive / SchemaUncertain
```
段/group summary = 维度 product:
```text
Summary = src_label × dst_label × edge_type × direction
        × degree_class × property × schema_epoch × tombstone
```
- 复用 `src/semantic.rs`:`DegreeClass`(已是 lattice,`may_contain_global_query`)、`GraphAccessSignature`、`EdgeDirection`、`PropertyPredicate`。
- `join(s1..sn)`:逐维取上确界(over-approximation)。`CanPrune(q, s)`:仅当某维 exact/conservative 证明 disjoint 或 exact absence,**且** tombstone/schema/snapshot 状态安全。

---

## §4 多级 compaction(lattice join + budgeted action + scheduler)

### 4.1 把 S2 推广到任意级联
L0→L1→L2→L3 每级 merge,**output group summary = join(input summaries)**;同质块重算 exact,异质标 mixed/unknown,tombstone-sensitive / schema-uncertain 保守。

### 4.2 merge action 集合(每级按 score 选)
```text
A coarse merge            : 全并大 mixed 段(写放大低、文件少、剪枝差)
B exact-compatible merge  : 仅并 semantic-compatible,output 仍 exact
C split-hot / merge-cold  : 热 signature 出 exact 段/group + 冷的并 mixed remainder ← 主力
D tombstone-aware merge   : reader snapshot 全过 safe point 则 apply+GC,否则保留并标 tombstone-sensitive
E schema-repair merge     : schema 映射可证稳定则按 canonical id 重算 exact,否则保旧 epoch/标 schema-uncertain
```

### 4.3 budgeted score(扩展现有打分)
现有 `score_l0_partition`(graph.rs:2702):
```text
score = query_count × avg_candidate_segments × (1+offset_cache_miss_rate) / rewrite_mib
```
S3 扩成 benefit/cost,**但保留可解释性、必须配 sensitivity 分析**(别盲目堆项):
```text
benefit = query_count × (saved_probes·c_probe + saved_offset·c_off
                         + saved_body·c_body + saved_bytes·c_io)
cost    = rewrite_bytes·c_write + output_files·c_file + metadata_bytes·c_mem
          + expected_future_compaction_bytes·c_future + stall_ms·c_stall
score   = benefit / cost   ; 在 rewrite budget B 下取 top
```
复用 `metrics.rs` 的 `l0_partition_snapshots()` / `L0PartitionSnapshot`,推广到 per-level / per-group。

### 4.4 production compaction scheduler(长跑)
- backlog 监控、write-stall 控制、rewrite bytes/sec 限速、space-amp 上限、A/B/A sentinel。
- 新模块(如 `src/compaction/scheduler.rs`)+ `graph.rs` compaction 家族集成 + `metrics.rs` 计数。
- **纪律**:内置 ETA/abort(LiveGraph SF100 17–21 天尾部教训);不破已发布数据;表可脚本再生。

---

## §5 读路径:hierarchical filter cascade

```text
query -> GraphAccessSignature (semantic.rs; S3-B 可加 numeric range predicate)
for level in [L0, L1, L2, L3...]:
    cands = LevelSemanticIndex.lookup(sig)            // 一级:level 语义目录
    cands = cands ∪ mixed/unknown/legacy fallbacks    // 必须并 fallback,否则 false negative
    for seg in cands:
        if CanPrune(sig, seg.global_summary): continue // 二级:段 summary filter
        for g in seg.groups:
            if CanPrune(sig, g.summary): continue       // 三级:semantic row-group filter
            if !g.source_bloom.may_contain(sig.src): continue // 四级:source/dst/property/tombstone/schema
            offset = find_offset_on_disk(within g.offset_block_range)  // 复用 reader.rs 二分
            read g body in body_block_range
            row-level snapshot/schema/property check
            merge into result                            // reader.rs merge_visible/merge_latest_records
```
- 一级目录:扩 `src/index.rs` / graph.rs 的 L1+ 索引(见 graph.rs:2977 注释"pruning structure for L1+ lookups")成
  `edge_type/src_label/dst_label/property_key/degree_class/schema_epoch -> 候选段/组` + mixed/unknown fallback list。
- 四级 filter:SourceBloom(已有)、DstLabel/DstBloom(可选,注意内存)、property presence/equality(已有 `retain_edges_for_property_predicate` / `with_required_property`)、tombstone filter、schema epoch filter。

---

## §6 正确性(跨级定理,S1 的扩展)
- **Lemma 1 Conservative Join**:`join` 产出输出段所有可见记录的 over-approximation。
- **Lemma 2 No-False-Negative Pruning**:`CanPrune(q,s)=true` ⇒ s 代表的记录在相关 snapshot/schema 边界下不可能命中 q。
- **Theorem Proof-Preserving Multi-Level Merge**:任何输出 summary 由 `join` 或 exact 重算的 compaction(任意级、任意 action A–E)都保持 no-false-negative;semantic split 提升剪枝精度,cold-mixed 仅减少优化机会、不破正确性。
- 与 snapshot safe point / tombstone / schema epoch 的交互单独证一节;对照 BACH file-snapshot MVCC 写 related work。

---

## §7 子项目 A:online schema migration(超出 W13 additive 边界)
- W13 现状:additive label/property + stable id + alias/drop + fixed-width encoding epoch + 保守 exact pruning(`src/schema.rs` SchemaCatalog 已实现已测)。
- S3-A 目标:rename / drop-with-reclaim / type-change 的**物理 rewrite + epoch 映射**,与 compaction Action E 合并(lazy 优先,safe point 后 eager)。
- 产出:schema-变更分类表(additive 支持 / rename=alias|relabel / drop=lazy→eager / type-change=保守或 rewrite)+ 安全规则 + 实验(见 §9 Exp E)。

## §8 子项目 B:numeric range pruning(当前 paper 显式 future work)
- 目标:`Like.time` 等数值 range 谓词。per-group min/max zone maps(PropertySidecars)+ 可选 SuRF/range-bloom。
- **严守 exact-proof**:仅在 group 的 [min,max] 与 query range disjoint 时 prune;不确定一律保守。
- 改动:`PropertySidecars` 格式 + `src/property_encoding.rs` + `GraphAccessSignature` 加 range predicate。

---

## §9 实验矩阵(完整 A–E + 多级专属)
- **Exp A semantic dilution(多级版,扩 S0/G3)**:`exactness_retention = exact_or_prunable_groups_after / before`;`pruning_surface_loss = read_bytes_after / before`;across L0→L1→L2→L3。
- **Exp B hierarchical filter effectiveness**:L0-only → +L1 segment summary → +L2 semantic groups → +full cascade,记 candidate segments/groups、body reads、read bytes、latency。证 L1+ 设计必要。
- **Exp C merge policy trade-off(主表,bounded-cost 框架)**:
  `{size-tiered, leveled, edge-type-only, SemMerge-static, SemMerge-feedback}` × `{read bytes, body reads, candidate groups, write-amp, space-amp, file count, metadata MiB, p99}`。
  win 条件:SemMerge 取 full-semantic 读收益的 50–80%,而 space/file/metadata 额外开销 <5–10%、write-amp 低于 full-semantic。
- **Exp D long-running writes**:throughput / flush latency / compaction backlog / stall / rewrite bytes-per-sec / space-amp / read latency over time。基于 W9 runner(`src/bin/w5_steady_state_real_store.rs`)扩。
- **Exp E schema evolution + merge**:schema change before/after compaction;old-epoch exact 段 → new-epoch query;mixed-epoch fallback;tombstone+schema+compaction 组合。基于 W13 runner 扩。
- 规模:SF30 全矩阵 + SF100 关键点(继承单-SF100 资源规约:同时仅 1 个 SF100 级任务)。

---

## §10 工程分期与工作量(总 ~3–6 个月,故归第二篇)
| 期 | 内容 | 主改文件 | 估时 |
|---|---|---|---|
| S3a | 多级容器格式 + reader/writer/manifest + 向后兼容 | `csr/format.rs` `csr/writer.rs` `csr/reader.rs` `csr/manifest.rs` `csr/mod.rs` | 4–6 周 |
| S3b | 多级 compaction + lattice join + action A–E + scheduler | `graph.rs` compaction 家族、新 `compaction/scheduler.rs`、`metrics.rs` | 4–6 周 |
| S3c | hierarchical read cascade + LevelSemanticIndex | `reader.rs` `index.rs` `graph.rs` | 3–4 周 |
| S3d | online schema migration | `schema.rs` + compaction 集成 | 3–5 周 |
| S3e | numeric range pruning | `property_encoding.rs` `semantic.rs` PropertySidecars | 2–3 周 |
| Exp | A–E 全矩阵(含 SF100) | `baseline/` runners + summarizers | 4–6 周 |
- 每期:`cargo test -j16 nice -n10` 全绿才继续;内置 ETA/abort;不覆盖旧 trace;表脚本再生。
- 破 `engine-freeze-sigmod2027` —— S3 本就是冻结之后的下一代,需重打基线 tag。

---

## §11 风险
- **C10 未解 → 多级剪枝仍可能 latency 平**:用 §0.3 bounded-cost read-amp 主指标对冲;latency 只在 read-amp-dominated 报。
- **新磁盘格式**:迁移/兼容/correctness re-validation 成本高;single-group 兼容层是硬约束。
- **scheduler 长跑稳定性**:LiveGraph SF100 尾部教训,必须 ETA/abort + backlog 上限。
- **schema migration / range pruning 放松 exact-proof 易引入 false negative**:严守"只在可证 disjoint/absent 时 prune"。
- **score 标定**:跨异构单位,reviewer 必戳 → 必须配 sensitivity 分析,且与简单基线(现有乘除式)对照。

---

## §12 启动 gate(S3 不满足则不开)
1. G1(W14)edge-type-only 必要性成立;
2. G2(C10)延迟归因清楚,主 claim 方向已定;
3. G3(S0)semantic dilution 显著;
4. S1 定理成形 + S2 L0→L1 原型 work、Exp C 雏形出正向 trade-off。
→ 全满足,才把 S3a–S3e 排进第二篇 / journal extension。

---

## §13 S3 模块 → 现有代码 锚点表
| S3 模块 | 现有锚点 | 改动 |
|---|---|---|
| 容器格式/group | `csr/format.rs`(footer/magic)、`CsrSegmentMeta` | 扩为 ContainerSegmentMeta + 新块 |
| 多 group 写 | `csr/writer.rs::write_segment_with_properties`、SourceBloom 落盘 | 新增多 group 路径 |
| group-local 读 | `csr/reader.rs::find_offset_on_disk` / `read_all_edges_with_properties` | 限定 group block range |
| 多级 compaction | `graph.rs::compact_l0_target_to_l1` / `compact_best_l0_partition_by_score` / `merge_l0_flush_segments_to_cap` / `split_property_compaction_segments` | 推广到 L1+、lattice join、action A–E |
| 打分/feedback | `graph.rs::score_l0_partition`、`metrics.rs::l0_partition_snapshots` | per-level/group benefit-cost |
| lattice/signature | `semantic.rs`(DegreeClass/GraphAccessSignature/PropertyPredicate) | 加各维 lattice + range predicate |
| 级目录 | `index.rs`、graph.rs:2977 L1+ 索引注释 | LevelSemanticIndex + fallback |
| schema | `schema.rs` SchemaCatalog(epoch/alias/drop 已有) | rename/drop-reclaim/type-change rewrite |
| 数值 range | `property_encoding.rs`、PropertySidecars | min/max zone maps + range-bloom |
| level 常量 | `levels.rs`(L0/L1) | 加 L2/L3 + split_levels 已支持任意级 |
