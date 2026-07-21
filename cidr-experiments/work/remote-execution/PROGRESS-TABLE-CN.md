# CIDR Linux 正式补跑进度表

最后审计：2026-07-22 06:34（Asia/Shanghai）
当前执行真源：`cidr-experiments/PROGRESS-CN.md` 第 3 节。
七天预算：按 E01 历史实测复核后，主路径 `132--138 h`，风险缓冲 `30--36 h`，硬上限 `168 h`。T0 是其他用户重负载释放且 clean-window sentinel PASS 的时刻；T0 前已经完成的工程会形成实际余量。

状态约定：`PASS` 表示对应工程/正确性 gate 已通过；`VALIDATING` 表示实现已落地但正式 runner/汇总/审计尚未收口；`BLOCKED_LOAD` 表示只被服务器环境阻塞；`NOT_STARTED` 表示正式性能点尚未启动。correctness PASS 不等于性能数据可入论文。

## 当前总览

| ID | 阶段 | 状态 | 已完成 | 当前阻塞/下一步 | T0 后预算 |
|---|---|---|---|---|---:|
| S0 | 论文审计、目录同步、文件数、SHA、commit | `PASS_INITIAL` | 20 来源完成 REUSE/FIX/RERUN 审计；145/145 payload SHA、146 总文件通过；初始提交 `acb167e` | clean integration 当前 `36f0fc14473d`；外部 store gate 收口后做最终全量 SHA、反向同步和封板 commit | 已完成 |
| S1 | ID map、shared truth、统一 telemetry、sentinel | `PASS_PREPARED / BLOCKED_LOAD` | P01 全量双向 ID map PASS；P31 PASS；P02B canonical admission 已接入 P20/P10；四套 SemL0 SF10 store、tree manifests 与 1,700-query plan 均完成，7/7 SHA、四套 0 mismatch | P03 READY 后跑真实 sentinel | T0 后 23--35 min |
| S2 | SF1 correctness gates | `PASS` | W13 10/10；W6 九变体 8 组 compare 均 checked=180、mismatches=0 | 跨系统 P02B gate 要在 clean window 运行 | 计入 setup 2 h |
| S3 | G1 matched external / 条件式 LDBC E2E | `6_OF_6_ADAPTERS / 3_STORE_GATES_PASS / 3_HARDENING` | 六系统真实 adapter 全部集成；SemL0/Aster/TuGraph SF10 store gates PASS；Aster build 与 Neo4j/Nebula image identity 已冻结 | LiveGraph build receipt/formal wrapper、Neo4j lifecycle/P31、Nebula runner/RAFT launcher 并行修复；通过独立审查后再跑其 SF10 correctness | 18--24 h |
| S4 | G2 A0--A6 staircase + G3 resource | `PASS_INTEGRATED / BLOCKED_LOAD` | A0--A6 core、formal runner、correctness generator、run-level Figure 2 aggregator、P31 与 canonical P02B admission 已集成；Python 40/40（另 1 条件项）、Rust 8/8 PASS | 正式点等 P02B formal PASS 与清场 | 54 h |
| S5 | G4 fixed-trace dynamic/full-read | `PASS_CORRECTNESS / BLOCKED_LOAD` | clean-head fixture 3×4 arms、36 queries、0 mismatch、62/62 SHA PASS | 30 min×3 正式运行及 real full-read 等清场 | 14 h |
| S6 | G5 workload coverage | `PREPARING / BLOCKED_LOAD` | workload 维度和代表 cells 已冻结；旧 raw 仅作校准 | runner 接统一 truth/P31；正式 representative cells 等清场 | 8 h |
| S7 | G6 scale + concurrency | `PREPARING / BLOCKED_LOAD` | SF1/10/30/100、4 variants 与 1/4/8/16/32 threads 已冻结；并发资源规则已冻结 | 复用已验证 stores；正式 query/concurrency 等清场 | 10 h |
| S8 | differential safety、normalize、final data | `PARTIAL` | correctness/manifest/图数据契约已定义 | 正式 raw 到 tidy TSV、统计/CI、claim-to-evidence 封板 | 14 h |

正式性能数据点：**0（尚未启动）**。当前完成的是前置工程、correctness 和数据契约；现有图仍是 provisional evidence。

## 已验收运行

