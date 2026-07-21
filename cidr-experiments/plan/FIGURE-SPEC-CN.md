# SemL0 完整实验版：六组论文图精确规格

本文档只定义论文图的数据契约与 Matplotlib 呈现方式，不包含实验结果，也不预设任何配置一定获胜。图中所有性能点都必须先通过相同结果集的 count/hash digest；不支持的接口显示为 `N/A`，不得当作 0。

## 0. 全局绘图契约

### 0.1 画布与排版

- 双栏宽度常量：`DOUBLE_W = 7.05` inch；单栏宽度常量：`SINGLE_W = 3.38` inch。最终应从 CIDR LaTeX 模板核对 `\textwidth`/`\columnwidth`，若偏差超过 3%，只同比缩放宽高，不重新压缩 panel。
- 矢量输出：PDF 与 SVG；`pdf.fonttype = 42`、`ps.fonttype = 42`、`svg.fonttype = "none"`。
- 默认字体：`DejaVu Sans`；正文 7.5 pt，轴标签 7.2 pt，tick 6.6 pt，legend 6.6 pt，panel label 8 pt bold。线宽 1.25 pt，marker 3.8 pt，误差带 `alpha=0.12`。
- 使用 `layout="constrained"`，保存时不再叠加 `bbox_inches="tight"`，避免最终宽度漂移。所有图在 100% PDF 缩放下检查轴标题、最长系统名和 log tick 是否被裁切。
- panel label 放在轴内左上角 `(0.01, 0.98)`，不使用负坐标，避免 `constrained_layout` 穿模。
- grid 只画主 y-grid：`#D1D5DB`、0.45 pt；不画上/右 spine。legend 优先直接标线；必须使用 legend 时放在图内空白处，最多两行。

### 0.2 统计与数值规则

- 所有正式点先做 3 个独立进程 run；QPS CV >3% 或 P99 CV >5% 时补到 5 个；长时动态、导入和 compaction 至少 3 个独立 run。图上显示全部独立 run 点与 run-level median；n=3 报告范围，n>=5 再使用 run-level bootstrap 95% CI。
- P50/P95/P99 先在每个 run 内计算，再跨 run 聚合；禁止把多个 run 的所有请求池化后重算分位数。
- 比率必须由 matched run 或相同实验批次的中心值计算，并在图注说明分母。所有绝对值保留在数据文件中。
- log 轴只接受严格正值。缺测、超时和 unsupported 使用空值及原因字段，不得填 0 或任意 epsilon。
- 性能图只纳入 `digest_pass=true AND mismatch_count=0` 的 run；失败 run 单独进入 correctness 表，不静默删除。
- 单位在原始数据中统一保存为 bytes、ns、seconds；绘图时再转换为 MiB/GiB、us/ms。

### 0.3 稳定颜色与线型

采用色盲友好的 Okabe-Ito 系列，并始终用 marker/line style 冗余编码。

| 身份 | 颜色 | 线型 | marker / hatch | 用途 |
|---|---:|---|---|---|
| `naive` / `none` | `#6B7280` | `--` | `o` 空心 / `///` | 语义关闭或诊断配置 |
| `schema` | `#0072B2` | `-.` | `^` | label/epoch coarse baseline |
| `budg-b64` / 默认 SemL0 | `#56B4E9` | `-` | `s` | 论文默认预算配置 |
| `semantic` / full | `#009E73` | `-` | `D` | 无预算 full semantic stress point |
| `oracle` | `#CC79A7` | `:` | `*` | 只用于 upper bound，不进入可部署排名 |
| `capacity-naive` | `#D55E00` | `--` | `X` | 不保留 pruning surface 的 compaction |
| `semantic-static` | `#009E73` | `-` | `D` | 固定语义 compaction |
| `semantic-feedback` | `#CC79A7` | `-` | `P` | feedback compaction |

外部系统在 Figure 1 中使用固定 marker，颜色仅用于区分系统：LiveGraph `#E69F00/o`、Aster `#D55E00/^`、TuGraph `#CC79A7/s`、NebulaGraph `#0072B2/P`、Neo4j `#6B7280/X`。SemL0 默认配置始终为 `#56B4E9/D`。不得仅靠颜色表达身份。

---

## Figure 1：Matched end-to-end comparison

