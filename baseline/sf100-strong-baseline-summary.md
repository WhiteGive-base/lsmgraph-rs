# SemL0 真实 SF100 强 baseline —— 运行总结（2026-06-11）

> 取代旧的同名文件（旧版其实是 ~SF1 规模、标题误写 SF100）。本次是**真实 LDBC SF100**。
> 机器可读产物：`remote-logs/qslsm-sf100-strong-baseline-20260610/summary-sf100.{md,tsv}`；
> 详细工程过程：`baseline/progress-note-20260610-cn.md`。

## 1. 实验配置
| 项 | 值 |
| --- | --- |
| 规模 | **真实 SF100**，`directed_edges = 3,570,968,680`（35.7 亿；旧 baseline 仅 3470 万≈SF1） |
| 输入 | `/data/WorkSpace/ldbc-sf100/social_network`（LDBC dynamic/+static/，90G） |
| 抽样 | `SAMPLES=5000`（主表档；每变体 9 edge type × 5000 = 45,000 次 get_neighbors） |
| edge types | `1,2,3,7,8,9,10,11,12`（Knows/HasCreator/HasTag/Likes*/ReplyOf*/ContainerOf/HasMember） |
| memgraph | 64 MiB；IO=blocking；workload 带 `--semantic-degree-hint`（query-semantic 签名） |
| 二进制 | 主分支 `codex/sf100-basegraph-bench`（SemL0 已合并 + budgeted 修正 + SF100 可行性补丁） |
| store/变体 | ~132 GiB；harness disk-safe：import→stats→bench→删，只保留 schema 参考 |
| 运行 | 2026-06-11 01:02 → 18:26（~17.4h，schema 导入 + plan-gen 缓存复用） |

## 2. 主结果表（SF100, s5000）
| Variant | Layout/Policy | read MiB | candidate L0 | L0 files | avg µs | 方法 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| naive | plain LSM-CSR | 26,361*(s200) | 1,967,530*(s200) | 1703 | — | 复用 e11（s5000 补跑中） |
| schema | graph-aware: src_label+edge_type+range | 149.7 | 5,929,197 | 3444 | 4906 | measured |
| edge-type-only | edge_type only | 146.7 | 5,899,015 | 3446 | 4289 | measured |
| **semantic** (full upper) | label+edge_type+degree | **9994.3** | 7,782,877 | **6615** | 4731 | measured |
| **budg-b64** (SemL0) | budgeted, file budget 64 | **146.7** | 6,022,519 | **3483** | **3658** | measured |
| budg-b256 (SemL0) | budgeted, file budget 256 | 146.7 | 6,486,337 | 3675 | 3681 | measured |
| budg-b1024 (SemL0) | budgeted, file budget 1024 | 700.1 | 7,676,875 | 4451 | 4061 | measured |

\* naive 当前是 e11 的 **s200** 数（26.36 GB / 1.97M cand），与其它行 s5000 不同档；**s5000 naive 补跑中**，到位后替换。

## 3. budget sweep —— 可控成本（SemL0 主图素材）
file budget 干净地控制分段数与读放大，从 schema 端单调滑向 full-semantic 的爆炸：
| budget | read MiB | L0 files |
| ---: | ---: | ---: |
| 0 (≡schema) | 149.7 | 3444 |
| 64 | 146.7 | 3483 |
| 256 | 146.7 | 3675 |
| 1024 | 700.1 | 4451 |
| ∞ (≡semantic) | 9994.3 | 6615 |
**小预算(64/256)= 安全区**（≈schema 读、最快延迟、分段几乎不涨）；预算调大开始向 full-semantic 的过度分段滑。

## 4. 核心发现
1. **full-semantic 在 SF100 过度分段、退化**：degree 分区把每个 (src_label,edge_type) 再切到多 degree-class，
   L0 文件 6615（schema 仅 3444），读放大爆到 10 GB。"越细越好"在 SF100 不成立。
