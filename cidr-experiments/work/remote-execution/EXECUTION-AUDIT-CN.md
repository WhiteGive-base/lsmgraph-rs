# Linux 补跑执行审计与总工期估算

审计快照：2026-07-21 22:58（Asia/Shanghai）  
Linux 仓库：`/data/WorkSpace/lsmgraph-rs`  
审计方式：只读；没有启动 benchmark，也没有修改远端文件。

## 1. 结论

当前可以立即进行文件同步后的 SHA-256、manifest、JSON/DONE 一致性校验，也可以在明确只记录 PASS/FAIL、不引用性能数字的前提下运行 SF1 correctness。当前不适合启动任何正式 latency、QPS、CPU、RSS/PSS、I/O、import、compaction 或 concurrency 测量。

原因不是总 CPU load 很高，而是同机另一个用户有两个持续满核的 Python 任务，审计时分别约占 117.6 GiB 和 31.3 GiB RSS。它们还可能持续改变 page cache、内存带宽和 `/data` I/O；这些影响无法靠 `nice` 消除。

若实验 runner 按本报告所列依赖补齐，并获得独占或稳定静默的服务器窗口，全部正式证据的端到端日历时间估算为：

- 乐观：约 161 小时，连续执行约 6.7 天；含 15% 重试余量后约 8 天。
- 期望：约 282 小时，连续执行约 11.8 天；含 15% 重试余量后约 14 天。
- 悲观：约 436 小时，连续执行约 18.1 天；含 15% 重试余量后约 21 天。

这里包含必要的 runner/协议工程、smoke、正式机器运行和最终归一化；不包含等待其他用户释放服务器的时间。多个 agent 能缩短工程准备，但同一台机器上的正式性能运行必须串行，因此不能把机器临界路径按 agent 数量等分。

## 2. 远端环境事实

| 项目 | 审计结果 |
|---|---|
| branch | `codex/sf100-basegraph-bench` |
| HEAD | `3d09ecb98b066e77839f9b09995f4b9e0e196e7c` |
| HEAD 说明 | `cidr-r4-current-source-baseline`，2026-07-11 |
| 工作区 | dirty；修改/未跟踪项集中在 CIDR 文稿及打包文件，`src/` 未显示修改 |
| 当前 `cidr-experiments/` | 审计时 Linux 上不存在，需先同步 |
| 主机 | `finbench`，Xeon 6982P-C，128 logical CPUs，495 GiB RAM，无 swap |
| 内存 | 167 GiB used，325 GiB available |
| `/data` | NVMe，2.0 TiB，总计约 900 GiB used、968 GiB available |
| 背景任务 | zcl 两个 Python worker 各约 100% CPU，RSS 约 123,343,044 KiB 与 32,806,784 KiB |
| 输入 | SF1 889 MiB；SF10 9.6 GiB；SF30 30 GiB；SF100 98 GiB，均存在 |
| 基础 store | SF30 21 GiB；SF100 66 GiB；C2 SF30 116 GiB；W6 SF10 schema/naive 各 14 GiB |
| 可执行文件 | `target/release/lsmgraph`、`target/release/w5_steady_state_real_store` 均存在 |
| 遥测工具 | `pidstat`、`iostat`、`taskset`、`sha256sum` 可用；`jq`、`numactl`、`perf`、`smem` 未发现 |

`smem` 缺失不是硬阻塞：PSS 可从 `/proc/<pid>/smaps_rollup` 读取。`numactl` 缺失也不是硬阻塞：可用 `taskset` 固定 CPU；但 NUMA 内存绑定若进入正式协议，需要安装 `numactl` 或明确不绑定并记录这一事实。

只提交 `cidr-experiments/` 并不会自动让现有工作区变 clean。正式运行推荐从同步后的提交创建独立 clean worktree，或者至少把 `git HEAD`、完整 `git status` 和 runner/source 的 SHA-256 写入每个 run manifest；不应清理或覆盖现有用户改动。

## 3. 立即可运行的项目

