# CIDR 正式实验进度表

更新时间：2026-07-22 04:42 CST
正式验收真源：`plan/ARTICLE-EXPERIMENT-OUTLINE-CN.md`。大纲决定必须覆盖的 RQ、指标和正确性边界；本文件第 3 节冻结七天内的代表性配置。用户于 2026-07-21 提供的最新 ZIP 用于核对当前稿已有内容与缺口，其 SHA-256、逐项清单和 claim 审计见 `work/latest-paper-audit/`。

## 0. 最新执行看板

当前结论：**前置工程和 correctness 已取得可验收结果，但正式 timing/resource 数据点仍为 0；当前论文性能图仍不能升级为正式图。** 正式性能任务必须等 clean-window gate 与 SF10 sentinel 通过后再串行启动。

| 工作包 | 状态 | 已完成/已验证 | 尚缺或下一步 |
|---|---|---|---|
| P00 论文与旧证据审计 | `DONE` | 20 个来源完成 `REUSE=5 / FIX=6 / RERUN=9` 分类；六图和 RQ 证据边界已冻结 | 后续正式数据回填后再做 claim-to-evidence 终审 |
| S0 Linux 同步、SHA、commit | `PASS_INITIAL` | Windows/Linux `145/145` payload SHA 一致，目录总文件数 146；初始同步包提交为 `acb167e`；已建立 clean 集成分支 | 当前集成 HEAD `3965a661f3f0`；外部 store gate 收口后做最终全量 SHA、反向同步和封板 commit |
| P01 shared truth / ID bridge | `PASS_INTEGRATED` | 完整扫描 `355,185,382` edges、`29,987,835` vertices；双向 ID map、lockstep、bijection、输入/输出 SHA 全 PASS；独立执行 `sha256sum -c SHA256SUMS` 3/3 OK；历史 converter lineage 已固化 | 集成态 8 Python + 6 Rust shared-truth tests 均 PASS |
| P02A correctness | `PASS` | W13 `10/10`；W6 SF1 九变体的 8 组 compare 均 `checked=180, mismatches=0` | 只作 correctness 证据，不能引用该轮 timing |
| P02B SF10 sentinel | `PREPARED_BLOCKED_LOAD` | fail-closed runner/consumer 已集成；14/14 tests + 3-repeat fake P31 smoke PASS；dataset、四套 SemL0 store、plan/preflight 的 `SHA256SUMS` 7/7 PASS；四 store 各 `1700/1700, mismatches=0` | 只等 P03 READY；清场后放行链预计 23--35 min |
| P20 A0--A6 组件消融 | `PASS_INTEGRATED_BLOCKED_LOAD` | A0--A6 core、formal runner、current-binary correctness generator、run-level Figure 2 aggregator 与 canonical P02B admission 已合入；Python 40/40（另 1 项按设计条件运行）、Rust 8/8、real-P31 fake smoke、cargo check 均 PASS | 正式 A0--A6 只等 P02B formal PASS 与 clean window |
| P31 资源采集与并发规则 | `PASS_INTEGRATED` | collector/validator、11 项单测、fixture smoke、11/11 双端 SHA 均 PASS；已接入 P02B/P20/P40/P10；正式实验单任务串行 | clean window 中验证真实长任务采样数量与 cpuset 绑定 |
| P40 fixed trace | `PASS_CORRECTNESS_INTEGRATED` | clean-head run `P40-CLEAN-HEAD-36Q-20260721T181522Z-d7c283c5808f`：3×4 arms、36 queries、0 mismatch、62/62 SHA PASS | `performance_eligible=false`；正式 30 min×3 等 clean window |
| P10/P11 跨系统 orchestrator | `6_OF_6_ADAPTERS_INTEGRATED` | SemL0、LiveGraph、Aster、TuGraph、Neo4j、NebulaGraph 真实 adapter 已合入；主分支 P10 33/33 + TuGraph embedded 2/2，真实 tiny fixture 0 skip；生命周期/runtime/image SHA、P02B/P31 fail-closed 已交叉绑定 | Aster final worker 已冻结；LiveGraph final build 中；TuGraph 旧 4.0 store 已被 4.5.2 gate 拒绝，正重建 4.5.2 SF10 store；随后补 Aster/Neo4j/Nebula isolated SF10 store gate |
| P03 clean-window monitor | `BLOCKED_LOAD` | monitor 持续运行；03:57 样本 load1=1.17、CPU idle=99.199%、磁盘 util/await=0/0、`/data` 可用 925.774 GiB | zcl 119.52 GiB worker、4 个 rsync（其中 2 个暂停、2 个活动）、GPStore、TuGraph；MemAvailable 355.003 GiB，未达 400 GiB；清场后还需连续 15 个 60 s 样本 |
| P10/P11/P20/P31/P40/P50/P60 正式性能 | `NOT_STARTED` | 前置 runner/contract 按优先级并行准备 | 清场后先连续观察 15 min，再跑 P02B sentinel；通过后正式任务严格串行 |

