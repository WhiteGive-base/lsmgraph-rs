# E11 Paper Text Patches — SemL0 Baseline-Strengthening Experiment

These patches are designed to be applied to `paper.md`. They introduce RQ0, update the Introduction, expand Related Work, and update the Limitations boundary summary.

---

---PATCH: Introduction---

**Location**: After the current contributions list (after "5. 一个 source-ready 的证据包" paragraph, around line 43)

**Find**:
```
1. 一种面向动态图属性图的查询语义 LSM 物理设计。SemL0 将标签、边类型、方向、度类别、属性要求、快照与 schema epoch 暴露为面向存储的元数据，而不是仅仅把它们当作查询计划中的谓词。
2. 一种用于语义 L0 布局的选择性 materialization 策略。收益评分在限制元数据和 fanout 的前提下保留高价值的语义分区，避免对每一种语义组合都完全 materialize 的成本。
3. 一个反馈驱动的语义 compaction 回路。运行时反馈可以识别热点图访问签名，并在受控工作负载迁移下重新定向 compaction 优先级；重写成本代理记账则与生产级 write-stall claim 明确分离。
4. 一个面向语义剪枝的 schema / snapshot 安全模型。版本化 schema catalog、segment `schema_epoch`、tombstone-sensitive 摘要以及"仅精确证明才可剪枝"的规则，使旧存储在增量 schema 变化和特定动态图 delta 下仍然可读，并避免假阴性。
5. 一个 source-ready 的证据包。当前包将 claim 映射到表格、命令、源码测试、store 保留决策和最终提交阻塞条件，使论文证据与未批准的后续实验彼此分离。
```

**Insert after** the paragraph ending with "5. 一个 source-ready 的证据包。":

```
通过回答这些问题，SemL0 主张：属性图查询语义可以指导 LSM 物理设计，而不必局限于拓扑与写入局部性。本文的中心观点不是"每个图数据库功能都已实现"，而是：标签、边类型、属性、schema epoch、snapshot 与运行时反馈，都可以在不牺牲保守正确性的前提下变得对存储层可见。

一个关键区分必须在开头就明确。SemL0 并不是一种新的 LSM+CSR 图存储——底层 LSM delta 写路径、CSR segment 格式和 MemGraph buffer 都是共享基础设施。SemL0 所添加的是**查询语义的物理设计**，作用于 LSM+CSR 动态属性图存储的 L0 区域。现有的 LSMGraph 风格系统虽然也在 LSM delta 区域维护 CSR segment，但把属性图查询语义视为对布局和剪枝"不可见"。SemL0 则把图访问签名——标签、边类型、度类别、属性要求、快照和 schema epoch——变成 L0 segment 候选选择的**一等输入**。这一区分是第 7 节 RQ0 中基线对比的基础。
```

---

---PATCH: Evaluation: RQ0---

**Location**: Section 7 (Evaluation), before RQ1

**Find**:
```
### RQ1：查询语义布局是否减少读放大？
```

**Insert before** `### RQ1：查询语义布局是否减少读放大？`:

```
### RQ0：SemL0 是否不仅仅是 LSMGraph 工程扩展？

在衡量 SemL0 的性能之前，必须先确认其贡献不仅仅是已有 LSM+CSR 基线的工程改进。我们与多个系统级和机制级基线进行对比，以隔离**查询语义 L0 物理设计**的独立效果。

naive L0 扫描基线展示了 L0 读放大的严重程度，且没有任何语义剪枝。LSMGraph 风格 LSM-CSR 基线将 SemL0 读路径中的语义 L0 索引移除，仅保留 key-range 过滤——隔离出了"将图查询语义暴露给候选选择"这一效果。单维度基线（仅标签、仅边类型、仅度）则检验是否有任何一个单独的语义维度驱动了收益，还是必须组合使用。Oracle 语义剪枝基线通过假设离线获知每个查询的真实候选集，提供了理论上限。

结果以三个表格呈现。表 E11-1 进行系统级基线对比。表 E11-2 展示所有变体的机制级隔离。表 E11-3 报告成本权衡。
```

---

---PATCH: Evaluation: Table E11 placeholders---

**Location**: Section 7 (Evaluation), after the current RQ1-RQ5 tables (after the RQ5 section ending, before "### Evaluation 流程")

**Find**:
```
### Evaluation 流程
```

**Insert before** `### Evaluation 流程`:

