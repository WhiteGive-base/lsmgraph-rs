# SemL0 论文 Outline 中文解释稿

> 对应英文源：`paper/final-paper-outline-md.md`（2026-06-17 经 S0 冻结升级为 Path B / lifecycle 框架）。
> 本稿是英文 outline 的**中文解释 + review 版**，逐节对应英文结构；**论文正文以英文为准**，本稿不入稿。
> 上层计划见 `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md`。

---

## 0. 这份 outline 是什么 / 不是什么

- **是**：把 SemL0 证据包组织成论文草稿前的"md-only 工作大纲"，约束所有章节的措辞与边界。
- **不是**：不生成 TeX/PDF、不跑实验、不删 store、不选 venue、不代表投稿就绪。
- **S0 冻结要点（2026-06-17，2026-06-19 更新）**：标题与主线从 *Physical Design* 升级为 *Lifecycle Management*；主 claim = **read-amp/candidate 下降 + 生命周期保持(retention) + 安全**；**latency 仅作辅助/observation**（Gate 1 = FALLBACK）；`K4` 只是论文代号，内核用中性命名；**C2 已补 controlled + real SF30 retention/write-cost 证据，但 real SF30 read workload 与生产级后台调度仍是边界**。

---

## 1. 论文定位（Positioning）

**标题**
- 主用：**SemL0: Query-Semantic Lifecycle Management for LSM-Based Dynamic Property Graphs**
- 兜底（仅 Path A 安全网）：SemL0: Query-Semantic L0 Design for LSM-Based Dynamic Property Graphs

**核心论点（thesis）**
> 一个 property-graph 查询签名应当充当 LSM 动态图存储的**生命周期控制信号**：flush 物化出每段的"语义裁剪面（semantic pruning surface）"，读路径利用它，feedback 找出高代价分区，compaction 跨层**保持或重建**裁剪面——同时在 tombstone、additive schema 演进、snapshot 可见 delta 下保持保守正确。SemL0 降低 read amplification；latency 收益随 workload 而定，并如实呈现。

**不要把论文定位成**（4 条红线）：完整图数据库 / 完整 schema 迁移引擎 / 生产级 write-stall 研究 / 端到端·生产级性能报告。

---

## 2. Abstract 计划（五句）

| # | 中文大意 | 证据状态 |
|---|---|---|
| 1 | LSM 让动态图写入友好，但读路径基本对查询语义"无感"：L0 段（以及 compaction 后的跨层段）即使被 label/edge type/方向/度/property 证明无关，仍被反复探测。 | 动机，无需证据 |
| 2 | SemL0 把查询签名变成生命周期控制信号：flush 物化每段语义裁剪面；只有当 metadata **证明**某段不可能含可见匹配时才剪枝，否则保守读取。 | ✅ W6/W8/W14（candidate/read 下降，mismatches=0） |
| 3 | 普通 compaction 会把语义分区塌回 mixed 段、悄悄侵蚀裁剪面；SemL0 让 compaction 语义感知：feedback 选高代价分区，语义 merge 跨层保持/重建裁剪面，并把读收益与 rewrite 代价配对核算。 | ✅ **承重句 = C2，已有 controlled + real SF30 证据；real SF30 read workload 仍是边界** |
| 4 | 在 tombstone、度变化、additive schema 演进、snapshot 可见 delta 下，SemL0 只在 exact 证据下剪枝、否则保守读，跨 compaction 与 reopen 保持"不漏读(no-false-negative)"不变量。 | ✅ W13 + lifecycle/reopen 单测 |
| 5 | 在 LDBC SNB 至 SF100，SemL0 降低 candidate 段与 read bytes、0 correctness mismatch；budgeted/schema variants 内存与"语义无感"基线持平，full semantic 是 unbudgeted stress point。latency 收益随 workload 而定，因此我们呈现"裁剪面保持 + 写放大代价"，而非声称普遍加速。 | ✅ read-amp/RSS/正确性（W6/W9）；✅ C2 controlled + real SF30 retention/write cost + metadata proxy；latency 措辞受 Gate 1 约束 |

---

## 3. 三大贡献（精确措辞 + 边界）

**C1 — 查询语义裁剪面（Query-Semantic Pruning Surface）**
- 把查询签名（源 label、edge type、方向、度类、目标 label、property presence、snapshot/schema epoch）作为段级 metadata 暴露给读路径；用 **exact / conservative / unknown** 完备性模型，只在 metadata 证明 disjoint 或 absence 时剪枝；配 prune-or-keep 的 **reason taxonomy** 解释每个决定。
- 边界（不要写）：always improves latency；composite 已稳定优于 edge-type-only。
- 证据：W6/W8/W14。