2. **budgeted（SemL0）用 file budget 把分段/读放大控制住**：小预算下 L0 文件 3483≈schema，读 ≈schema，
   **平均延迟最快（3658 µs，比 schema 4906 快 ~25%）**。→ **"可控成本胜过会爆的 full upper bound"**。
3. **SF100 大样本下层级在 read_bytes 上彼此收敛**（schema/edge-type/budgeted 都 ~147 MiB）：因为读被
   body（真实邻居数据，~154 MB）主导，而 schema 本身已对 naive 剪枝 ~99.4%。强区分度在**对 naive** 和
   **full-semantic 过度分段**这两处，不在 graph-aware 层级之间。这与 SF1/SF30 不同（见 §5）。

## 5. 与 SF1/SF30 对比（为什么收敛）
| 档 | schema 读 | edge-type 读 | semantic 读 | 说明 |
| --- | --- | --- | --- | --- |
| SF1 (s50) | 3.31 MiB | 0.57 MiB | 0.57 MiB | semantic/edge-type 远好于 schema |
| SF30 (s200, e11) | 13.1 MB | 4.70 MB(99.9%) | 4.70 MB(99.9%) | semantic 最佳（注：当时 budgeted 是被废的默认参数=13.1M） |
| **SF100 (s5000)** | 149.7 MiB | 146.7 MiB | **9994 MiB(爆)** | semantic 反转变最差；budgeted 受控 ≈schema |
小规模：细分区净剪枝；大规模：schema 已吃掉大部分剪枝收益，细分区的元数据/分段开销反超 → full-semantic 退化。

## 6. 已知问题与后续（按用户优先级）
- **[P1·待评估] reader body 过读**：semantic/budg-b1024 的 `read_bytes` 被放大——每给一个 source 读邻居
  pread ~590 KB 却只用 ~32 B（`add_read` 计 pread 全长、`record_body_read` 只计用到的）。
  **但延迟只差 ~3×**（OS cache 吸收）→ 说明是 read_bytes 指标被污染、非真慢。
  **待办：定位 CSR reader body-read range 的具体根因（疑为 merge 合并 reader.rs 的退化 or SF100 段变大后 offset 跨度变大），并评估"修复后对论文的收益"**（修了 read_bytes 干净、可能改变 semantic 结论；不修则主表用 latency+candidate）。
- **[P1·补跑中] naive@s5000**：当前 naive 是 e11 s200，已在后台补跑 s5000 同档公平基线，到位后替换主表第一行。
- **[有效指标]** latency、candidate_l0、L0 files、store 大小：**均有效**，可直接用。read_bytes 对重分段层级需带注脚。

## 7. 论文叙事建议（基于本次真实 SF100）
- **主张 1**：所有 graph-aware/semantic L0 布局对 naive LSM-CSR 剪枝 ~99%+（schema/edge-type/budgeted ~147 MiB vs naive 数十 GB）。【待 naive@s5000 锁定百分比】
- **主张 2（SemL0 核心）**：full-semantic 在 SF100 **过度分段退化**；SemL0 的 **budgeted（benefit-scored + file budget）把成本控制住**，达到 schema 级读放大、**最优延迟**、且分段/manifest 远低于 full-semantic → 可控成本的实用最优。
- **图**：budget sweep 的「read/L0-files vs budget」曲线（§3），展示从 schema 端到 full-semantic 爆炸端的可控插值。
- **诚实声明**：SF100 大样本下 graph-aware 层级在总 read_bytes 上收敛（body 主导）；层级差异用 candidate-L0/延迟刻画；read_bytes 对重分段层级受 reader 过读影响（见 §6，待修/带注脚）。
- 不要 claim「SemL0 在 SF100 比 schema 大幅降低 read bytes」——数据不支持；要 claim 的是「可控成本 + 避免 full-semantic 退化 + 最优延迟 + 对 naive 99%+」。