**输出 stem**：`fig_e2e_matched`  
**主版式**：双栏，`figsize=(7.05, 2.55)`，`GridSpec(1, 3, width_ratios=[1.35, 0.85, 1.05])`。单栏版本必须拆成 `fig_e2e_latency` `(3.38, 2.65)` 与 `fig_e2e_cost` `(3.38, 2.35)`，不能把三 panel 硬压入单栏。

### Panel (a)：端到端 latency range

- **x**：latency，单位 us，log10。
- **y**：系统，固定顺序：SemL0、SemL0-naive、LiveGraph、Aster RocksGraph、TuGraph、NebulaGraph、Neo4j；不按本次结果排序。
- **marks**：每个系统一条细 horizontal range `P50 -> P99`；P50 圆点、P95 三角、P99 菱形。每个 quantile 带跨 run 95% CI 横向误差线。
- **series**：系统身份使用全局颜色/marker；分位数身份用 marker 形状，避免再引入颜色。
- **annotation**：轴内写 `SF10, same trace, C=<n>, digest pass`；若某系统缺 P99，位置显示灰色 `P99 N/A`，不画到原点。只标注 SemL0 相对最强主要基线的 P99 比率及 CI。
- **scale/ticks**：`set_xscale("log")`；tick 使用 `100 us, 1 ms, 10 ms, 100 ms, 1 s, 10 s` 的格式化标签。

### Panel (b)：固定并发下吞吐

- **x**：系统，沿用 panel (a) 顺序，标签旋转 32°、右对齐。
- **y**：completed QPS，log10；起点不能伪装为 0。
- **marks**：bar + 95% CI；超时请求不计入 completed QPS，并在 bar 上方标 `timeout=<rate>%`。
- **annotation**：一条浅灰水平线标 offered load（仅 open-loop 实验）；closed-loop 时不画 offered-load 线。

### Panel (c)：导入与磁盘代价

- **x**：最终数据库 disk footprint，GiB，log10。
- **y**：load/build wall time，seconds，log10。
- **marks**：每个系统一个等面积散点；load time 使用跨 run 95% CI，disk 若只有单次测量则只画点且在 caption 声明。
- **annotation**：直接标系统短名；不使用 bubble size 编码第三指标。右下角箭头 `lower is better`。

### Caption claim

安全模板：**“On the matched SF10 outgoing typed-neighbor interface, all plotted runs pass the same digest gate. The figure compares end-to-end latency, completed throughput, and load/storage cost; it does not rank complete graph-database functionality.”** 只有当 CI 支持时，才在正文添加具体倍数；不得写“SemL0 全面优于现有图数据库”。

### 所需数据字段

见 TSV 的 `F1`：系统/版本、run、query trace/hash、并发、P50/P95/P99、completed/timeout、measurement time、load time、disk bytes、digest/mismatch。CPU/RSS 可进入配套表，不强塞进本图。

### 潜在误读与穿模风险

- 外部系统接口功能不同：caption 必须重复限定 matched outgoing typed-neighbor。
- closed-loop QPS 与 latency 近似互为倒数，不能将两 panel 描述为两份独立证据。
- 系统跨度可达多个数量级，线性轴会压平快系统；log 轴必须明确标单位。
- 长系统名、P99 N/A 和 CI 易越过左/右边界；必须以最终 PDF 字体尺寸检查。
- 不得恢复旧脚本中口径不一致的 SF100 `LSM-style*` 点到 SF10 图中。

---

## Figure 2：Query-control component ablation

**输出 stem**：`fig_component_ablation`  
**主版式**：双栏，`figsize=(7.05, 3.75)`，`GridSpec(2, 2)`。单栏拆为性能 `(3.38, 3.55)` 与 CPU 分解 `(3.38, 2.25)`。

**共同 x**：有序配置 `A0..A6`：A0 naive；A1 + exact label/type evidence（routing off）；A2 + semantic routing；A3 + budgeted degree；A4 + feedback priority；A5 + semantic compaction；A6 full SemL0。x 标签使用 `A0` 等短名，完整定义放在 caption，避免斜长标签穿模。

### Panel (a)：tail latency