**C2 — 裁剪面的生命周期保持（Lifecycle Retention，承重贡献）**
- 把裁剪面当成**必须活过整个 LSM 生命周期、而不仅是 L0** 的状态：flush 生成、feedback 找高代价分区、语义感知 compaction 跨层保持/重建（而非塌成 mixed 段）；读收益与 rewrite/写放大代价显式配对。
- 边界（不要写）：fully optimal / 全自动最优 compaction；生产级常驻调度器。可写 prototype + 有界代价。
- 证据：L1+ semantic split/filter 内核已在；**retention vs 退化 + 写放大的量化 = C2 实验，待跑**。

**C3 — snapshot/schema 正确的语义剪枝**
- 语义剪枝是 DB-safe 的：tombstone、度变化、additive schema 演进、snapshot 可见 delta 下，只在 exact 证据下剪枝、否则保守读；落成 3 个 invariant（Segment Schema / Epoch-Aware Resolution / Conservative Pruning）+ 不漏读 theorem。
- 边界（不要写）：任意 schema migration；range/string/compound predicate；在线全库 rewrite。
- 证据：W13 + 单测。

---

## 4. 各章节大纲（§1–§10）

### §1 引言
- 读者问题：为什么 LSM 图存储需要查询语义驱动的物理设计？
- 段落线：① 动态图需要写友好 → LSM delta 有吸引力；② 读贵在 L0 overlap 反复探测无关段；③ 图查询不只是拓扑读，还有 label/edge type/方向/度/property/schema epoch；④ SemL0 论点：把这些语义暴露给布局、metadata、compaction、安全剪枝；⑤ 贡献 = **C1/C2/C3**（见上）。
- 不要说：SemL0 是完整图数据库；已证明生产级自适应 compaction。

### §2 背景与问题
- 读者问题：LSM 图存储的"查询语义无感"到底指什么？
- 共享契约（全篇后续都受此约束）：**只有当 metadata 证明 disjoint 或 absence 时才剪枝，否则保守读取。**

### §3 系统概览
- 读者问题：四个 loop 怎么拼在一起？
- 线：写路径产出带语义 metadata 的 CSR-like L0 段 → 读路径把谓词编译成 `GraphAccessSignature` → metadata 驱动剪枝 + fallback 过读 → feedback 驱动 compaction 维护 loop → schema catalog + snapshot metadata 作安全层。
- 图：`paper/figures/seml0-pipeline.pdf`。必须连起 C1 布局 / feedback compaction / schema epoch / snapshot·tombstone delta。

### §4 查询语义物理设计 → **贡献 C1**
- 读者问题：查询语义能否实质降低 read amplification？
- 线：`GraphAccessSignature` 是面向存储的签名（非逻辑查询计划）→ 段 summary（label/edge type/度类/property）→ exact·conservative·unknown 三态 → 语义 L0 索引与度感知剪枝 → 为什么需要 benefit-scored 物化而非 full-semantic 物化。
- 安全 claim：SF1 读放大下降；benefit scoring 保住有用语义分区；raw threshold 暴露 fanout/read-benefit 悬崖。

### §5 反馈驱动语义 compaction → **贡献 C2（承重）**
- 读者问题：语义裁剪面能否在 LSM compaction 后继续保留，并且写放大代价可控？
- **S0 升级**：本节改标题为 **"Lifecycle Retention of the Pruning Surface"**，从"L0 feedback 自适应"升级为"**跨 flush + 跨层 compaction 保持/重建裁剪面**"：吸收已有 L1+ semantic split/filter，并补上 C2 证据（controlled read-amp/correctness + real SF30 retention/write cost）。**注意边界：真实 SF30 open-mode 只证明 surface retention 与 write cost，read amplification 后果仍由 controlled rows 展示。**
- 不要说：写开销可忽略；生产 write-stall 安全；长跑 SF30/SF100 自适应验证；全局最优。

### §6 schema 演进与 snapshot 正确 delta → **贡献 C3**
- 读者问题：schema 变更会让旧语义存储失效/无用吗？
- 直接答：**不会。** additive 变更只推进 catalog 解释与未来写路由；旧段在其原 `schema_epoch` 下仍可读。
- 安全 claim：add-label/add-property 不让旧存储失效；unknown/mixed 只导致多读、不导致漏读；定向 schema/snapshot/tombstone 回归通过。
- 边界：完整 rename/drop/type-change 物理迁移是 future work；range/string/compound 谓词、SQL null 语义是 future work。