进度口径：上表的 `PASS` 表示对应工程或正确性 gate 已验收，不代表论文性能数据已经完成。当前 **正式性能数据点完成数为 0**。按五个外部系统历史单次耗时复核后，E01 从 12 h 修正为 18--24 h；当前计划为 `132--138 h` 主路径 + `30--36 h` 风险缓冲，硬上限仍为 `168 h`。已在 T0 前完成的工程会直接形成实际余量。

## 1. 当前状态

| 项目 | 当前状态 | 结论/下一步 |
|---|---|---|
| 最新论文与旧证据审计 | `DONE` | 20 个来源已分为 REUSE=5、FIX=6、RERUN=9；当前图仍是 provisional evidence |
| W6/RQ3/W13 parser 与来源指针修复 | `DONE` | 可复用 raw 已规范化；不能把历史单次/不同硬件数据升级为正式性能点 |
| Linux 目录同步、文件数与 SHA-256 校验、commit | `PASS` | 最终 145/145 payload SHA、146 个总文件已双端校验；本文件所在提交 SHA 以远端 `git log -1` 为准 |
| 当前可做的 correctness-only smoke | `PASS` | W13 已 10/10 PASS；W6 SF1 九变体 neighbor-compare 已全部 PASS，二者均为 `performance_eligible=false` |
| 正式 timing/resource 实验 | `BLOCKED_LOAD` | `zcl` 大内存任务+rsync、GPStore/TuGraph 常驻服务未释放；并行完成 runner/collector 工程，不启动正式 timing |

远端快照（2026-07-22 03:57 CST）：load1=`1.17`、CPU idle=`99.199%`、MemAvailable=`355.003 GiB`、`/data` 可用=`925.774 GiB`、NVMe util/await=`0/0`。瞬时 CPU 与磁盘已较空闲，但 zcl 的 119.52 GiB worker、4 个 rsync 以及 GPStore/TuGraph 仍在；共享内存、page cache、NUMA 和服务干扰不能排除，所以当前 timing 数据不具备论文资格。

执行恢复点（2026-07-22 00:07--04:42 CST）：W13 与 W6 SF1 correctness-only、P01 全量 ID map、P31 collector、P20 runner/admission、P40 fixed trace 均已验收；SemL0 四 store 和六系统 P10 adapter 已完成 correctness/contract 回归。Aster clean-tree worker 已冻结，TuGraph 4.5.2 SF10 store 正在 correctness-only 重建。正式性能仍等待 clean-window gate。

当前 clean-window 阻塞（2026-07-22 03:57 CST）：`zcl` 的大内存 worker 占 119.52 GiB；SF1 rsync 两进程已暂停约 38.6 h，SF300 rsync 两进程按当前速率仍可能需要 2--3 天；正式测量前还需由维护者停止 GPStore 与 TuGraph。全部释放后要求连续 15 个 60 s 样本满足 load <5、CPU idle >95%、MemAvailable >=400 GiB、`/data` util <5%、await <5 ms，再执行 QPS CV <=3%、P99 CV <=5% 的 SF10 sentinel。

### P02A correctness-only 实时结果

| 子任务 | 状态 | Run ID | 验收 |
|---|---|---|---|
| W13 schema evolution | `PASS` | `P02-W13-CORRECTNESS-20260721T160741Z-acb167eba8fd` | exit 0；DONE；10/10 pass；49 artifacts SHA-256 全部 OK；manifest `77c9b3c1...9fb8e` |
| W6 SF1 九变体 compare | `PASS` | `P02-W6-SF1-CORRECTNESS-20260721T161132Z-acb167eba8fd` | 8 个 compare 均 checked=180、mismatches=0；76 artifacts SHA-256 全部 OK；仅 correctness，不采信 timing |

