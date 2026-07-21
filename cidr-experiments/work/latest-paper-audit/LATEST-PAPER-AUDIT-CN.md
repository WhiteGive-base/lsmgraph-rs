# 最新论文实验需求审计

审计日期：2026-07-21。此报告只审阅用户提供的 ZIP，不修改论文源、正式 `plan/`、`data/` 或 `figures/`。

## 1. 版本与完整性

- 原始 ZIP：`C:\Users\Coword\Desktop\__Query_Signatures_as_a_Storage_Control_Plane_for_Dynamic_Property_Graphs.zip`
- 文件大小：291,529 bytes
- Windows mtime：2026-07-21 22:51:54 +08:00
- ZIP SHA-256：`352E7CA8BCF631F9CFCF61CB8C249B1589201193E646A9B4DE21773A8A7C26F0`
- ZIP 内共 18 个文件、解包后合计 540,460 bytes；逐项哈希见 `ZIP-INVENTORY-SHA256.tsv`。
- 主稿：`tex/seml0-cidr-main-20260630.tex`，732 行，SHA-256 `D9A93232E58A6B791B4EB6DF98CBDE8A37C15672D3BFF610C335E9EF6972B4A8`。
- 两个中文阅读稿字节完全相同，SHA-256 均为 `B432EE788113B545A74FDD8CD80861B5578EEA6A072E2A9EA4380F1DCF8EE646`。
- 文件名中的 `20260630` 不能证明稿件是 6 月 30 日版本；ZIP 和全部 entry 的时间均为 2026-07-21，稿内没有 Git SHA、版本号或生成 manifest。
- ZIP 不含整篇论文 PDF；4 个 PDF 均为单独图片资产。
- 临时解包内容在 `tmp-extracted/`，由本目录 `.gitignore` 排除，不应纳入 `cidr-experiments` commit。

## 2. 最新稿的实际实验结构

主稿的 `Prototype Evidence` 只有四个证据主题（TeX 529--674 行），不是现有完整计划中的六图结构：

1. **SF100 read/budget**：W6 SF100；49,257,601 个 naive candidates、3,461.6 MiB read bytes；semantic variants 约 5.9M--7.9M candidates、642.7--820.0 MiB；45,000 operations、0 mismatch；full semantic peak import RSS 118.03 GiB，对比约 2.2--2.5 GiB。
2. **C2 lifecycle/compaction**：controlled SF1/SF10c 的 full-read 4x--6x；real SF30 的 retention、WA 1.24 vs 1.07，以及 metadata-replay proxy 6.52x vs 1.00x。
3. **Correctness + dynamic**：W13 两次运行、10/10 schema tests；W9 SF30 30 分钟、约 163 QPS，1800 秒时 schema/semantic p99 为 8,740.2/1,274.0 us。
4. **External baseline context**：LiveGraph、Aster、TuGraph、NebulaGraph、Neo4j 的 SF10 digest gate；明确不做完整图数据库 head-to-head。

稿件产物为：1 张设计图、3 张数据图（SF100 budget、C2、SF30 dynamic）和 1 张外部 baseline gate 表。没有 Experimental Setup 章节/表，也没有 component staircase、workload heatmap、四点 scale curve 或 concurrency curve。

稿件 714--724 行又明确列出补证据优先级：

- catalog/semantic-index overhead：catalog size、in-memory index size、reload/build time、metadata/segment；
- property-aware read 小实验：typed-neighbor only、typed-neighbor + predicate、property-absence exact pruning；
- controlled SF1/SF10 上校准 metadata replay 与 full body-read；
- 同一 SF10 外部 workload 的 SemL0 rows；
- 更大的 differential correctness stress；
- dynamic run 的 compaction/L0/candidate/writer/CPU/RSS 事件与资源指标。

## 3. 当前数据能否支撑正式 claim

| 稿件 claim | 当前证据状态 | 正式缺口 | 队列映射 |
|---|---|---|---|
| SF100 candidates/read/RSS 与 118.03 GiB cliff | 数值直接硬编码在绘图脚本；ZIP 无 raw/TSV/log/manifest；旧 W6 只有历史单次/同进程统计 | 固定 Git/binary/host/input/query hash；query 5 个独立进程 run，import 至少 3 次；资源采样和原始记录 | **当前 22 项无直接覆盖**；P61 只计划最终 3--4 variants 的 scaling 点，不足以复现稿中 9 variants |
| C2 controlled 4x--6x 与 SF30 retention/WA/proxy | controlled rows 与 real proxy 混在一图；real SF30 明示不是 full body read | 同一 truth/query 下校准 replay 与 full read；再做 real SF30 full read 或继续明确 proxy | P41 只覆盖 real SF30；缺 controlled calibration |
| W13 no-false-negative fallback | 已找到实际 10/10 run；只有两个完成 run，且历史来源曾指错 | 多 seed update/delete/tombstone/schema/compact/reopen 差分 stress，记录 fallback rate/cost | P00-W13-POINTER、P70 |
| W9 动态 p99 趋势可代表 evolving LSM lifecycle | 旧 W9 `COMPACT_EVERY_SECS=0`；单次、无统一固定 trace；只能说明 no-compaction L0 累积时的 workload-specific 趋势 | 实际 compaction arms、同 trace SHA、3 runs、完整事件/资源 telemetry | P00-RQ3、P40 |
| 五个外部系统使用同一 truth/workload 并通过 gate | 历史系统的查询接口、计时边界、硬件、query consumer 不完全匹配；表格仅写 `yes/archived` | dense-original bridge、同 truth/order/cache、fresh manifests、5 runs；表中落实际 hash/commit/run IDs | P01、P02、P10、P11 |