### 3.1 同步后立即做，不受共享负载影响

1. 对 `cidr-experiments/` 做本地/远端文件数、相对路径集合和逐文件 SHA-256 校验。
2. 冻结 run manifest schema：run ID、HEAD、dirty status、hostname、CPU、memory、kernel、filesystem、binary SHA、runner SHA、input/truth/query-list SHA、参数、开始/结束时间、退出码、DONE/PASS/FAILED 优先级。
3. 对旧 raw 做 JSON schema、wrapped JSON、DONE/result、重复 `record_id` 和 artifact hash 校验。
4. 验证所有 runner 的 `--help`、配置展开和 dry-run；不触碰大 store。
5. 生成 shared truth/ID map 的代码和小样 fixture；全量转换应避开其他用户的 I/O 峰值。

现有基准哈希：

| artifact | SHA-256 |
|---|---|
| external SF10 `truth-s50-seed42.tsv` | `876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788` |
| W6 SF10 sample plan | `61f3bd9f391c0a056f76d60c228a44801d00265b8f8798c495e127fbfc424d0c` |
| W6 SF100 sample plan | `81080052c44f23326a80cdbe830cc74f64dfe807f8f28e4167a6cdbb7d23fc78` |
| 旧 W6 SF1 dry-run sample plan | `08381762a8f5a36c88aff720e7947585e99f262e7ba98e9cf1703477b3840cc7` |

### 3.2 可在当前负载下做，但只允许报告 correctness

| gate | 当前可执行性 | 运行时间依据 | 结果用途 |
|---|---|---|---|
| W13 schema-evolution 10 tests | runner 和 Cargo tests 已存在；同步/commit 后可运行 | 历史 warm-cache 的 10 项测试约 1 秒；冷 build 预计 5–30 分钟 | 只报告 10/10 PASS/FAIL，不引用 `/usr/bin/time` |
| W6 SF1 9 变体 neighbor-compare | `SCALE=sf1 DRY_RUN=1`、SF1 input、release binary 均可用 | 历史运行 02:43:29–03:00:13，即 16 分 44 秒；全新 store/共享负载下预计 20–60 分钟 | 变体间 0 mismatch gate；不等于跨系统 shared-truth gate，不引用 latency/import time |
| P70 differential safety 现有 W13 子集 | 现有 10 tests 可立即复验 | 5–30 分钟 | correctness sanity；完整多 seed stress 仍需实现 |

运行 correctness 时仍应使用唯一 RUN_ID、保留 stdout/stderr/exit code/DONE、以低优先级运行，并在 manifest 写明 `performance_eligible=false`。目前不要启动这些 gate 的正式 timing 版本。

## 4. 分阶段时长、磁盘和并行性

所有时间均为 wall time。O/E/P 表示 optimistic/expected/pessimistic。