- **y**：P99 latency，us，log10。
- **marks**：单条深蓝折线，marker + 95% CI；A0 灰色、A6 绿色，其余浅蓝。折线只表达配置顺序，不暗示理论单调性。
- **annotation**：每个相邻阶段只在变化超过 CI 且绝对变化超过 10% 时标 `-x%`/`+x%`；回退点必须保留正号，不隐藏 regression。

### Panel (b)：候选与 body read

- **y**：segments/op，log10。
- **series**：candidates/op `#0072B2/o/-`；body reads/op `#D55E00/s/--`。
- **annotation**：若两线重合，在线尾直接标名并错开 4 pt；不得用透明重叠制造一条线的错觉。

### Panel (c)：物理读取

- **y**：physical read bytes/op，MiB，log10。
- **marks**：bar + 95% CI；每个 bar 上方可标相对 A0 的 `x×`，不标超过两位有效数字。
- **annotation**：若 page-cache 命中使设备读与逻辑 body bytes 不同，图中采用 `body_read_bytes/op`，caption 同时说明 `device_read_bytes` 在补充表中。

### Panel (d)：CPU/op 分解

- **y**：CPU us/op，线性，从 0 起。
- **marks**：stacked bar，依次为 signature build、metadata admission、routing/index、body decode/filter、MVCC/result；bar 顶端是 total CPU/op 的跨 run 95% CI。
- **颜色**：各 phase 使用同一蓝灰色系，由浅到深；body decode/filter 用灰色，避免与 variant 身份颜色冲突。
- **annotation**：bar 顶只标总 CPU/op；不在细小 stack 内强塞文本。

### Caption claim

安全模板：**“The staircase isolates the marginal effect and CPU cost of each enabled control-plane component under the same data and query trace. Non-monotonic steps are retained because finer partitioning can trade fewer bytes for more candidate files.”** 只有 A0--A6 真正由独立开关隔离时才可称为 causality/ablation。

### 所需数据字段

见 TSV 的 `F2`：stage、feature switches、latency、candidates、body reads/bytes、各 CPU phase、文件数、digest。资源与 compaction 字段保留用于解释异常，但不全部画进本图。

### 潜在误读与穿模风险

- 阶梯配置若同时改变两个机制，不能把相邻差异归因于单一组件。
- 连接线可能被读成预算连续曲线；caption 必须写 ordered categorical configurations。
- stacked CPU 只能加和 mutually exclusive CPU phases；wall time、I/O wait 不能混入 CPU stack。
- log 轴遇到 0 body read 时显示 `< detection limit` 或空值，不填 epsilon。
- A0--A6 标签、相邻百分比和 CI 容易相撞；最多标显著且大于 10% 的三处变化。

---

## Figure 3：Budget/resource Pareto

**输出 stem**：`fig_budget_resource_pareto`  
**主版式**：双栏，`figsize=(7.05, 2.70)`，`GridSpec(1, 3, width_ratios=[1, 1, 1.15])`。单栏拆为 Pareto `(3.38, 3.25)` 与 breakdown `(3.38, 2.35)`。

**配置顺序**：schema/no-degree、B16、B64、B256、B1024、full semantic。预算值用 point label，不用六种颜色。

### Panel (a)：performance--memory Pareto

- **x**：steady-state RSS，GiB，log10；import peak RSS 不得代替此轴。
- **y**：P99 latency，us，log10。
- **series**：uniform `#0072B2/o/-`、Zipf/hot `#D55E00/s/--`、A→B shift `#CC79A7/^/-.`。同一 workload 内按预算顺序连线并加小箭头。
- **annotation**：只标 `B64` 与 `full`；右上到左下箭头 `lower is better`。根据实际数据计算非支配点，用 1.8 pt 深色外圈标出；不得事先指定 B64 为 knee。

### Panel (b)：performance--semantic-disk Pareto

- **x**：control-plane persistent bytes/edge，线性，从 0 起；只含 metadata/catalog/sidecar/manifest 的增量，不含 payload。
- **y**：body read bytes/op，MiB，log10。
- **series/annotation**：与 panel (a) 相同；非支配点独立计算。若 workload 不影响静态磁盘值，可共享 x，但不能复制伪造独立测量。

### Panel (c)：持久化资源分解