### P02B / SemL0 SF10 frozen store 证据

四套 store 均通过同一 1,700-query truth：`checked=1700`、`mismatches=0`、`total_neighbors=84,104,814`。`/data/WorkSpace/results/P02B/SHA256SUMS` 为 7/7 PASS；清单 SHA-256 为 `9bb5270cb2cc9a4eefee4865c1929deb65d565eac9e92d69c4e026a54c0d3c35`。

| store | files | logical bytes | frozen tree SHA-256 |
|---|---:|---:|---|
| naive | 173 | 14,399,231,973 | `133e2ab535dd2c915d93e6e5ded65295151e199ec7268609f0a3e2f65387ddd2` |
| schema | 379 | 14,306,672,496 | `b7217d6d839d11255c7e7c9c4e6c317469da650091eaefa453f3b5908019348e` |
| budg-b64 | 448 | 14,312,271,718 | `ae77255c03c40d9d7e55071374ab3adc1dc67942f9443ad7d97dacacbe78c8b5` |
| semantic | 740 | 15,716,301,617 | `ae1d0021a759cef6d68b4c57a144fa0e6f5bdba813cb35f7235a82b40a3e72f6` |

构建证据位于 `/data/WorkSpace/results/P10-SEML0-STORES/P10-SEML0-SF10-STORE-REBUILD-20260721T193325Z-d123990/`，小文件 `SHA256SUMS` 11/11 PASS。该目录明确写入 `correctness_only=true`、`performance_eligible=false`、`formal_performance_points=0`；为避免在共享服务器重复约 58 GB I/O，本次独立复核重算了目录文件数和逻辑字节，但没有二次读取 store 内容重哈希，这一边界已写入 `DONE.correctness-only`。

### P10 六系统工程证据

| 系统 | 当前工程证据 | 剩余 formal 前置 |
|---|---|---|
| SemL0 | 四套 SF10 store 各 1700/0 mismatch；7/7 P02B SHA | fresh P02B PASS + clean window |
| LiveGraph | real API/lifecycle tests 7/7；fresh-import→warmup→measured 与 `liblivegraph.so` SHA 已绑定；final release build 正在冻结 | final binary manifest；正式 repeat 内 fresh import |
| Aster | clean source `6abb258e...`；final worker SHA `12f848aa5aa7595d8626e67066280cc6dd023067b597d123fdc6caa067a6c1b8`；tiny 7/7；build artifact 62/62 SHA | 新建或安全迁移带 key-map/metadata 的 SF10 reopen store，1700-query gate |
| TuGraph | final 4.5.2 worker SHA `8a845f078a3460c3a6120f511916b35a007bb9ee545a92fd4f9e83be41a3df88`；tiny 2/2 | 旧 4.0 store 与 4.5.2 不兼容且已 fail-closed；正在 fresh rebuild 4.5.2 SF10 store |
| Neo4j | `neo4j:5.26.24` RepoDigest `f66304b9...96435`；real Bolt 4/4；runtime evidence SHA256SUMS 已固化 | 对历史 SF10 store 做独立 clone、manifest、P31 correctness gate |
| NebulaGraph | v3.8.0 三镜像 digest 3/3 与历史一致；real nGQL 5/5；runtime manifest SHA `66497ff04cfdde7532efb7eb2a00fbbf1fb0f03c25967d02ea446cdc2aa787f6` | 对历史 44 GB store 做独立 clone、manifest、三容器 P31 correctness gate |

六系统最终主分支回归为 P10 `33/33` 加 TuGraph 独立 `2/2`，全部使用真实 tiny API 且 0 skip。上述 build/fixture/兼容性数字均为 `performance_eligible=false`；正式性能数据点仍为 0。

状态枚举：`DONE`、`PREPARING`、`READY`、`RUNNING`、`VALIDATING`、`PASS`、`FAIL`、`BLOCKED_LOAD`。进程退出码为 0 不等于 `PASS`；正式完成还必须有 raw、manifest、hash、正确性 gate 和规定的独立重复。

## 2. 执行通道

