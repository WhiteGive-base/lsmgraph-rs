# CIDR Linux 正式补跑进度表

最后审计：2026-07-21 22:58（Asia/Shanghai）  
预计总工期：乐观 161 h / 期望 282 h / 悲观 436 h；含 15% 重试余量建议按 8 / 14 / 21 天安排。  
正式性能运行前置条件：独占或稳定静默的服务器窗口。

状态约定：`READY` 可启动；`RUNNING` 正在运行；`BLOCKED-LOAD` 等待服务器；`BLOCKED-IMPL` 等实现；`VERIFY` 已有结果待复验；`DONE` gate 全通过；`FAILED` 保留失败证据并处理。

| ID | 阶段 | 状态 | O/E/P | 依赖 | 开始 | 结束 | raw/run_id | gate/备注 |
|---|---|---|---:|---|---|---|---|---|
| S0 | 同步、文件数、SHA-256、commit、manifest | READY | 0.3/0.7/1.5 h | 无 | — | — | — | 双端文件相对路径和逐文件 hash 完全一致 |
| S1 | ID map、shared truth、统一 runner/telemetry | BLOCKED-IMPL | 4.5/11/22 h | S0 | — | — | — | SF1 shared truth 0 mismatch，manifest fixture PASS |
| S2 | SF1 correctness gates | READY-PARTIAL | 0.5/1/2 h | S0；跨系统 gate 还依赖 S1 | — | — | — | W13 10/10；W6 variants 0 mismatch；不引用 timing |
| S3 | G1 matched external + LDBC E2E | BLOCKED-LOAD/IMPL | 39/68.5/98 h | S1、S2、静默服务器 | — | — | — | 同一 truth/order/cache；每系统 5 runs；完整资源 |
| S4 | G2 staircase + G3 resource | BLOCKED-IMPL | 26/47/76 h | 独立开关、PID telemetry、静默服务器 | — | — | — | A0–A6 单变量；3/5 independent runs；digest=0 mismatch |
| S5 | G4 fixed-trace compaction/full-read | BLOCKED-IMPL | 18/29/44 h | fixed trace/seed、第四 arm、telemetry | — | — | — | 4 arms×3；trace SHA 相同；full-read digest 完整 |
| S6 | G5 SF10 coverage + SF30 representative | BLOCKED-IMPL | 32/55/86 h | stratifier/truth、静默服务器 | — | — | — | 每 cell query SHA/correctness；SF30 代表 cells 3 runs |
| S7 | G6 scale + concurrency | BLOCKED-IMPL | 36/60/90 h | 统一 generator、选定 variants、静默服务器 | — | — | — | SF1/10/30/100；1/4/8/16/32 threads；无 error/timeout |
| S8 | differential safety、normalize、final data | READY-PARTIAL | 5/10/16 h | P70 完整 runner；其余依赖 S3–S7 | — | — | — | false negative=0；formal contract 全字段齐全 |

## 最近检查点

| 时间 | 检查 | 结果 |
|---|---|---|
| 2026-07-21 22:58 | Linux `cidr-experiments/` | `MISSING`，需同步 |
| 2026-07-21 22:58 | Git | branch `codex/sf100-basegraph-bench`，HEAD `3d09ecb...`，工作区已有 CIDR 文稿 dirty 项 |
| 2026-07-21 22:58 | 服务器负载 | 两个外部 Python worker 各约 100% CPU，RSS 总计约 149 GiB；正式 timing 禁止启动 |
| 2026-07-21 22:58 | 容量 | `/data` 可用 968 GiB；MemAvailable 325 GiB |
| 2026-07-21 22:58 | inputs/stores | SF1/10/30/100 inputs、SF30/SF100/C2 base stores、W6 SF10 schema/naive 均存在 |
| 2026-07-21 22:58 | SF1 运行依据 | 旧 W6 dry-run 历时 16m44s；W13 warm-cache 10 tests 约 1s，冷 build 预计 5–30m |

## 下一步队列

- [ ] 同步 `cidr-experiments/` 到 Linux。
- [ ] 比较本地/远端文件数量、相对路径集合和逐文件 SHA-256。
- [ ] 提交同步目录，记录 commit；不覆盖现有 dirty 文稿。
- [ ] 生成/验证统一 run manifest schema。
- [ ] 低优先级运行 W13 10-test correctness，标记 `performance_eligible=false`。
- [ ] 低优先级运行 W6 SF1 9-variant neighbor-compare，标记 `performance_eligible=false`。
- [ ] 实现 dense↔original ID map 与 shared truth consumer。
- [ ] 等服务器释放并通过静默负载 gate 后，开始 S3 正式 timing。

## 每次运行更新模板

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
