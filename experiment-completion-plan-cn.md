# SemL0 实验完成计划

日期：2026-06-05

## 目标

在回归论文写作之前，完成支撑论文所需的实现和实验闭环。

目标不是"更多日志"。目标是：

```text
每个论文论点都有当前代码支持、可复现命令和保留的证据目录。
```

## 当前状态

代码已改进，但实验闭环尚未完成。

当前可用的证据：

- 最新代码 SF1 C1 剪枝和消融实验证据；
- P7.5 类型读取剪枝 bug 诊断和修复；
- 针对性的模式/快照/墓碑/属性正确性测试；
- 反馈压缩针对性测试和微基准脚手架；
- 写/重写成本代理材料。

缺失或未完成：

- 最新代码 SF30/SF100 性能刷新；
- 持续反馈工作负载切换运行；
- 清理后的完整代码就绪门槛；
- 最终证据保留映射；
- 一键复现包；
- 最终实验表冻结。

## 代码就绪门槛

在运行大规模实验之前，验证当前代码能够支撑预期论点。

必检项：

| 门槛 | 目的 | 状态 |
|---|---|---|
| cargo formatting | 确保源码机械清洁 | 本次交接未运行 |
| 针对性引擎测试 | 模式/快照/墓碑/属性/反馈正确性 | 必需 |
| C1 类型读取回归 | 确保 P7.5 误报保持修复状态 | 必需 |
| p3 反馈基准冒烟测试 | 确保长运行前基准二进制可用 | 必需 |
| 命令/日志标记 | 保存二进制/配置/路径元数据 | 必需 |

下一个窗口在通过此门槛之前不应启动 SF30/SF100。

## 实验矩阵

| ID | 实验 | 重要性 | 输出规则 | 当前决策 |
|---|---|---|---|---|
| E0 | 代码就绪门槛 | 防止运行不可用实验 | 新日志目录 | 必须首先运行 |
| E1 | 针对性正确性套件 | 支撑模式/快照/墓碑安全性 | 新日志目录 | 必需 |
| E2 | 最新代码 SF1 C1 刷新 | 在当前代码上确认主要 C1 论点 | 新日志目录 | 必需 |
| E3 | C1 消融表刷新 | 分离语义索引、度策略、阈值策略 | 新日志目录 | 必需 |
| E4 | 持续反馈工作负载切换 | 支撑 P3 维护/适应论点 | 新日志目录，不覆盖旧数据 | 如果 P3 保持为主要贡献则必需 |
| E5 | 模式增量压力 | 加强加性模式演进论点 | 新日志目录 | 模式章节必需 |
| E6 | SF30 刷新 | 超出 SF1 的规模证据 | 新日志目录 | 推荐 |
| E7 | SF100 刷新 | 大规模证据 | 新日志目录 | 除非声称大规模否则可选 |
| E8 | 写/重写成本代理刷新 | 约束维护成本 | 新日志目录 | 推荐 |

## 论点到证据映射

| 论点 | 最少所需证据 | 当前风险 |
|---|---|---|
| C1 语义剪枝减少 L0 读放大 | 最新代码 SF1 加消融 | 大体可用，建议刷新 |
| C1 不破坏类型读取 | P7.5 回归加针对性检查 | 可用但应重新运行 |
| 加性模式变更不使旧存储失效 | 模式时代测试加混合增量压力 | 部分，需要最终运行 |
| 属性谓词在缺失/默认/别名/编码时代下安全 | 针对性属性测试 | 可用但处于原型范围 |
| 反馈压缩适应工作负载切换 | 持续趋势日志 | 未完成 |
| 快照/墓碑正确性在压缩/重开后保持 | 针对性测试 | 可用但应重新运行 |
| 写成本有界 | 代理核算 | 部分，非生产写停顿 |

## 实验日志规则

每次新运行必须使用新目录。不要覆盖现有存储或日志。

命名约定：

```text
remote-logs/e0-code-readiness-YYYYMMDD-HHMMSS
remote-logs/e1-targeted-correctness-YYYYMMDD-HHMMSS
remote-logs/e2-c1-sf1-refresh-YYYYMMDD-HHMMSS
remote-logs/e4-sustained-feedback-YYYYMMDD-HHMMSS
```