| 通道 | 当前是否可执行 | 允许执行 | 结果边界 |
|---|---:|---|---|
| `NOW_SAFE` | 是 | 同步、SHA-256、commit、parser、registry、truth/ID bridge、manifest、dry-run、非计时 correctness smoke | correctness-only；统一写 `performance_eligible=false`，不得引用 timing/CPU/RSS/I/O |
| `NEED_CLEAN_WINDOW` | 否 | sentinel 通过后的 import/query/dynamic/resource/跨系统正式 runs | 只有该通道的合格数据可进入性能图表 |

`P02` 必须拆成两部分：

- `P02A-SF1-CORRECTNESS-SMOKE`：`NOW_SAFE`。包括 W13 10-test 和 W6 SF1 9-variant neighbor-compare；保存 digest/mismatch/manifest，只判 PASS/FAIL。
- `P02B-CROSS-SYSTEM-SHARED-TRUTH-GATE`：`NEED_CLEAN_WINDOW`。跨系统同 truth/order/cache gate 及其资源记录要等服务器释放；它是 P10/P11 的正式放行条件。

进入 clean window 的最低条件：其他用户重负载已释放；P03 连续 15 个 60 s 样本无明显后台 I/O 且 host/load/memory 全部达标；当前任务磁盘水位满足；随后 sentinel 的 QPS CV ≤3%、P99 CV ≤5%。任一条件失败就回到 `BLOCKED_LOAD`。

## 3. 七天封顶执行计划（当前执行真源）

用户给定硬期限为 `168 h`。`T0` 定义为其他用户重负载释放且 clean-window sentinel PASS；等待释放的时间不计入运行预算。同步、hash、commit 和 correctness-only 准备可在 T0 前完成。正式执行采用“每个 RQ 都覆盖、每个选定点指标齐全、但不穷举所有组合”的方案；下文 385 h/592 h 仅保留为历史审计上界，不再作为本轮排程。

### 3.1 冻结协议

- 每个点先做 **3 个独立进程 run**；run-level QPS CV >3% 或 P99 CV >5% 时，才从 42 h 缓冲中补到 5 次。
- SF100 budget 固定六点：`naive/schema/B64/B256/B1024/semantic`。`kv-lsm/edge-type-only/oracle` 不在主结论中重复测量，只能保留为明确标注的历史 appendix 数据。
- scaling 固定 `naive/schema/B64/semantic`，SF1/SF10/SF30/SF100 复用 E04 已验证的 immutable stores，不重复 import。
- 所有系统统一 1,700-query truth、相同顺序、cache/warmup 和计时边界；旧 45,000-query SF100 流程不再重复执行。
- workload coverage 使用冻结的分层代表集覆盖 rare/medium/frequent、low/high degree、typed/property/two-hop、四种读写比和 stable/hotspot/shift，但不跑完整笛卡尔积。
- 数据不减项：每个正式 run 仍采 latency/QPS、CPU/op、RSS/PSS、磁盘分项、I/O、candidate/body read、compaction/WA（适用时）、digest、mismatch 和完整 provenance。
- E02 full LDBC E2E 保持条件项：只有 adapter 语义等价 gate 已通过时才使用缓冲执行；否则在论文中明确 `BLOCKED`，不以 storage-kernel 数字冒充。

### 3.2 旧数据与 store 复用

现有 20 个证据来源已经审计为 `REUSE=5 / FIX=6 / RERUN=9`。复用的单位是受限 claim、raw、truth 或 store，不是把旧 performance 图整张升级为正式图：

- 直接复用 W13 10/10 bounded correctness、C2 三层机制/retention/proxy 证据，以及 W6 SF100 layout/correctness facts；保留其 bounded、proxy 或 historical 边界。
- 重解析 W6 import resource、RQ3、W8 property/two-hop 和 W13 supplemental；它们用于 appendix、选点和 runner 校准，不重复旧 SF100 九点探索矩阵（旧矩阵约 30 h 53 min）。
- 共享 truth、dense/original ID bridge 和通过兼容性/hash gate 的 SF10/SF30/SF100 immutable base store 直接复用；只补缺失的正式 timing、telemetry、独立 repeats 与 matched protocol。
- 跨系统主性能、A0--A6 因果消融、fixed-trace dynamic、四点 scaling 和 concurrency 必须补跑，因为旧数据无法靠 parser 补出可比性。