### §7 评测（Evaluation）
- 五个 RQ：① 语义布局是否降读放大；② benefit scoring 是否避开 full-semantic 代价；③ feedback compaction 是否随 workload-shift 自适应；④ schema 演进 + 动态 delta 是否保正确；⑤ 写/存储/rewrite 代价可见多少。
- 表格归位：RQ1→T1；RQ2→T2,T5；RQ3→T3,T7；RQ4→T4,T10；RQ5→T6,T7。

### §8 相关工作
- 对位区分：LSM/KV 系统（写优化布局，但不把图查询签名当一等 metadata）/ 图存储（拓扑感知，但少做动态更新下的 L0 语义剪枝）/ 自适应索引（查询驱动物理设计，但物理对象与安全边界不同）/ schema 演进系统（版本化解释，但 SemL0 用于图 LSM 段 metadata 与语义剪枝）。
- ⚠️ **Path B 需补**：显式对 **workload-aware/query-driven layout、semantic-aware compaction、learned LSM tuning** 立靶子（reviewer 必问的"外部 SOTA/先例"）。不要把相关工作写成 claim 扩张。

### §9 局限（Limitations）
- 必含：latency workload-dependent / C2 证据分三层（controlled full read、real SF30 retention/write cost、real SF30 metadata proxy）/ external baseline 仅 LiveGraph SF10 / 无生产 write-stall 刻画 / 无完整 schema 迁移引擎 / range·string·compound 谓词为 future work / 最终投稿就绪受 venue·TeX·PDF·page·visual gate 阻塞。

### §10 结论
- 回到 thesis（**应改用新的 lifecycle 论点**）：查询语义能引导 LSM 的物理设计**与生命周期维护**，并在 schema 演进与动态 delta 下保持保守正确。结论不引入新 claim。

---

## 5. 附录计划
- 含：claim/evidence map、artifact path map、schema 演进边界矩阵、复现命令指针、最终投稿 blocker。
- 锚点：`paper/tables/table8-claim-evidence-map.tex`、`table9-artifact-path-map.tex`、`table10-schema-evolution-claim-gap-map.tex`、`paper/artifact-checklist.md`、`paper/package-manifest.md`。

## 6. 写作顺序与理由
1. 引言 + 贡献 → 2. 系统概览 + 共享安全契约 → 3. §4 C1 → 4. §6 C3 正确性 → 5. §5 C2 compaction → 6. 评测 → 7. 局限 → 8. 相关工作 + 结论 → 9. 附录对齐。
- 理由：引言与安全契约约束后续所有 claim；schema/snapshot 正确性应在放宽 feedback/评测措辞之前先锁死。
- 注：Path B 下 §5（C2）是承重；当前已有 controlled + real SF30 结果，可写成正式证据，但必须保留 real SF30 read workload 未重跑的边界。

## 7. 投稿边界（final-submission gates）
- 本 outline 自身不是投稿就绪物。剩余 gate：目标 venue / venue 模板 / TeX·PDF 后端 / 编译 PDF / 页数预算 / PDF 目检。

---

## 附：英文稿过时点同步记录（review checklist，✅ 已于 2026-06-17 同步到英文稿）
1. ✅ **§4 boundary**：已改为"SF100 read-amp/candidate/correctness/RSS 已有(W6)；latency 仅辅助(Gate 1=FALLBACK)；不声称 composite 优于 edge-type-only(W14 partial)"；安全 claim 由 SF1 扩为 SF1+SF100。
2. ✅ **§7 Evaluation**：边界改为"SF100 矩阵已完成(W6)，C2 已有 controlled + real SF30 retention/write-cost 结果"；新增 **RQ6**（C2 跨层 retention）+ 对应表位。
3. ✅ **§9 Limitations**：删除"无 SF30/SF100 refresh"，改为按 Gate 收窄的实测边界（latency workload-dependent、composite 未证、real SF30 read workload 未重跑等）。
4. ✅ **§10 Conclusion**：换成新的 lifecycle thesis 措辞（生命周期控制信号）。
5. ✅ **Evidence to cite**（§1 + §4 + §5）：补 `baseline/sf100-matrix-20260613-cn.md`(W6)、`baseline/w14-final-verdict-20260617-cn.md`(W14)、`SEML0-K4-STATUS-AND-PLAN-20260617-CN.md`，以及 C2 real SF30 `baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md` / `remote-logs/c2-sf30-real-20260619/`。

> 以上 5 点的过时表述**已从英文 `final-paper-outline-md.md` 移除并替换为 Path B / Gate-accurate 措辞**；英文稿与本中文解释稿现已一致。