| 任务 | 状态 | Run ID / commit | 核心验收 |
|---|---|---|---|
| W13 schema evolution | `PASS` | `P02-W13-CORRECTNESS-20260721T160741Z-acb167eba8fd` | 10/10；49 artifacts SHA OK |
| W6 SF1 九变体 | `PASS` | `P02-W6-SF1-CORRECTNESS-20260721T161132Z-acb167eba8fd` | 8×checked=180；0 mismatch；76 artifacts SHA OK |
| P01 ID map recovery | `PASS` | `P01-IDMAP-20260721T171325Z-187e851` | 355,185,382 edges；29,987,835 vertices；双向 map/lockstep/bijection/SHA PASS；20m15s，仅工程验收 |
| P31 collector | `PASS` | `95942ff` + `7e09304` | 11 unit tests、fixture smoke、11/11 双端 SHA；正式实验只允许 1 benchmark + 1 sidecar |
| P40 fixed-trace fixture | `PASS_CORRECTNESS` | `P40-CLEAN-HEAD-36Q-20260721T181522Z-d7c283c5808f` | clean git；3×4 arms；36 queries；0 mismatch；62/62 SHA；`performance_eligible=false` |
| P02B shared-truth preflight | `PASS_CORRECTNESS` | `/data/WorkSpace/results/P02B/` | 1,700/1,700 checked；0 mismatch；dataset + 4 stores + plan + preflight `SHA256SUMS` 7/7 OK |
| P02B 四套 SemL0 SF10 store | `PASS_CORRECTNESS` | `/data/WorkSpace/results/P10-SEML0-STORES/P10-SEML0-SF10-STORE-REBUILD-20260721T193325Z-d123990/` | naive/schema/budg-b64/semantic 各 1700/0 mismatch；P02B 7/7 SHA；artifact 11/11 SHA；明确 `formal_performance_points=0` |
| P10 六系统 adapter | `PASS_ENGINEERING` | integration `36f0fc14473d` | 六系统真实 tiny API 回归 PASS；未跑 SF10 timing |
| Aster clean final build | `PASS_CORRECTNESS` | `/data/WorkSpace/results/P10-ASTER-BUILD/P10-ASTER-BUILD-20260721T200600Z-6abb258e/` | worker `12f848...c1b8`；source clean；tiny 7/7；artifact 62/62 SHA；性能点 0 |
| LiveGraph build/readiness | `PASS_BINARY / FAIL_FORMAL_AUDIT` | `/data/WorkSpace/results/P10-LIVEGRAPH-BUILD/P10-LIVEGRAPH-CORRECTNESS-BUILD-20260721T204315Z-3965a66/` | source/binary/lib 可复用且 tiny 15/15；但 receipt 内 integration HEAD 矛盾，缺 formal template/P02B/P31/env/PID 绑定；须重建后才能跑 SF10/formal |
| TuGraph 旧 SF10 compatibility | `FAIL_CLOSED_EXPECTED` | `/data/WorkSpace/results/P10-TUGRAPH-STORES/P10-TUGRAPH-SF10-FROZEN-20260721T203500Z-e650709/` | 旧 DB format 4.0 与 final 4.5.2 不兼容；warmup 前 SIGABRT；source 未改；性能点 0；已启动 4.5.2 fresh rebuild |
| TuGraph 4.5.2 SF10 store | `PASS_CORRECTNESS` | importer `e1bdf312`；traversal fix `36f0fc14`；run `P10-TUGRAPH-SF10-REBUILD-20260721T205301Z-2fc3a11/attempts/attempt2` | 29,987,835 vertices/355,185,382 edges；warmup+measured 各1700/0/84,104,814；45/45 SHA；旧867 mismatch attempt保留；性能点0 |
| Aster SF10 store | `PASS_CORRECTNESS` | `P10-ASTER-SF10-FRESH-20260721T215115Z-6abb258e` | fresh/reopen warmup+measured 四阶段各1700/0/0/84,104,814；154 files/11.92GB；store SHA稳定；37/37 SHA；性能点0 |
| Neo4j/Nebula runtime | `PASS_TINY / FORMAL_HARDENING` | `/data/WorkSpace/results/P10-NEO4J-ADAPTER/`；`/data/WorkSpace/results/P10-NEBULA-RUNTIME/` | Neo4j Bolt 4/4、Nebula nGQL 5/5 与 image digest 可复用；tiny 不覆盖 receipt/P31/repeat/launch/stop/RAFT logical-host，相关正式路径正在修复 |