### 3.3 132--138 h 计划 + 30--36 h 缓冲

| 顺序 | 大纲映射 | 执行内容 | 期望 wall time | 累计 | 主要复用/停止条件 |
|---:|---|---|---:|---:|---|
| 0 | E00/E09 前置 | 同步、双端 SHA、commit、manifest/truth/collector/feature-switch 工程、P02A correctness | **12 h** | 12 h | 工程多 agent 并行；correctness 失败立即停 timing |
| 1 | E00 | clean-window sentinel、setup 表和 P02B shared-truth gate | **2 h** | 14 h | sentinel 或 shared truth 不通过则保持 `BLOCKED_LOAD` |
| 2 | E01 | SemL0 `naive/schema/B64/semantic` + 五个外部系统，同 truth 的 3-run matched comparison | **18--24 h** | 32--38 h | 历史五系统三轮下限已约 11.3 h；另计 warmup、P31、SemL0 与切换；按 embedded/client-server 分组报告 |
| 3 | E03 + E04/SF10 | SF10 A0--A6×typed/degree/property；SF30 A0/A2/A4/A6 代表复验；六个 budget uniform 全阶段、四核心点补 Zipf/shift mixed/compaction | **14 h** | 46--52 h | 共用 import/store/collector，不重复构建 |
| 4 | E04/SF100 | 六个 budget 点，import×3、query×3 起跑、1,700-query truth、完整资源数据 | **40 h** | 86--92 h | variants 串行；高方差 query 才补到 5；空间硬水位 180 GiB |
| 5 | E05 | controlled proxy/full-read 校准、real SF30 C2、四 arms×30 min×3 dynamic | **14 h** | 100--106 h | 同 immutable base、fixed trace；四 arms 串行 |
| 6 | E06 | 分层代表 workload/property/two-hop/RW/shift cells，3-run | **8 h** | 108--114 h | 不做全笛卡尔积，但每个维度至少有正式证据 |
| 7 | E07/E08 | 四规模×四 variants，以及 1/4/8/16/32 concurrency | **10 h** | 118--124 h | 全部复用前序 stores；只运行 query/concurrency phase |
| 8 | E09 | 3 个固定 seed×至少 20,000 ops 的 differential safety、fallback rate/cost、失败 trace shrink | **5 h** | 123--129 h | false negative 必须为 0；失败时优先修正确性 |
| 9 | 全部 | normalize、统计/CI、缺字段检查、claim-to-evidence 审计和数据封板 | **9 h** | **132--138 h** | 任何缺 raw/hash/manifest 的点不得进入正式结果 |
| 10 | 缓冲 | 高方差补到 5 runs、runner 修复、失败重跑、可选 E02 | **30--36 h** | **168 h** | 不用于扩大 variant/cell 范围 |

### 3.4 七天内的取舍边界

七天计划不会删除任何 RQ，也不会少采资源或正确性字段；缩减的是重复的 baseline variants、完整 workload 笛卡尔积和所有点固定 5-run。因而论文可以声称“在预注册的代表性配置上覆盖 RQ1--RQ7”，不能声称穷举了所有 workload/预算组合。若某阶段超时，按以下顺序使用缓冲：correctness 修复 > 缺失字段补跑 > 高方差补重复 > E02；不得为了赶期限丢 digest、资源指标或把历史 provisional 数据升级为正式结果。

## 4. 历史全量 backlog（ARCHIVE / NOT SCHEDULED）

下面的 161/282/435 h 是**原始 22 项完整六图计划**的单机串行 wall-clock 包络，不是最新稿关键路径。阶段预算包含工程、运行、校验和合理重试；任务细节仍以 `work/remote-audit/RERUN-CANDIDATES.tsv` 为准。