```
### 表 E11-1：系统级基线对比

[占位符——待运行外部系统对比后填充。指标：QPS、P95/P99 延迟、更新吞吐、存储字节。]

| System | Layout principle | QPS | P95 μs | P99 μs | Update tput | Store GB |
|--------|-----------------|-----|--------|--------|-------------|----------|
| SemL0 (benefit-scored) | query-semantic L0 + CSR | 待测 | 待测 | 待测 | 待测 | 待测 |
| SemL0 (full semantic) | full semantic L0 + CSR | 待测 | 待测 | 待测 | 待测 | 待测 |
| LSMGraph-style LSM-CSR | CSR, no semantic L0 index | 待测 | 待测 | 待测 | 待测 | 待测 |
| RocksDB-style KV-LSM | key=(src,etype,dst,ts) | 待测 | 待测 | 待测 | 待测 | 待测 |
| naive | single L0 segment | 待测 | 待测 | 待测 | 待测 | 待测 |
| full L0→L1 compact | no L0 | 待测 | 待测 | 待测 | 待测 | 待测 |
| materialized adjacency cache | hot adjacency pre-materialized | 待测 | 待测 | 待测 | 待测 | 待测 |

**测试条件**：SF1，IC1/IC5/IC7 查询混合，更新比例 10%/50%/90%，每个配置至少 3 次取中位数。

### 表 E11-2：机制级隔离（SF1）

| Variant | L0 布局信号 | 候选 L0 | 读取字节 | Avg μs | P50 | P95 | P99 | Import s | L0 文件数 | 正确性 |
|---------|------------|---------|---------|--------|------|------|------|---------|-----------|--------|
| naive | 无 | — | — | — | — | — | — | — | — | — |
| LSMGraph-style | 仅 key/range | — | — | — | — | — | — | — | — | pass |
| schema-only | src_label | — | — | — | — | — | — | — | — | pass |
| label-only | 仅 src_label | — | — | — | — | — | — | — | — | pass |
| edge-type-only | 仅 edge_type | — | — | — | — | — | — | — | — | pass |
| degree-only | 仅 degree_class | — | — | — | — | — | — | — | — | pass |
| label+edge-type | src_label+edge_type | — | — | — | — | — | — | — | — | pass |
| label+edge-type+degree | src_label+edge_type+degree | — | — | — | — | — | — | — | — | pass |
| full semantic | 所有维度 | — | — | — | — | — | — | — | — | pass |
| benefit-scored SemL0 | 选择性 | — | — | — | — | — | — | — | — | pass |
| oracle | Ground Truth | — | — | — | — | — | — | — | — | N/A |
| full-compact | L0 消除 | — | — | — | — | — | — | — | — | pass |

**测试条件**：SF1，C1 读取，采样 1000 次 warm 后取 P50/P95/P99，`--sample-plan-in` 固定采样计划，`--max-mismatches 0` 正确性校验。

### 表 E11-3：成本对比

| Variant | Import s | L0 文件数 | Manifest MB | Store GB |
|---------|---------|---------|------------|---------|
| naive | 待测 | 待测 | 待测 | 待测 |
| LSMGraph-style | 待测 | 待测 | 待测 | 待测 |
| schema-only | 待测 | 待测 | 待测 | 待测 |
| label-only | 待测 | 待测 | 待测 | 待测 |
| edge-type-only | 待测 | 待测 | 待测 | 待测 |
| degree-only | 待测 | 待测 | 待测 | 待测 |
| label+edge-type | 待测 | 待测 | 待测 | 待测 |
| label+edge-type+degree | 待测 | 待测 | 待测 | 待测 |
| full semantic | 待测 | 待测 | 待测 | 待测 |
| benefit-scored | 待测 | 待测 | 待测 | 待测 |
| oracle | 待测 | 待测 | 待测 | 待测 |
| full-compact | 待测 | 待测 | 待测 | 待测 |
```

---

---PATCH: Related Work: SemL0 vs LSMGraph distinction---

**Location**: Section 8 (Related Work), "动态图存储" subsection

**Find**:
```
### 动态图存储

动态图系统通常优化的是可变图存储、事务访问与新鲜分析。LiveGraph 使用事务边日志来支持顺序 adjacency-list 扫描 [@zhu2020livegraph]。Teseo 关注结构性动态图及其更新 / 扫描性能 [@leo2021teseo]。LLAMA 使用大规模多版本数组来支持基于 CSR 的可变图分析 [@macko2015llama]。LDBC Social Network Benchmark 则提供了一套评估图系统的标准交互式工作负载 [@erling2015ldbc]。

SemL0 与它们共享动态图动机，但它优化的对象是 LSM delta 区域，而不是一个完整图执行引擎。它的 segment 元数据将标签、边类型、度类别、属性摘要、schema epoch 与 tombstone sensitivity 暴露给候选剪枝与 compaction。这里的存储层问题更窄：查询语义能否减少需要读取的最近 segment 数量，同时不违反 snapshot 或 schema 正确性？
```