## 最新服务器门禁

采样时间：2026-07-22 06:25:11 CST，run `P03-CLEAN-WINDOW-20260721T161610Z-acb167eba8fd`。

| 指标 | 当前值 | 门槛 | 结果 |
|---|---:|---:|---|
| load1 | 1.09 | <5 | PASS |
| CPU idle | 99.191% | >95% | PASS |
| MemAvailable | 353.937 GiB | >=400 GiB | FAIL |
| `/data` free | 872.324 GiB | 足够本阶段且保留硬水位 | PASS |
| NVMe util / await | 0% / 0 ms | <5% / <5 ms | PASS |
| 外部任务 | zcl 约120 GiB worker、4 个 rsync、GPStore、TuGraph | 正式窗口内全部退出/干净停止 | FAIL |

因此门禁状态为 `BLOCKED_LOAD（0/15）`。SF1 rsync 的两个进程已暂停约 41 h，但仍会永久挡门；SF300 rsync 仍在活动。这里不能通过“少跑几个进程”规避：共享内存、page cache、NUMA 和服务抖动会污染正式结果。当前可安全并发的是轻量工程、代码测试与 correctness-only；大规模 I/O 最多 1 个，正式性能一律串行。全部清场后，P03 最坏约 15 分 1 秒才会 READY。

## 下一步队列

- [x] 同步 `cidr-experiments/` 到 Linux。
- [x] 校验本地/远端文件数量、相对路径和逐文件 SHA-256。
- [x] 提交初始同步包并记录 commit。
- [x] 完成 W13 与 W6 SF1 correctness-only。
- [x] 完成 dense↔original ID map、shared truth 基础与独立 SHA 复核。
- [x] 完成 P31 资源采集器和并发/资源门禁。
- [x] 完成 P40 4-arm fixed-trace correctness fixture。
- [x] 完成 P20 单 profile runner、correctness generator、Figure 2 run-level 聚合与第二轮审计。
- [x] 完成 P02B SF10 sentinel runner、P31 fixture、dataset/store tree SHA 和 shared query plan。
- [x] 完成统一 P10/P11 六系统 orchestrator 与 fixture 契约。
- [x] 完成 P20↔P02B admission receipt 最终兼容测试。
- [x] 完成 SemL0/LiveGraph/Aster/TuGraph/Neo4j/NebulaGraph 六系统真实 adapter 和 tiny correctness/contract 回归。
- [x] 固化 Aster final worker 与 Neo4j/Nebula image/runtime identity。
- [x] 完成 LiveGraph correctness binary build，并合入 Aster formal template 与 TuGraph frozen importer。
- [ ] 重做 LiveGraph 结构化 formal build receipt、template/P02B/P31/env/PID 绑定并运行 SF10 correctness；旧 receipt 因 HEAD 矛盾不得用于 formal。
- [x] 完成 TuGraph 4.5.2 SF10 fresh import/manifest/3400-query correctness；保留并修复旧 worker 的 867 mismatch 证据。
- [x] 完成 Aster SF10 fresh/freeze/reopen gate：四阶段1700/0，37/37 SHA，store SHA稳定。
- [ ] 修复并固化 Neo4j formal/P31 与 NebulaGraph runner/RAFT lifecycle/SF10 store gate。
- [ ] 将最终工程分支集成，同步后复核全量文件数/SHA，并做最终 commit。
- [ ] 等 zcl 释放大内存与全部 rsync、维护者停止 GPStore/TuGraph；P03 连续 15 个 60 s 样本后运行 SF10 sentinel。
- [ ] sentinel PASS 后启动 P02B，再按 S3→S8 串行跑正式性能数据。

## 每次正式运行更新模板

```text
run_id:
candidate_id:
formal_eligible: true|false
commit:
dirty_status_sha256:
binary_sha256:
runner_sha256:
input_manifest_sha256:
truth_sha256:
query_list_sha256:
start_time:
end_time:
exit_code:
DONE/PASS/FAILED:
repeats_completed:
correctness_checked:
mismatches:
raw_dir:
normalized_output:
known_issues:
next_action:
```