| 阶段 | 任务 ID | 当前状态/通道 | 主要依赖 | 乐观 / 期望 / 悲观 | 新增磁盘或工作集峰值 | 核心产物 |
|---|---|---|---|---:|---:|---|
| E0. Provenance 与 correctness | `P00-W6-RESOURCE-REPARSE`、`P00-RQ3-RESUMMARIZE`、`P00-LEGACY-NORMALIZE`、`P00-W13-POINTER`、`P01-SHARED-TRUTH-BRIDGE`、`P02A/P02B` | P00 `DONE`；P01/P02A `NOW_SAFE`；P02B `BLOCKED_LOAD` | Linux 同步、统一 truth/hash | **8 / 15 / 25 h** | ≤20 GiB | registry、ID bridge、SF1/W13 correctness、跨系统 gate manifest |
| E1. End-to-end/baseline | `P10-G1-SEML0-MATCHED-SF10`、`P11-G1-EXTERNAL-SF10`、`P12-G1-LDBC-E2E` | `BLOCKED_LOAD` | E0 PASS；adapter 语义等价 | **45 / 85 / 135 h** | 130--300 GiB | 同 truth/order/cache 的 SemL0 与外部系统 5-run 结果、可选 LDBC E2E |
| E2. Component staircase | `P20-G2-STAIRCASE-SF10`、`P21-G2-STAIRCASE-SF30` | `BLOCKED_LOAD` | 正交 feature switches；E0 | **16 / 30 / 50 h** | 80--180 GiB | A0--A6 单开关消融、SF30 关键点复验、digest/resources |
| E3. Resource Pareto | `P30-G3-IMPORT-PARETO-PROVISIONAL`、`P31-G3-FULL-RESOURCE-SF10` | P30 `NOW_SAFE`；P31 `BLOCKED_LOAD` | PID/PSS/pidstat/disk collector | **8 / 15 / 25 h** | 90--180 GiB | import-only 历史诊断；SF10 full lifecycle CPU/PSS/disk/metadata 指标 |
| E4. Dynamic compaction | `P40-G4-FIXED-TRACE-RQ3`、`P41-G4-C2-FULL-READ` | `BLOCKED_LOAD` | fixed trace；clean SF30 base；统一 query | **18 / 32 / 50 h** | 120--260 GiB | 四 arms×30 min×3、real SF30 full read/retention/WA/digest |
| E5. Workload coverage | `P50-G5-W8-PROVISIONAL`、`P51-G5-WORKLOAD-SF10`、`P52-G5-SF30-REPRESENTATIVE` | P50 `NOW_SAFE`；P51/P52 `BLOCKED_LOAD` | query stratifier、truth、P51→P52 | **25 / 45 / 70 h** | 100--250 GiB | selectivity×degree×semantics×hop×RW matrix 与 SF30 代表 cell |
| E6. Scale/concurrency | `P60-G6-SCALE-SF1-30`、`P61-G6-SCALE-SF100`、`P62-G6-CONCURRENCY` | `BLOCKED_LOAD` | 统一 sample plan；P60→P61；固定 cpuset/stream | **37 / 55 / 70 h** | 420--600 GiB | SF1/10/30/100 scale curve、1/4/8/16/32 concurrency curve |
| E7. Differential safety | `P70-DIFFERENTIAL-SAFETY` | P70 correctness 可 `NOW_SAFE`，资源成本需 clean window | P02A、trace shrinker | **4 / 5 / 10 h** | 5--30 GiB | 多 seed update/delete/tombstone/schema/compact/reopen、fallback rate/cost |
| **原 22 项总计** |  |  |  | **161 / 282 / 435 h** | **全局峰值≤600 GiB** | 原六图全量数据 |

## 5. 历史新增项估算（ARCHIVE / NOT SCHEDULED）

这四项不是用来替换原 22 项的同名近似实验，而是最新稿 claim 的新增正式缺口。