- **x**：预算配置。
- **y**：control-plane bytes/edge，线性，从 0 起。
- **marks**：stacked bar：segment metadata、semantic index/catalog、degree sidecar、manifest、WAL extra。payload 用一条浅灰 reference line/文字报告，不能堆在同一 bar 中压扁增量。
- **annotation**：bar 顶标 `files=<n>`；正文/补充表报告 temporary peak disk、load/build/reopen time。文件数只作为文字，不开双 y 轴。

### Caption claim

安全模板：**“Budgeted configurations expose the measured latency/read-amplification versus memory and persistent-metadata tradeoff. Outlined points are empirically non-dominated; the figure does not assume that a larger budget is monotonically better.”** 若 B64 不是非支配点，caption 不得称其为 sweet spot。

### 所需数据字段

见 TSV 的 `F3`：budget、workload distribution、phase、steady/peak RSS、P99、body/device read bytes、分项磁盘、file count、load/build/reopen、CPU 与 write amplification。

### 潜在误读与穿模风险

- `full semantic` 的 118 GiB import cliff 与 steady RSS 是不同阶段，必须分字段、分文字说明。
- 总数据库大小会掩盖 semantic metadata；本图画增量 bytes/edge，另表给 total GiB。
- bubble size 不用于文件数，避免面积感知误差。
- Pareto 连线只表示有序预算实验，不表示插值可实现。
- point label 在 log-log 图易重叠；使用固定 4--6 pt 偏移和 `adjustText` 等价的确定性布局，最终固定坐标写入脚本。

---

## Figure 4：Dynamic compaction timeline

**输出 stem**：`fig_dynamic_compaction_timeline`  
**主版式**：双栏，`figsize=(7.05, 5.10)`，`GridSpec(4, 2, hspace=0.10, wspace=0.22)`，所有轴共享时间 x。单栏不得缩成八 panel；应拆成 performance/lifecycle 与 resource 两张 `(3.38, 4.8)` 图。

**共同 x**：elapsed minutes，线性。所有 run 使用固定 workload phase 边界；背景仅用极浅灰区分 steady、burst、A→B shift，边界处标一次文字。每 30 s 一个聚合 window；中心线是跨 run median，带为 95% CI。

**共同 series**：`none` 灰色点线、`capacity-naive` 橙色虚线、`semantic-static` 绿色实线、`semantic-feedback` 紫色实线加 P marker。`none` 只作 L0 accumulation 诊断，不进入 steady-state 胜负结论。

### 八个 panel

| Panel | y | scale | 关键呈现 |
|---|---|---|---|
| (a) | read P99 latency (us) | log10 | 固定 offered load；只在 phase 末端标 policy 比率 |
| (b) | achieved read QPS | linear, 0 起 | 黑色细点线标 offered QPS；差值代表 backlog/timeout |
| (c) | writer throughput (ops/s) | linear, 0 起 | 与固定 update trace 对齐；不得用 read QPS 替代 |
| (d) | candidate bytes/op (MiB) | log10 | 用 full query execution 计数；proxy 另用空心 marker |
| (e) | live L0 file count | linear step | `drawstyle="steps-post"`；flush/compaction 造成阶跃 |
| (f) | process CPU / allocated quota (%) | linear 0--100 | 使用 quota 归一化，禁止出现“800% CPU”而不解释 |
| (g) | RSS (GiB) | linear | 标 peak，swap-in/out 另表报告 |
| (h) | cumulative compaction output bytes (GiB) | linear step | 终点标 compaction count 与 write amplification |

compaction 事件不在八个 panel 全部画竖线；只在 (e)/(h) 顶部用 2 pt rug，避免黑栅栏。若不同 run 的 compaction 时间不对齐，rug 只展示明确标记的 representative run；主线仍是跨 run window 聚合。

### Caption claim

安全模板：**“Under the synchronized SF30 mixed trace, semantic compaction policies retain lower candidate-byte pressure over time while exposing their writer, CPU, memory, and rewrite costs. Checkpoints are time windows, not independent repetitions; the no-compaction row is diagnostic rather than a steady-state baseline.”** 只有执行真实 body reads 后才写 read-I/O claim。

### 所需数据字段

见 TSV 的 `F4`：run/window、workload phase、offered/achieved QPS、read P99、writer throughput、candidate/body/device bytes、L0 files、CPU quota、RSS/swap、compaction count/input/output、WA、mismatch。

### 潜在误读与穿模风险