| 阶段 | 范围与组成 | O / E / P | 峰值新增磁盘 | 并行性 | 当前状态 |
|---|---|---:|---:|---|---|
| S0 同步与完整性 | 同步、文件数/路径/SHA、commit、manifest schema | 0.3 / 0.7 / 1.5 h | <1 GiB | 可与文稿审计并行 | 可立即做 |
| S1 协议与 harness | dense↔original ID map、shared truth consumer、统一 query list、telemetry/manifest fixture；含 smoke | 4.5 / 11 / 22 h | 1–5 GiB | 工程可多 agent；全量 hash/convert 避开 I/O 峰值 | 代码可立即做；未完成 |
| S2 SF1 correctness | W13、W6 变体 compare、shared-truth SF1 gate | 0.5 / 1 / 2 h | 5–20 GiB | 可在当前负载下低优先级执行；不能与正式性能实验重叠 | 部分立即可跑，跨系统 gate 等 S1 |
| S3 G1 matched/E2E | P10 SemL0 3/4.5/6 h；P11 external 12/16/20 h；P12 LDBC E2E 24/48/72 h | 39 / 68.5 / 98 h | 180–300 GiB | 工程可并行；各系统正式 load/query 必须串行 | 被 S1、runner 修复和共享负载阻塞 |
| S4 G2+G3 ablation/resource | 开关与 telemetry 8/16/32 h；P20 4/7/10 h；P21 8/14/20 h；P31 6/10/14 h | 26 / 47 / 76 h | 180 GiB sequential；保留全矩阵更高 | 共享 import/store，但正式采集串行 | 需实现独立开关和 PID telemetry |
| S5 G4 lifecycle | fixed trace/第四 arm/telemetry 4/8/16 h；P40 6/8/10 h；P41 8/13/18 h | 18 / 29 / 44 h | 120–260 GiB | arms/repeats 不并行，避免 I/O 干扰 | 需固定 seed/trace 和 full-read phase |
| S6 G5 workload coverage | stratifier/truth 8/16/32 h；P51 12/18/24 h；P52 12/21/30 h | 32 / 55 / 86 h | 150–250 GiB | cell 可分批，但同机 timing 串行 | 需实现 coverage runner |
| S7 G6 scale/concurrency | generator/runner 修复 6/12/24 h；P60 8/13/18 h；P61 18/27/36 h；P62 4/8/12 h | 36 / 60 / 90 h | SF100 transient 420–600 GiB | scale、variant、thread points 正式运行串行 | 依赖前面选定 variants；当前 load 阻塞 |
| S8 safety 与收尾 | P70 2/5/8 h；归一化、统计 gate、正式图数据 3/5/8 h | 5 / 10 / 16 h | 5–30 GiB | safety 可与非 timing 工程并行；最终统计等数据齐全 | 部分可提前 |
| **总计** | 不含等待服务器释放 | **161.3 / 282.2 / 435.5 h** | **最高约 600 GiB transient** | 正式 timing 关键路径串行 | 当前仅 S0、S1 代码和 S2 correctness 可开始 |

建议外部承诺使用含 15% 重试余量的 8/14/21 天，而不是直接承诺 7/12/18 天。首次运行新 runner、外部 DB 服务启动失败、SF100 磁盘回收和 checksum 都可能触发重试。

## 5. 22 项候选的可执行性