结论：当前 ZIP 的图能作为历史/prototype evidence，但不能单靠 ZIP 作为正式可复现实验。绘图脚本从 Python 常量直接取数（`SF100`、`C2`、`DYNAMIC_SF30`、`BASELINES`），没有读取原始或规范化数据，也没有误差线/CI/独立重复 provenance。

## 4. 对现有 22 项队列的变更建议

### 新增（最新稿明确要求、原队列未完整覆盖）

1. `P03-E00-SETUP-MANIFEST-TABLE`：冻结 hardware/software、Git/binary SHA、dataset/query/trace SHA、cache/timing/repeat/statistics protocol，并生成论文 Experimental Setup 表。
2. `P32-G3-SF100-BUDGET-CORE`：正式重跑稿中 SF100 全 budget matrix；至少覆盖图中所有被比较 variants，query 5 个独立进程 run、import 3 runs、digest + full telemetry。不能用 P61 的 3--4 点 scaling 子集代替。
3. `P42-G4-C2-PROXY-CALIBRATION`：controlled SF1/SF10，naive/semantic 对同一 query 同时采 metadata replay 与 full body read，验证方向、倍率/相关性和 digest；然后才解释 real SF30 proxy。
4. `P53-G5-PROP-SIGNATURE-MICRO`：三组明确 workload：typed only、typed + property predicate、property-absence exact pruning；独立标注 blocker-preserving prototype 和安全前提。

### 保持，但按最新稿收紧

- 保持四个 P00 parser/registry 修复；它们是 provenance 前置条件。
- 保持 P01、P02、P10、P11，用于稿中的 digest-gated SF10 context；不要把结果表述为完整 DB 排名。
- 保持 P30 仅作 import-only provisional 诊断，不可升级成 formal full-lifecycle evidence。
- 保持 P31，并新增必采字段：`semantic_index_memory_bytes`、catalog/index build 与 reload wall time、`metadata_bytes_per_segment`；现有 `reopen_wall_s` 和 persistent bytes 不能完全替代这些量。
- 保持 P40；其 fixed trace、四 arms、3 repeats 和 telemetry 正好替换不合格的 W9 claim。
- 保持 P41，但放在 P42 之后，作为 real SF30 full-read 验证。
- 保持 P70，作为 W13 两次 10/10 之外的正式 safety gate。

### 从“当前稿正式补跑关键路径”删除/降级（不建议物理删除队列记录）

- `P12` full LDBC E2E：稿件明确说 optimized API 只支持 concrete-source outgoing typed-neighbor，且不做完整 DB head-to-head。
- `P20/P21` A0--A6 component staircase：最新稿没有 component-ablation 图或 causality claim。
- `P50` W8 provisional heatmap、`P51` 全 workload matrix、`P52` SF30 representative：当前稿只明确要求小型 property micro；由新增 P53 覆盖最小正式需求，完整 matrix 可留 future work。
- `P60/P61/P62` data-size/concurrency scaling：最新稿无 scaling/concurrency 图或 claim；P61 不能冒充 SF100 budget 主实验，应保留为未来 scaling 或由新增 P32 单独覆盖。

## 5. 对正式 plan/figure contract 的影响

- 现有 F1--F6 完整计划比最新稿更宽，不能继续把“六张 formal 图全部完成”当作最新稿的唯一验收标准。
- 最新稿需要的正式数据 contract 应对应：SF100 budget/read/RSS、C2 retention/full-read/proxy、dynamic trace/telemetry、baseline gate/setup 表，以及 future-work 明列的 overhead/property/safety 数据。
- 现有 F3/F4 的多数 resource/dynamic 字段可复用；F1 可降为 baseline table；F2/F5/F6 暂不进入当前稿主线。
- 论文源最终还需要补 Experimental Setup、run counts/CI、硬件和版本、query/cache/timing 边界；本审计没有修改论文源。