- 当前旧 W9 若 `COMPACT_EVERY_SECS=0`，不能进入 semantic-compaction 因果图，只能作为 `none` 诊断。
- checkpoint 是相关时间点，不能当作六次重复计算 CI。
- workload phase、compaction 和 latency 同时变化时只能表述相关性；需要 controlled trace 与 policy 对照支持归因。
- 八 panel legend 只出现一次；每个 panel 重复 legend 会吞掉数据区。
- CI band、四条 policy 线和 phase shading 可能混色；phase alpha 不超过 0.045，CI alpha 不超过 0.12。
- 不允许插值跨越缺失 window；line 必须断开。

---

## Figure 5：Workload coverage matrix

**输出 stem**：`fig_workload_coverage`  
**主版式**：双栏，`figsize=(7.05, 4.45)`，顶部两个 heatmap，底部两个 line/bar panel，`GridSpec(2, 2, height_ratios=[1.35, 1])`。单栏拆为 heatmap `(3.38, 5.0)` 与 update/fallback `(3.38, 3.4)`。

### Panel (a)：P99 speedup heatmap

- **rows**：typed-only、destination-label、record-time、property-present、property-absence、two-hop。
- **columns**：`Rare-L/M/H`、`Medium-L/M/H`、`Frequent-L/M/H`，即 edge selectivity × source degree 的 9 个组合。
- **cell value**：`log2(P99_naive / P99_b64)`；`TwoSlopeNorm(vmin=-2, vcenter=0, vmax=4)`；colormap `PuOr`，负值橙色表示 regression，正值紫色表示 improvement。
- **annotation**：每格显示一位小数的 `x×`，而非 log2 数字；超出色阶用三角 cap。unsupported 用浅灰 `////`，缺数据用白底 `xx`，二者不得混淆。

### Panel (b)：I/O reduction heatmap

- **rows/columns**：与 (a) 完全共享并锁定顺序。
- **cell value**：`log2(read_bytes_naive / read_bytes_b64)`；与 (a) 共享 norm 与 colorbar，便于比较收益方向。
- **annotation**：同 panel (a)；若某查询由 page cache 命中但 body bytes 非零，仍以逻辑 body read bytes 计算。

### Panel (c)：read/write mix 与 update pattern

- **x**：write percentage：0、10、50、90，线性分类轴，标签 `100/0`、`90/10`、`50/50`、`10/90`。
- **y**：P99 speedup `P99_naive/P99_b64`，log2；画 `1×` 水平 reference。
- **series**：uniform `#0072B2/o/-`、burst `#D55E00/s/--`、hotspot `#E69F00/^/-.`、A→B shift `#CC79A7/P/-`，带 95% CI。
- **annotation**：只标落到 `<1×` 的 regression 和最大收益点；不能只标正结果。

### Panel (d)：安全 fallback 的读取代价

- **x**：stable、schema epoch、alias/drop、high tombstone、old snapshot、reopen/rebuild。
- **y**：fallback extra read ratio，`read_bytes_fallback/read_bytes_exact`，log2，以 `1×` 为参考。
- **marks**：bar + CI；bar 顶同时标 `fallback=<rate>%`。仅当 mismatch=0 时着正常颜色；出现 mismatch 时该点变红叉且整组性能结论判失败。

### Caption claim

安全模板：**“The matrix exposes where SemL0 benefits, regresses, or is unsupported across semantic selectivity, degree, update mix, and fallback states; unsupported cells are not averaged into the reported speedups.”** 只有实现等价 optimized path 后才将 `In/Both` 或通用 property equality 加入 rows。

### 所需数据字段

见 TSV 的 `F5`：semantic class、selectivity、degree、read/write ratio、update pattern、schema/version state、support state、P99、read bytes、fallback rate、mismatch/digest。

### 潜在误读与穿模风险

- speedup heatmap 会隐藏绝对慢值；正文或补充表必须同时给 naive 与 SemL0 的绝对 P99/read bytes。
- 6×9 单元格已经是上限；不得继续把 schema、snapshot 维度塞入列，应由 panel (d) 承担。
- property equality 属于独立 prototype 时必须在 row label 加 `prototype` 脚注。
- two-hop 若由多个 one-hop 调用拼接，需报告整条请求路径，不能只画 storage-kernel 子调用。
- heatmap 数字颜色需按背景亮度切换黑/白，打印灰度版也应可读。

