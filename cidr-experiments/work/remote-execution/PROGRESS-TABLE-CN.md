# CIDR Linux 正式补跑进度表

最后审计：2026-07-22 02:49（Asia/Shanghai）
当前执行真源：`cidr-experiments/PROGRESS-CN.md` 第 3 节。
七天预算：按 E01 历史实测复核后，主路径 `132--138 h`，风险缓冲 `30--36 h`，硬上限 `168 h`。T0 是其他用户重负载释放且 clean-window sentinel PASS 的时刻；T0 前已经完成的工程会形成实际余量。

状态约定：`PASS` 表示对应工程/正确性 gate 已通过；`VALIDATING` 表示实现已落地但正式 runner/汇总/审计尚未收口；`BLOCKED_LOAD` 表示只被服务器环境阻塞；`NOT_STARTED` 表示正式性能点尚未启动。correctness PASS 不等于性能数据可入论文。

## 当前总览

| ID | 阶段 | 状态 | 已完成 | 当前阻塞/下一步 | T0 后预算 |
|---|---|---|---|---|---:|
| S0 | 论文审计、目录同步、文件数、SHA、commit | `PASS_INITIAL` | 20 来源完成 REUSE/FIX/RERUN 审计；145/145 payload SHA、146 总文件通过；初始提交 `acb167e` | clean integration 当前 `70acae3550bf`；P20 admission 完成后做最终全量 SHA + commit | 已完成 |
| S1 | ID map、shared truth、统一 telemetry、sentinel | `PASS / VALIDATING` | P01 全量双向 ID map PASS；P31 PASS；P02B runner、tree manifests 与 1,700-query plan 均完成，preflight 0 mismatch | P20 正在改为消费 canonical P02B admission；P03 READY 后跑真实 sentinel | T0 后 23--35 min |
| S2 | SF1 correctness gates | `PASS` | W13 10/10；W6 九变体 8 组 compare 均 checked=180、mismatches=0 | 跨系统 P02B gate 要在 clean window 运行 | 计入 setup 2 h |
| S3 | G1 matched external / 条件式 LDBC E2E | `IMPLEMENTED / BLOCKED_ADAPTERS_LOAD` | 六系统 orchestrator、warmup/measured、P95/P99、digest、P31 和 API 分组已集成；8 tests PASS | SemL0/LiveGraph adapter 并行实现；Aster/TuGraph/Neo4j/Nebula 仍需真实 binary/image/store gate | 18--24 h |
| S4 | G2 A0--A6 staircase + G3 resource | `INTEGRATED_VALIDATING / BLOCKED_LOAD` | A0--A6 core、formal runner、correctness generator、run-level Figure 2 aggregator 与 P31 已集成；Python 35、Rust 8/8 PASS | 完成 P02B admission receipt 兼容测试；正式点等清场 | 54 h |
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
| P02B shared-truth preflight | `PASS_CORRECTNESS` | `/data/WorkSpace/results/P02B/` | 1,700/1,700 checked；0 mismatch；dataset/store/plan/preflight `SHA256SUMS` 4/4 OK |

## 最新服务器门禁

采样时间：2026-07-22 02:48:11 CST，run `P03-CLEAN-WINDOW-20260721T161610Z-acb167eba8fd`。

| 指标 | 当前值 | 门槛 | 结果 |
|---|---:|---:|---|
| load1 | 1.83 | <5 | PASS |
| CPU idle | 99.21% | >95% | PASS |
| MemAvailable | 355.53 GiB | >=400 GiB | FAIL |
| `/data` free | 956.77 GiB | 足够本阶段且保留硬水位 | PASS |
| NVMe util / await | 0% / 0 ms | <5% / <5 ms | PASS |
| 外部任务 | 1 个 zcl 大内存任务、4 个 rsync、GPStore、TuGraph | 正式窗口内全部释放/暂停 | FAIL |

因此门禁状态为 `BLOCKED_LOAD`。这里不能通过“少跑几个进程”规避：共享内存、page cache、NUMA 和服务抖动会污染正式结果。当前可安全并发的是轻量工程、代码测试与 correctness-only；大规模 I/O 最多 1 个，正式性能一律串行。

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
- [ ] 完成 P20↔P02B admission receipt 最终兼容测试。
- [ ] 完成 SemL0/LiveGraph 真实 adapter，再继续 Aster/TuGraph/Neo4j/Nebula adapter。
- [ ] 将最终工程分支集成，同步后复核全量文件数/SHA，并做最终 commit。
- [ ] 等外部重负载释放；连续观察 10--15 分钟后运行 SF10 sentinel。
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