| candidate | 判定 | 当前能否启动 | 关键依赖/说明 |
|---|---|---|---|
| P00-W6-RESOURCE-REPARSE | 本地已完成 | 同步后只需复验 | parser-only；SF10 elapsed 已从 raw 修正 |
| P00-RQ3-RESUMMARIZE | 本地已完成 | 同步后只需复验 | parser-only；旧 raw 仍只可 provisional |
| P00-LEGACY-NORMALIZE | 本地已有 normalizer/registry | 可立即做 | 不受负载影响；应在 Linux 重放并比较 hash |
| P00-W13-POINTER | 本地已完成 | 同步后只需复验 | 指向 `20260615-143649`，10/10 PASS |
| P01-SHARED-TRUTH-BRIDGE | 尚未实现 | 只能开始编码/fixture | converter 不保留 dense↔original map，digest 语义不同 |
| P02-SF1-CORRECTNESS-GATE | 部分 ready | 变体 correctness 可；跨系统 gate 不可 | W6 dry-run/W13 可跑；shared truth 依赖 P01 |
| P10-G1-SEML0-MATCHED-SF10 | 需新 runner | 不可正式启动 | 依赖 P01、5 个独立 runs、clean commit、静默服务器 |
| P11-G1-EXTERNAL-SF10 | legacy scripts/store 存在 | 不可正式启动 | LiveGraph consumer 要改；fresh load、服务清理、统一 truth、静默服务器 |
| P12-G1-LDBC-E2E | E12 存在但不可直接用 | 不可启动 | E12 把同一 store 同时传给 SF10/SF30，并且 driver 失败后仍 `touch PASS`；必须修复和做语义等价 gate |
| P20-G2-STAIRCASE-SF10 | 未有正交开关 | 不可启动 | admission/routing/degree/feedback/compaction 仍耦合 |
| P21-G2-STAIRCASE-SF30 | 依赖 P20 | 不可启动 | 先在 SF10 冻结 3–4 个关键点 |
| P30-G3-IMPORT-PARETO-PROVISIONAL | 本地已完成 | 可复验 | 只能标 import-only/historical/single-run |
| P31-G3-FULL-RESOURCE-SF10 | 需 collector | 不可正式启动 | PID CPU、PSS、disk breakdown、temp peak 尚未接入 |
| P40-G4-FIXED-TRACE-RQ3 | 旧 runner 不合格 | 不可正式启动 | wall-clock RNG、缺 semantic-static arm、缺 digest/PID telemetry；3×4 arms 必须串行 |
| P41-G4-C2-FULL-READ | 只有 metadata proxy | 不可启动 | 需 full body decode、fixed truth、digest、3 runs |
| P50-G5-W8-PROVISIONAL | 本地已完成 | 可复验 | 仍是 edge type 1、无版本化 plan/digest |
| P51-G5-WORKLOAD-SF10 | 未实现完整 stratifier | 不可启动 | selectivity×degree×semantics×hop×RW truth/cell SHA 缺失 |
| P52-G5-SF30-REPRESENTATIVE | 依赖 P51 | 不可启动 | 需先由 SF10 选异常/代表 cells |
| P60-G6-SCALE-SF1-30 | runner/query generator 未统一 | 不可正式启动 | SF1/SF10/SF30 必须相同 query count 与 strata |
| P61-G6-SCALE-SF100 | 依赖 P60 | 不可启动 | 先冻结 3–4 variants；启动前建议 `/data` 可用空间至少 800 GiB |
| P62-G6-CONCURRENCY | E9 仅单点 smoke | 不可正式启动 | 需 fixed stream、taskset/cpuset、driver/server telemetry；修复 E9/E12 path/PASS gate |
| P70-DIFFERENTIAL-SAFETY | 现有 W13 子集 ready | 子集可立即跑；完整 stress 不可 | 完整版需 multi-seed、trace shrinker、digest 和失败 trace 保留 |

## 6. 正式启动前硬 gate

只有以下条件全部满足，才允许把某次 run 标记为 `formal_eligible=true`：

1. 服务器背景负载进入稳定静默窗口；无未知长任务占用 CPU、内存或 `/data` I/O。
2. 使用冻结 commit；推荐 clean worktree。binary、runner、truth、query list、输入 manifest 均有 SHA-256。
3. 每个系统/variant 使用相同 truth、query order、warmup/cache policy 和计时边界。
4. 短查询 5 个独立 process runs；import/dynamic/compaction 至少 3 个独立 runs。
5. correctness mismatch=0；runner exit=0；DONE/PASS 与 raw final event 一致。
6. PID 级 CPU、RSS/PSS、I/O、磁盘分项和临时峰值齐全；不能用全机 MemAvailable 代替进程资源。
7. 每个 run 都记录失败原因；不得把 failed/partial run 从 denominator 中静默删除。
8. 启动 SF100 前重新检查磁盘增长；600 GiB transient 估算下应保留至少 200 GiB 安全余量。

## 7. 推荐启动顺序

1. S0：同步、双端 SHA、commit、manifest schema。
2. S1 与 S2 并行推进：一组 agent 做 truth bridge/harness，另一组执行只报告 correctness 的 W13 和 W6 SF1 gate。
3. 服务器释放后先做短的 matched SemL0 SF10 sentinel；确认噪声和协议，再做 G1 external/E2E。
4. G2/G3 共用一次经过验证的 import/store 和 telemetry，先 SF10，再选关键点到 SF30。
5. G4 fixed trace；每个 arm/repeat 串行并从同一 immutable base clone。
6. G5 coverage；先 SF10 完整矩阵，再跑 SF30 代表 cells。
7. G6 最后执行；先 SF1/10/30 冻结 variants，最后才花 18–36 小时跑 SF100。
8. 每完成一阶段立刻 normalize、hash、备份 raw、更新进度表；不要等所有实验结束再发现字段缺失。