| 阶段 | 任务 ID | 当前状态/通道 | 主要依赖 | 乐观 / 期望 / 悲观 | 新增磁盘或工作集峰值 | 核心产物 |
|---|---|---|---|---:|---:|---|
| N0. Setup | `P03-E00-SETUP-MANIFEST-TABLE` | `READY` / NOW_SAFE | Linux commit、P01 truth/hash | **1 / 2 / 4 h** | <1 GiB | hardware/software、Git/binary/dataset/query/trace SHA、cache/timing/repeat/statistics 表 |
| N1. SF100 主结果 | `P32-G3-SF100-BUDGET-CORE` | `BLOCKED_LOAD` | P03、P02B、P31 collector 验收 | **60 / 85 / 120 h** | 650--800 GiB，variants 串行 | 最新稿全部 9 budget variants；import×3、query×5；candidate/read/RSS/disk/latency/digest/raw/normalized/manifest |
| N2. C2 calibration | `P42-G4-C2-PROXY-CALIBRATION` | `BLOCKED_LOAD` | P03、P02B；先于 P41 | **4 / 8 / 16 h** | 20--60 GiB | controlled SF1/SF10 同 query 的 metadata replay 与 full body-read、倍率/相关性、digest |
| N3. Property micro | `P53-G5-PROP-SIGNATURE-MICRO` | `BLOCKED_LOAD` | P03、P02B、property truth | **4 / 8 / 16 h** | 20--80 GiB | typed only、typed+predicate、property-absence exact pruning、安全前提与独立 repeats |
| **新增项合计** |  |  |  | **69 / 103 / 156 h** | **全局峰值≤800 GiB** | 最新稿新增 claim 的缺失证据 |

算术合计为 `230 / 385 / 591 h`；为避免虚假精度，容量规划将悲观值向上记为 **592 h**。因此“原 22 项 + 新增 4 项”的**全数据计划**按 **230 / 385 / 592 h** 管理。

### 历史全数据日历估算（不得用于当前 ETA）

正式性能任务在一台服务器上串行，不以互相干扰的并行作业缩短工期。按 15% 交接、冷却、重启和人工验收缓冲：

| 情景 | 规划工时 | +15% 后 | 日历承诺（约） |
|---|---:|---:|---:|
| 乐观 | 230 h | 264.5 h | **11 天** |
| 期望 | 385 h | 442.8 h | **19 天** |
| 悲观 | 592 h | 680.8 h | **29 天** |

11/19/29 天从服务器释放并通过 sentinel 后起算，不包含等待其他用户释放资源的未知时间。同步/hash/commit 和 NOW_SAFE correctness 可在等待期间完成。

## 6. 历史最新稿子集估算（ARCHIVE / NOT SCHEDULED）

最新稿当前没有完整 LDBC head-to-head、A0--A6、全 workload heatmap、四点 scale 或 concurrency claim。因此可优先补跑以下子集以尽快替换现有 prototype evidence；但最终论文实验仍按 `ARTICLE-EXPERIMENT-OUTLINE-CN.md` 验收，其余任务不能因最新 ZIP 暂未引用而省略。

| 优先级 | 任务 | 作用 | 前置关系 | 子集预算（乐观 / 期望 / 悲观） |
|---:|---|---|---|---:|
| 0 | `P00/P01/P02A/P02B` + `P03` | provenance、truth、correctness、Experimental Setup | 第一阶段 | 9 / 17 / 29 h |
| 1 | `P32` + `P31` | 复现 SF100 budget/read/RSS 主 claim，并补 catalog/index overhead | P03、P02B；先在 SF10 验 collector | 66 / 95 / 134 h |
| 2 | `P42` + `P41` | 先校准 metadata proxy，再解释 real SF30 C2 | P42→P41 | 12 / 21 / 34 h |
| 3 | `P40` | 用 fixed trace 四 arms 替换不合格的 W9 no-compaction 趋势 | P03、P02B | 10 / 18 / 28 h |
| 4 | `P10/P11` | 最新稿 external baseline gate；不表述为完整 DB 排名 | P02B | 20 / 35 / 50 h |
| 5 | `P53` + `P70` | property-aware read 小实验与扩大后的 safety stress | P02A/P02B | 8 / 13 / 26 h |
| **最新稿关键子集总计** |  |  |  | **125 / 199 / 301 h** |

本节仅保留 2026-07-21 早期审计过程，不触发任务，也不得覆盖第 3 节的 126+42 h ETA。当前 ETA 只根据七天冻结队列的实际 repeat 中位 wall time更新。

## 7. 阶段放行与停止规则