每个证据目录应包含：

- 所用命令；
- git/源码状态（如有）；
- 二进制路径或构建配置；
- 配置文件或 CLI 标志；
- 开始/结束时间戳；
- stdout/stderr 日志；
- 汇总表；
- 通过/失败标记。

## 现有命令辅助脚本

现有运行脚本已移出仓库根目录：

```text
scripts/experiments/
```

现有分析脚本已移至：

```text
scripts/analysis/
```

以这些为 E1/E2/E3/E4 的参考，但不要将旧脚本名称作为对应实验已完成的证明。每次新运行仍需要一个新日志目录和一个新的通过/失败汇总。

## 立即下一步

1. 运行 E0 代码就绪门槛。
2. 运行 E1 针对性正确性套件。
3. 运行 E2/E3 最新代码 C1 SF1 刷新和消融。
4. 决定 P3 反馈是否仍为主要贡献。
5. 如果是，运行 E4 持续反馈工作负载切换实验。
6. 运行 E5 模式增量压力。
7. 仅在此之后才决定 SF30/SF100 是否为论文目标所需。

## E12-E17 端到端性能补充

1. 运行 E16 metadata 开销表（复用已有 E11 数据，快速）
2. 修改 E15 server.rs 支持 latency percentile
3. 运行 E12 LDBC 端到端延迟对比
4. 运行 E14 mixed workload 实验
5. 从 E12 解析 E13 update throughput 数据
6. 运行 E17 SF10/SF30 端到端刷新
7. 更新 paper narrative spine 和 claim ledger

## 停止条件

如发生以下任一情况，停止并修复代码后再继续：

- 类型读取不匹配再次出现；
- 模式时代测试在压缩或重开后失败；
- 墓碑分段可能在影响快照正确性时被剪枝；
- 反馈压缩无解释地增加候选分段；
- 实验日志无法识别命令、配置和输出存储。

## E11 基线强化门槛

### 背景与动机

**SemL0 vs LSMGraph 区分**：SemL0 的核心贡献在于将**查询语义的物理设计**引入 LSM+CSR 动态属性图存储的 L0 区域。当前 LSMGraph 风格的系统（如 DGS 参考实现及类似架构）仍然将属性图查询语义视为对存储布局和剪枝"不可见"——即物理层不知道也不利用 `src_label`、`edge_type`、`degree_class` 等查询维度来进行 L0 分段和压缩决策。

本实验矩阵通过系统化的基线对比，隔离出 SemL0 各维度贡献，并建立从"无语义"到"全语义"再到"有预算的全语义"的完整技术栈参照系。

### 基线变体定义

| 名称 | CLI layout 标志 | 角色 | 核心机制 |
|---|---|---|---|
| naive | `--l0-layout naive` | 下界 | 无语义布局，L0 全量重叠，单段 |
| LSMGraph-style LSM-CSR | `--l0-layout lsmgraph-style` | 主基线 | CSR 分段但**无**语义 L0 索引；无反馈压缩 |
| RocksDB-style KV encoding | `--l0-layout kv-lsm` | KV-LSM 基线 | key=(src, etype, dst, ts)，纯范围扫描，无 CSR 摘要 |
| schema-only LSM-CSR | `--l0-layout schema` | 机制隔离 | 仅 schema/source-label 剪枝 |
| label-only | `--l0-layout label-only` | 单维度 | 仅 src_label 语义 |
| edge-type-only | `--l0-layout edge-type-only` | 单维度 | 仅 edge_type 语义 |
| degree-only | `--l0-layout degree-only` | 单维度 | 仅 degree_class 语义 |
| label+edge-type | derived | 2D 组合 | label + edge_type（从 `--l0-layout semantic` 派生，按需禁用 degree） |
| label+edge-type+degree | `--l0-layout semantic` | 3D 组合 | label + edge_type + degree（全语义） |
| full semantic | `--l0-layout semantic` | 上界 | 所有语义维度全量物化 |
| benefit-scored SemL0 | `--l0-layout semantic-budgeted` | 论文当前行 | 选择性物化，按收益评分裁剪 |
| oracle semantic pruning | `--l0-layout oracle` | 理论上界 | 离线已知 Ground Truth，最优 L0 布局 |
| full L0->L1 compact | `--l0-layout full-compact` | 写优化极端 | L0 完全消除，所有数据直接落入 L1 |
| materialized adjacency cache | 独立命令/构建模式 | 读优化极端 | 热邻接全量物化，零 L0 搜索 |

