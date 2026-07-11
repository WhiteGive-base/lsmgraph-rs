# SemL0 设计空间对比表候选

更新日期：2026-07-11

## 1. 结论

可以为 SemL0 制作一张类似 Aster Table 1 的设计空间总结表，但不建议直接照搬“Update / Lookup / Scalability + 主观星级”的形式。

Aster 的表先定义三项评价标准，再对四种磁盘存储结构作定性概括，用来引出 Poly-LSM 无法同时兼顾 update、lookup 和 scalability 的问题。SemL0 的核心问题不同：它不是在 vertex-based 与 edge-based update 之间选择，而是在 semantic pruning precision（语义剪枝精度）、resource cost（资源成本）和 adaptability（自适应能力）之间选择。因此更适合 SemL0 的指标是：

1. typed-neighbor admission efficiency（带类型邻居读取准入效率）；
2. import-memory efficiency（导入内存效率）；
3. L0 file efficiency（L0 文件效率）；
4. degree precision（度数语义精度）；
5. hard budget（是否有硬预算）；
6. feedback input（是否支持反馈输入）；
7. durable realization（是否为持久化物理布局）。

主稿中推荐使用“真实数值 + 设计能力”的紧凑表，而不是只放星级。星级版本可以保留为视觉草案，但必须公开评分阈值，且不能解释成统计显著性或总体性能排名。

## 2. Aster 表格在原论文中的作用

Aster 在 Table 1 前明确给出：

- Update：处理边插入、删除等图演化的能力；
- Lookup：沿边遍历和邻居查询的能力；
- Scalability：图规模增加时性能退化的程度，不是横向扩展机器数量。

表格随后用等级概括 linked list、relational table、vertex-based LSM 和 edge-based LSM，目的是暴露现有结构的冲突，再引出 Poly-LSM。它是 motivation/design-space table（动机与设计空间表），不是一张直接由某个 benchmark 计算出来的实验结果表。

SemL0 也可以采用同样的叙事结构：query-blind LSM 的文件数和内存成本较低，但几乎没有语义准入能力；full semantic 有更高语义精度，却产生文件和内存悬崖；budgeted SemL0 试图在两者之间提供有界工作点。

## 3. 完整真实数据底表

下表是所有后续简化表和星级表的唯一数据底表。Candidate L0 和 peak RSS 来自 W6 SF100；L0 files 来自最新 W6 SF10。两个规模不能在表头中省略。

| Variant | SF100 candidate L0 | 相对 naive | SF100 peak RSS GiB | SF10 L0 files | 实现含义 |
|---|---:|---:|---:|---:|---|
| naive | 49,257,601 | 100.00% | 2.43 | 170 | 不主动进行语义分区 |
| kv-lsm | 49,257,601 | 100.00% | 2.49 | 170 | KV-style 范围布局，语义证据为 Unknown/Mixed |
| schema | 5,929,197 | 12.04% | 2.42 | 376 | 起点标签/边类型精确，degree 为 Mixed |
| edge-only | 5,899,015 | 11.98% | 2.53 | 380 | 边类型分区，标签和 degree 较粗 |
| budg-b64 | 6,022,507 | 12.23% | 2.21 | 444 | schema 基础上，在 $B=64$ 下选择 degree-exact 候选 |
| semantic | 7,782,877 | 15.80% | 118.03 | 737 | 所有标签/边类型分组都参与 degree-class 拆分判断 |
| oracle | 532,193 | 1.08% | 2.67 | 170 | 导入后建立的精确内存 L0 索引，仅作上界参考 |

解释：

- Candidate L0 越低，表示 typed-neighbor query 在读取正文前准入的 L0 文件越少。
- Full semantic 的 candidate L0 高于 schema/b64 并不矛盾。更细分区能够改善部分语义维度，但也增加与一个 source range 重叠的物理文件数。
- Oracle 的 candidate 最低，但它不是可直接部署的持久化 layout，因此不能只凭这一行声称 oracle 是最佳系统。
- Read bytes 没有进入底表，因为 SF100 存在 offset-array/cache over-read caveat，论文已经把它降为次要指标。
- Latency 没有进入底表，因为 SF100 的 b64/schema latency gate 为 `FALLBACK`，不适合转成稳定的星级差异。

## 4. 推荐放入主稿的紧凑表

为了服务论文主线，建议把 naive 与 kv-lsm 合并为 Query-blind LSM，省略次要 edge-only ablation，只保留五个设计点。该表可以替换当前主稿 Table 1，而不是在六页正文中额外增加一张重复表。