**Replace the last paragraph** (starting with "SemL0 与它们共享动态图动机……") with:

```
SemL0 与它们共享动态图动机，但它优化的对象是 LSM delta 区域，而不是一个完整图执行引擎。它的 segment 元数据将标签、边类型、度类别、属性摘要、schema epoch 与 tombstone sensitivity 暴露给候选剪枝与 compaction。这里的存储层问题更窄：查询语义能否减少需要读取的最近 segment 数量，同时不违反 snapshot 或 schema 正确性？

**SemL0 vs. LSMGraph 风格 LSM+CSR。** 最关键的对比是与 LSMGraph 风格 LSM+CSR 图存储的对比——后者与 SemL0 共享相同的 CSR segment 格式和 MemGraph buffer，但在读路径中不对属性图查询语义暴露给 L0 区域。区别完全在读路径：LSMGraph 风格系统使用 key-range 和 source-label 过滤进行 L0 候选选择，而 SemL0 额外使用了边类型分区、度类别摘要和属性存在位图。这一**查询语义 L0 剪枝表面**正是 SemL0 的贡献所在。表 E11-1 和表 E11-2 对此进行了量化。
```

---

---PATCH: Limitations: Claim Boundary Update---

**Location**: Section 9 (Limitations), "边界总结" table

**Find**:
```
| 领域 | 当前支持的 claim | 尚不支持 |
|---|---|---|
| performance scale | latest-code SF1 evidence | final latest-code SF30/SF100 performance |
| feedback compaction | controlled workload-shift adaptation | production long-run adaptation or write-stall safety |
| write cost | import/store/rewrite proxies | production write-overhead characterization |
| schema evolution | additive no-rebuild old-storage readability | full physical schema migration |
| property predicates | fixed-width equality and presence/absence boundary | range/string/compound predicates and SQL null semantics |
| correctness | targeted schema/snapshot/tombstone regression coverage | exhaustive dynamic-graph differential proof |
| artifacts | source-ready MD/table/evidence package | venue-specific compiled final PDF |
| store cleanup | retention and approval packet | deletion execution |
```

**Add a new row** before the last row (`| artifacts | …`):

```
| baseline strengthening | LSMGraph-style comparison, multi-dimension ablation, oracle upper bound | external system artifact availability |
```

The resulting table becomes:

```
| 领域 | 当前支持的 claim | 尚不支持 |
|---|---|---|
| performance scale | latest-code SF1 evidence | final latest-code SF30/SF100 performance |
| feedback compaction | controlled workload-shift adaptation | production long-run adaptation or write-stall safety |
| write cost | import/store/rewrite proxies | production write-overhead characterization |
| schema evolution | additive no-rebuild old-storage readability | full physical schema migration |
| property predicates | fixed-width equality and presence/absence boundary | range/string/compound predicates and SQL null semantics |
| correctness | targeted schema/snapshot/tombstone regression coverage | exhaustive dynamic-graph differential proof |
| baseline strengthening | LSMGraph-style comparison, multi-dimension ablation, oracle upper bound | external system artifact availability |
| artifacts | source-ready MD/table/evidence package | venue-specific compiled final PDF |
| store cleanup | retention and approval packet | deletion execution |
```

---

## Patch Application Order

Apply patches in the following order to avoid anchor shifts:

1. **Patch 1** (Introduction — Core Claim Addition) — adds the SemL0 vs LSMGraph distinction paragraph after the contributions list
2. **Patch 2** (Evaluation — New RQ0 Section) — inserts RQ0 before RQ1
3. **Patch 3** (Evaluation — Table E11 placeholders) — inserts three tables before "Evaluation 流程"
4. **Patch 4** (Related Work — SemL0 vs LSMGraph distinction) — replaces the last paragraph of the "动态图存储" subsection
5. **Patch 5** (Limitations — Claim Boundary Update) — adds a row to the boundary summary table

## E11 Experiment Dependency

These patches assume E11 will produce data for:

- **Table E11-1**: system-level QPS / latency / throughput metrics — requires running SemL0 (benefit-scored + full semantic), LSMGraph-style variant, RocksDB-style KV-LSM variant, naive, full-compact, and materialized adjacency cache on SF1
- **Table E11-2**: mechanism-level isolation per variant — requires running all 12 variants with `--sample-plan-in` sampling on SF1 C1 reads
- **Table E11-3**: cost comparison per variant — requires running import + stats for all 12 variants

All "待测" placeholders must be filled before final submission. The RQ0 section and the LSMGraph-style row in the mechanism table should be prioritized, as they are the primary evidence that SemL0's contribution is more than an LSMGraph engineering extension.