> **注**：`lsmgraph-style`、`kv-lsm`、`oracle`、`full-compact` 四个变体如尚未在 `src/config.rs` 中实现，需要在 E11 执行前完成相应 CLI 标志和处理逻辑的补充实现。可参考现有 `--l0-layout` 分发模式（见 `graph.rs` `build_l0_flush_segments()`）。

### 实验矩阵 A：系统级性能对比

目的：验证整体 SemL0 系统在完整查询+更新工作负载下与各基线的差距。

| System | Layout principle | Update model | QPS | P95 latency | P99 latency | Update throughput | Store bytes |
|--------|-----------------|--------------|-----|-------------|-------------|-------------------|-------------|
| SemL0 (benefit-scored) | query-semantic L0 + CSR | LSM+delta | 待测 | 待测 | 待测 | 待测 | 待测 |
| SemL0 (full semantic) | full semantic L0 + CSR | LSM+delta | 待测 | 待测 | 待测 | 待测 | 待测 |
| LSMGraph-style LSM-CSR | CSR + no semantic L0 index | LSM+delta | 待测 | 待测 | 待测 | 待测 | 待测 |
| RocksDB-style KV-LSM | key=(src,etype,dst,ts) | KV-LSM | 待测 | 待测 | 待测 | 待测 | 待测 |
| naive | single L0 segment | LSM+delta | 待测 | 待测 | 待测 | 待测 | 待测 |
| full L0->L1 compact | no L0 | LSM+delta | 待测 | 待测 | 待测 | 待测 | 待测 |
| materialized adjacency cache | hot adjacency pre-materialized | flat CSR+cache | 待测 | 待测 | 待测 | 待测 | 待测 |

**测试条件**：SF1，IC1/IC5/IC7 查询混合，更新比例 10%/50%/90%，每个配置至少运行 3 次取中位数。

### 实验矩阵 B：机制隔离（逐维度贡献）

目的：通过微基准（单查询读取字节数、候选分段数）隔离每个语义维度的独立贡献。

| Variant | L0 layout signal | Candidate L0 | Read bytes | Avg μs | P50 | P95 | P99 | Import time | L0 files | Mismatches |
|---------|-----------------|--------------|------------|--------|-----|-----|-----|-------------|----------|------------|
| naive | none | 全部 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| LSMGraph-style | CSR segments, no semantic L0 index | 全部 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| schema-only | src_label + edge_type | src_label + edge_type | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| label-only | src_label only | src_label | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| edge-type-only | edge_type only | edge_type | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| degree-only | degree_class only | degree_class | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| label+edge-type | src_label + edge_type | src_label + edge_type | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| label+edge-type+degree | full semantic | exact match | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| full semantic | all dimensions | exact match | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| benefit-scored | selective | scored subset | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| oracle | offline ground truth | optimal | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| full-compact | none (L0 eliminated) | N/A | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |

**测试条件**：SF1，C1 读取，采样 1000 次查询 warm 后取 P50/P95/P99，`--sample-plan-in` 固定采样计划，`--max-mismatches 0` 正确性校验。`Mismatches` 列若 >0 则立即停止。

### 实验矩阵 C：导入与存储成本

目的：量化各变体的写放大、压缩代价和存储空间占用。

| Variant | Import wall time | Write throughput | Compaction bytes | Compaction time | Store bytes | Manifest bytes | L0 file count |
|---------|------------------|-----------------|------------------|-----------------|-------------|----------------|---------------|
| naive | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| LSMGraph-style | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| schema-only | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| label-only | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| edge-type-only | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| degree-only | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| label+edge-type | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| label+edge-type+degree | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| full semantic | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| benefit-scored | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| oracle | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |
| full-compact | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 |