1. **同步放行**：Windows/Linux 文件数和逐文件 SHA-256 完全一致；远端 commit SHA 已写入同步 manifest。
2. **correctness-only 边界**：P02A、W13/P70 的非计时部分可在当前负载下运行，但 manifest 必须写 `performance_eligible=false`；不得记录或引用 timing 结论。
3. **clean-window 放行**：重负载释放、sentinel 方差合格、磁盘满足水位后，P02B 才能开始；P02B PASS 后再启动正式性能阶段。
4. **正确性优先**：任一 `digest_pass=false`、`mismatch_count>0` 或 query count/order/hash 不一致，立即停止该配置 timing，保留失败产物并修 correctness。
5. **重复次数**：所有正式点先做 3 个独立进程 run；QPS CV >3% 或 P99 CV >5% 时补到 5 个。import/dynamic/compaction 至少 3 个独立 run。同进程循环不能冒充独立重复。
6. **隔离**：不得同时运行两个 NEED_CLEAN_WINDOW 任务；不得边 import 边测另一个系统 latency；cache state、cpuset/quota 与 warmup 边界必须固定。
7. **磁盘水位**：P32 开始前 `/data` 可用空间建议 ≥850 GiB，运行中硬停止水位 180 GiB。六个正式 variants 串行构建；raw、telemetry、manifest、hash、normalized 永久保留，可重建 store 按预先冻结的 retention policy 处理。
8. **完成定义**：只有 raw、normalized、manifest、artifact SHA、规定 repeats、correctness 和统计摘要全部齐全，task 才能从 `VALIDATING` 改为 `PASS`。

## 8. 实时日志字段

每个 attempt 使用独立 run 目录，并向 append-only 进度日志追加事件。长任务不能只依赖终端输出；最低字段为：

```text
updated_at_cst,phase,task_id,lane,state,performance_eligible,
run_id,attempt,repeat_index,repeats_target,
host_fingerprint,git_sha,binary_sha256,dataset_sha256,truth_sha256,
query_or_trace_sha256,config_sha256,cache_state,cpuset,command_sha256,
start_at_utc,end_at_utc,elapsed_s,pid,exit_code,
load1,load5,load15,other_user_rss_gib,mem_available_gib,data_free_gib,
data_read_mib_s,data_write_mib_s,nvme_util_pct,process_cpu_pct,process_rss_gib,
digest_pass,mismatch_count,operations_done,operations_target,
artifact_dir,stdout_path,stderr_path,telemetry_path,manifest_path,
status_reason,next_action,eta_cst
```

`run_id` 格式：`<task_id>-<UTC YYYYMMDDTHHMMSSZ>-<short_git_sha>-rNN`。原始时间使用 UTC，进度表展示 CST；原始大小使用 bytes，展示时再换算 GiB/MiB。

## 9. 更新规则

- **启动前**：追加 `PREPARING` 事件，记录完整命令 hash、版本/hash、背景负载、磁盘水位和通道判定；缺项不启动。
- **启动后**：立即追加 `RUNNING`；超过 15 分钟的任务每 15 分钟 heartbeat，更新操作进度、RSS、CPU、I/O、剩余磁盘和 ETA。
- **每个 repeat/variant/arm 完成后**：先写 raw artifact SHA，再跑 correctness gate；通过后才增加 `repeats_done`。P32 至少按 variant 更新一次本表。
- **异常**：追加 `FAIL` 或 `BLOCKED_LOAD` 并保留 stdout/stderr/telemetry；不覆盖失败 run，不复用 run_id。外部负载突然出现、I/O 异常或磁盘触及水位时安全终止 attempt。
- **阶段完成**：核对所有 task 为 `PASS`，生成阶段 SHA-256 manifest 和规范化摘要，再更新本表的实际耗时、状态与剩余 ETA。
- **ETA**：未开始时使用三点估计；同类独立 repeat ≥2 后，用已完成 repeat 的中位 wall time×剩余数量，再加 15% 清理/验证缓冲。变化超过 20% 时在 `status_reason` 说明。
- **论文入图前**：重新做 claim-to-evidence 审计。provisional、历史不同硬件、单次运行或不同 truth 的值不得自动升级成正式结果。

## 10. 范围说明

- 第 4--6 节只保留历史估算和设计来由，状态统一为 `ARCHIVE / NOT SCHEDULED`。
- 当前唯一执行真源是第 3 节：P32 六个可解释 budget 点，scale 四个核心 variants，所有正式点 n=3 起跑并按方差补到 5。
- 七天方案覆盖大纲中全部 RQ、指标类别和 correctness gate；它使用预注册代表性 cells，因此不声称穷举所有 workload、budget 与 lifecycle 组合。