---

## Figure 6：Data-size and concurrency scaling

**输出 stem**：`fig_scaling_concurrency`  
**主版式**：双栏，`figsize=(7.05, 4.55)`，`GridSpec(3, 2)`。单栏拆为 data scaling `(3.38, 4.8)` 和 concurrency `(3.38, 2.45)`。

**共同 series**：naive、schema、budg-b64、semantic，严格使用全局颜色/线型/marker；所有 panel 共享一个顶部 legend。

### Data-size scaling panels

实际 x 使用 `directed_edges`，不是字符串 SF；log10。tick label 可写成 `SF10\n360M edges`。若采用严格 2× 数据点，可把 x 改为 log2，但仍使用实际 edge count。

| Panel | y | scale | annotation |
|---|---|---|---|
| (a) | P99 latency (us) | log10 | 标 b64 相对 naive 的末端比率；CI band |
| (b) | body read bytes/op (MiB) | log10 | 不用 metadata proxy 代替真实执行；必要时 proxy 用空心 marker |
| (c) | final database disk (GiB) | log10 | payload 与 control-plane breakdown 在 Figure 3；此处画 total |
| (d) | steady-state RSS (GiB) | log10 | 固定 cache state；import peak 不进入该轴 |

只有至少 4 个规模点且 log-log 拟合 `R² >= 0.9` 时，才在 (a)/(b) 标注经验斜率 `N^alpha`；否则只连测量点，不报告复杂度。

### Concurrency panels

- **共同 x**：clients/threads：1、4、8、16、32，log2；所有 variant 使用相同 cpuset/CPU quota。
- **Panel (e) y**：completed QPS，线性从 0 起；为 b64 画一条从 C=1 外推的黑色点状 ideal-linear reference，不为所有 variant 重复理想线。
- **Panel (f) y**：P99 latency，us，log10；超时点在图顶用向上三角并标 timeout rate，不把 timeout 当作固定最大延迟。
- **annotation**：当相邻并发 QPS 增益 `<10%` 且 P99 已超过 C=1 的 2× 时，标一次 empirical saturation；该阈值必须在绘图脚本中固定。

### Caption claim

安全模板：**“Across the measured single-node data sizes and CPU-matched client counts, the figure reports absolute performance and resource growth for each configuration. The concurrency panels characterize single-host saturation, not distributed scale-out.”** 只有完整四点以上才能称 scaling curve。

### 所需数据字段

见 TSV 的 `F6`：actual vertices/edges/properties、scale、variant、concurrency、cpuset/quota、cache state、P99/QPS/read bytes、disk、steady RSS、timeout、digest。

### 潜在误读与穿模风险

- 现有 SF1 synthetic、SF30 real、SF100 read-only 不能拼成一条曲线；必须使用相同生成器、sampling 和 workload。
- SF 值不一定是精确 2× edge count，x 必须取实际 edges。
- 数据放大导致 cache working-set 变化，应分别画 cold/warm 或固定 cache budget，不能混合平均。
- semantic full 发生 OOM 时保留为 censored/OOM 标记并报告资源上限，不能删除该点后声称曲线稳定。
- concurrency 的 closed-loop 与 open-loop 结果不可共线；图中必须固定一种协议。
- log-x 的 1/4/8/16/32 tick 容易被 Matplotlib 自动插入无意义 minor labels，应关闭 minor tick labels。

---

## 7. 图注与结果冻结检查表

每张图进入论文前必须满足：

1. 图中系统、variant、数据集、并发、cache state 能由 TSV/原始结果唯一还原。
2. 所有点有独立 run 数；CI 不是 query-level 伪重复，也不是 checkpoint-level 伪重复。
3. 绝对值、比率分母、单位与 log base 明确；N/A、timeout、OOM 与 0 明确区分。
4. caption 只陈述图直接支持的 claim，并保留 matched-interface、single-node、prototype 等边界。
5. 颜色、marker、线型在六张图中身份稳定；黑白打印仍能区分。
6. 用最终 LaTeX 宽度渲染 PDF，检查 legend、长标签、CI、annotation、heatmap 数字无裁切和重叠。
7. 绘图脚本只读取冻结的 tidy data，不内嵌手抄常量；数据与脚本都记录 SHA-256。