### 实验命名约定

```text
remote-logs/e11-{variant}-{YYYYMMDD}
```

示例：

```text
remote-logs/e11-naive-20260606
remote-logs/e11-lsmgraph-style-20260606
remote-logs/e11-semantic-budgeted-20260606
remote-logs/e11-oracle-20260606
remote-logs/e11-matrix-b-20260606
remote-logs/e11-matrix-c-20260606
```

每个实验目录应包含：
- `run.meta`：git commit hash、二进制路径、CLI 参数
- `import.stdout` / `import.stderr`：导入日志
- `snb-validate-*.json`：验证结果
- `stats.stdout`：存储统计
- `summary.csv`：矩阵表格数据
- `pass.txt` 或 `fail.txt`：通过/失败标记及原因

### 停止条件

如发生以下任一情况，停止当前变体并记录后再继续：

- 正确性不匹配出现（`Mismatches > 0`）：立即停止该变体，记录日志，回溯原因；
- 外部系统构建静默失败（`cargo build` 非零退出但无错误输出）：重新构建并验证；
- 实验日志覆盖旧数据：每个变体必须使用新目录，命名中包含日期戳，旧数据仅追加不覆盖；
- L0 文件数异常激增（超过 `max_l0_segments_per_flush * level_count` 的 3 倍）：检查布局逻辑；
- 导入阶段 panic：记录错误栈，修复代码后再重新运行；
- P95/P99 latency 超过基线 10x 且无明显写放大解释：检查索引一致性。

## E12-E17 实验矩阵：端到端性能补充

### 背景

Draft 当前主要展示 microbenchmark 层面的指标（IO read bytes、candidate L0 segment 计数）。
审稿人必然会问：
- end-to-end query latency 降了多少？
- P50 / P95 / P99 latency 如何？
- QPS 如何？
- update throughput 有没有下降？
- mixed workload 下是否仍然有效？
- query + update 并发时是否有 stall？
- metadata / manifest / file fanout 开销有多大？

E12-E17 补充这些维度的端到端证据。

| ID | 实验 | 重要性 | 输出规则 | 当前决策 |
|----|------|--------|----------|----------|
| E12 | LDBC 端到端延迟/QPS 对比 | 核心：回答 latency 降了多少 | 新日志目录 | 必需 |
| E13 | Update throughput 测量 | 回答 update 吞吐量是否下降 | E12 日志解析 | 必需（可从 E12 派生） |
| E14 | Mixed workload 测试 | 回答 mixed workload 下有效性 | 新日志目录 | 必需 |
| E15 | Write stall 检测 | 回答并发时是否有 stall | server-metrics 扩展 | 必需 |
| E16 | Metadata/manifest/fanout 开销 | 回答元数据成本 | E11 数据解析 | 快速完成 |
| E17 | SF10/SF30 端到端刷新 | 回答规模扩展性 | 新日志目录 | 必需 |

### 论点到证据映射（E12-E17）

| 论点 | 最少所需证据 | 当前风险 |
|------|-------------|----------|
| end-to-end query latency 在语义剪枝后降低 | E12 SF10 多线程延迟对比 | 需要 E12 完成 |
| update throughput 在语义布局下未显著下降 | E13 update latency 数据 | 需要 E12/E14 完成 |
| mixed workload 下语义剪枝仍然有效 | E14 混合读写实验 | 需要 E14 完成 |
| concurrent query+update 无显著 write stall | E15 server percentile 数据 | 需要 E15 代码修改 |
| metadata/manifest 开销有界 | E16 cost table | 快速完成 |

### 停止条件（E12-E17）

如发生以下任一情况，停止当前实验并记录后再继续：

- 正确性验证失败（LDBC validate 不通过）：立即停止，检查 driver/server 一致性；
- latency 数据异常（某变体 P99 > 10x 其他变体且无写放大解释）：检查索引一致性；
- write stall 检测到 P99 latency 激增但无明显 compaction 触发：记录并报告边界；
- metadata 开销超出预期 3x：检查 manifest 写入逻辑；
- SF30 实验 OOM 或超时：调整并发参数或超时阈值。