| 指标 | Query-blind LSM | Schema | Budgeted SemL0（b64） | Full semantic | Oracle reference |
|---|---|---|---|---|---|
| Admission evidence | Unknown/Mixed | Exact label/type，Mixed degree | Exact label/type；入选组可 degree-exact | 所有组参与 degree 拆分 | Exact in-memory index |
| SF100 candidate L0 | 49.26M（100%） | 5.93M（12.0%） | 6.02M（12.2%） | 7.78M（15.8%） | 0.53M（1.08%） |
| SF100 peak RSS | 2.43/2.49 GiB | 2.42 GiB | **2.21 GiB** | **118.03 GiB** | 2.67 GiB |
| SF10 L0 files | 170 | 376 | 444 | 737 | 170 |
| Hard file budget | No | No | **Yes，$B=64$** | No | N/A |
| Feedback input | No | No | **Supported** | No | N/A |
| Durable physical layout | Yes | Yes | Yes | Yes | **No** |
| 在论文中的角色 | 语义盲下界 | 强静态基线 | 目标工作点 | 资源悬崖端点 | 剪枝上界参考 |

这张表要传达的不是“b64 每一项都最好”，而是：

- schema 已经是很强的静态基线；
- b64 的 candidate 数并未稳定优于 schema；
- b64 的贡献是硬资源边界和反馈入口；
- full semantic 展示无预算语义物化的资源失败模式；
- oracle 只定义剪枝上界，不是生产可选项。

“Feedback input = Supported”只表示实现允许配置权重和观测反馈共同进入选择。W6 导入行不能被描述成来自长期生产 workload 的在线学习结果；反馈移动的独立证据来自 W7 SF30-derived workload shift。

## 5. Aster 风格星级版本

如果希望视觉上接近 Aster Table 1，可以只对三项实测指标使用星级，对设计能力继续使用 Yes/No/N/A。星级只能在同一行内比较。

| 指标 | Query-blind LSM | Schema | Budgeted SemL0（b64） | Full semantic | Oracle reference |
|---|---:|---:|---:|---:|---:|
| Read-admission efficiency | ★☆☆☆☆ | ★★★★☆ | ★★★★☆ | ★★★☆☆ | ★★★★★ |
| Import-memory efficiency | ★★★★★ | ★★★★★ | ★★★★★ | ★☆☆☆☆ | ★★★★☆ |
| L0-file efficiency | ★★★★★ | ★★★★☆ | ★★★☆☆ | ★★☆☆☆ | ★★★★★ |
| Hard file budget | No | No | Yes | No | N/A |
| Feedback input | No | No | Supported | No | N/A |
| Durable physical layout | Yes | Yes | Yes | Yes | No |

### 5.1 星级计算规则

星级不是凭印象填写，而是由下列固定阈值从底表计算：

**Read-admission efficiency**，使用 SF100 `candidate_L0 / naive_candidate_L0`：

| 比率 | 星级 |
|---:|---:|
| $\le 2\%$ | ★★★★★ |
| $(2\%,13\%]$ | ★★★★☆ |
| $(13\%,25\%]$ | ★★★☆☆ |
| $(25\%,60\%]$ | ★★☆☆☆ |
| $>60\%$ | ★☆☆☆☆ |

**Import-memory efficiency**，使用 SF100 `peak_RSS / naive_peak_RSS`：

| 比率 | 星级 |
|---:|---:|
| $\le 1.05\times$ | ★★★★★ |
| $(1.05,1.25]\times$ | ★★★★☆ |
| $(1.25,2]\times$ | ★★★☆☆ |
| $(2,10]\times$ | ★★☆☆☆ |
| $>10\times$ | ★☆☆☆☆ |

**L0-file efficiency**，使用 SF10 `L0_files / naive_L0_files`：

| 比率 | 星级 |
|---:|---:|
| $\le 1.1\times$ | ★★★★★ |
| $(1.1,2.25]\times$ | ★★★★☆ |
| $(2.25,3]\times$ | ★★★☆☆ |
| $(3,4.5]\times$ | ★★☆☆☆ |
| $>4.5\times$ | ★☆☆☆☆ |

这些阈值是为了把现有设计点压缩成可读等级，并不是统计学阈值。因此：

- 不应对三行星级求平均或形成 overall score（总体分数）；
- 不应把相邻星级描述为显著性能差异；
- camera-ready 版本若保留星级，caption 或正文必须给出“derived from measured bins”的说明；
- 更严格的 CIDR 写法仍然是保留真实数值表，把星级版留在中文说明或 artifact 中。

## 6. 为什么选择这些指标

### 6.1 Read-admission efficiency

这是 SemL0 最直接的机制指标。系统在读取正文前使用 `GraphAccessSignature` 和 `CsrSegmentMeta` 判断 candidate segment。Candidate L0 比 latency 更接近设计本身，也不会直接受到正文解码和 OS cache 状态影响。

### 6.2 Import-memory efficiency

这是 budgeted control 相比 full semantic 的核心价值。SF100 上 b64 为 2.21 GiB，而 full semantic 为 118.03 GiB，能够直接展示 memory cliff。

### 6.3 L0-file efficiency

语义分区会增加物理文件数。只展示 candidate reduction 而不展示 L0 files，会掩盖 semantic materialization 的 fanout cost（扇出成本）。SF10 提供了完整且可读的文件数对比。

### 6.4 Hard budget 和 feedback input

这两项用于解释为什么 b64 不是简单的静态 schema 或缩小版 full semantic：

- `used + cost <= B` 提供硬性文件预算；
- `query_weight = max(configured_weight, feedback_weight)` 提供需求反馈入口。

### 6.5 Durable physical layout

该项防止把 oracle 当作可部署系统。Oracle 是导入后构建的内存索引，用于给出剪枝上界，没有与其他布局相同的持久化构建成本。

## 7. 不应放入这张表的指标

### 7.1 SF100 latency 星级

不能使用。虽然 b64 的平均延迟低于 schema，但一倍标准差区间重叠，现有 gate 为 `FALLBACK`。把它画成不同星级会比论文当前口径更强。

### 7.2 SF100 read bytes 星级

不能作为主要行。Reader 会在 metadata-cache miss 后重读整个 offset array，SF100 方差较大。Read bytes 可以继续保留在 Figure 2，但不应作为定性总表的主要评价轴。

### 7.3 Update throughput

当前 W6 主要测 import resource 和 read admission，不是与 Aster 相同的在线边更新 benchmark。没有足够证据把 SemL0 variants 评为一到五星的 update performance。

### 7.4 Scalability 星级

目前只有 SF10/SF100 两个主要资源点，并且各项指标的采样数不同。可以写 scale trend（扩展趋势），但不足以像 Aster 一样给出笼统 scalability 等级。

### 7.5 Compaction retention

不能直接塞进 L0 layout 表。`naive/schema/b64/semantic/oracle` 是 L0 layout 或参考点；`naive merge/semantic merge` 是 compaction policy。把两者放进同一星级行会混淆正交维度。

## 8. Compaction 如需单独表述

如果后续要把 Figure 3 压缩成表格，应单独比较 merge policy：

| Policy | Retention（SF1/SF10c/SF30） | Write amp（SF1/SF10c/SF30） | Read consequence（SF1/SF10c/SF30） |
|---|---|---|---|
| naive merge | 0 / 0 / 0 | 1.22 / 1.14 / 1.07 | 4x / 6x / 6.52x proxy |
| semantic merge | 1 / 1 / 1 | 1.87 / 1.85 / 1.24 | 1x / 1x / 1.00x proxy |

SF30 的 read consequence 必须继续标为 metadata replay proxy，不得改写为完整 body-read speedup。

## 9. 可用于主稿的 LaTeX 草案

下面的版本使用真实数值，不依赖星形字符，适合作为当前 Table 1 的替代候选：

```latex
\begin{table*}[t]
\centering
\scriptsize
\caption{SemL0 design points. Candidate admission and RSS are measured at
SF100; L0 files are measured at SF10. Oracle is a post-import in-memory upper
bound, not a durable layout.}
\label{tab:design-space}
\begin{tabular}{lccccc}
\toprule
 & Query-blind LSM & Schema & budg-b64 & Full semantic & Oracle \\
\midrule
Candidate L0 (SF100) & 49.26M & 5.93M & 6.02M & 7.78M & 0.53M \\
Peak RSS GiB (SF100) & 2.43/2.49 & 2.42 & \textbf{2.21} & \textbf{118.03} & 2.67 \\
L0 files (SF10) & 170 & 376 & 444 & 737 & 170 \\
Degree precision & none & Mixed & budgeted exact & all groups considered & exact index \\
Hard file budget & no & no & $B=64$ & no & n/a \\
Feedback input & no & no & supported & no & n/a \\
Durable layout & yes & yes & yes & yes & no \\
\bottomrule
\end{tabular}
\end{table*}
```

在六页限制下，建议用这张表替换现有 Table 1，不要同时保留两张高度重叠的 variant/resource 表。替换后还需要重新编译确认仍为六页，并检查列宽是否出现 overfull。

## 10. 数据来源

- Aster 参考论文：`C:/Users/Coword/Desktop/Aster Enhancing LSM-structures for Scalable Graph Database.pdf`，Table 1 及其前后定义。
- SF100 实测汇总：`baseline/sf100-matrix-20260613-cn.md`。
- SF10 实测汇总：`baseline/w6-sf10-priority-20260709-summary.md`。
- Figure 2 canonical 数据：`baseline/seml0-lifecycle-paper/cidr/scripts/plot_fig2_sf100_read_budget.py`。
- W7 反馈证据：`baseline/w7-sf30-workload-shift-summary-20260615-cn.md`。
- C2 受控结果：`baseline/path-b-c2/stage4-smoke-summary-20260618-cn.md` 和 `stage5-scale-summary-20260618-cn.md`。
- C2 SF30：`baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md` 和 `stage7-sf30-readamp-proxy-summary-20260622-cn.md`。

所有实测数值只来自 `DONE` artifact。该文件是表格候选与写作口径说明，目前没有修改六页英文主稿或中文逐段翻译版。
