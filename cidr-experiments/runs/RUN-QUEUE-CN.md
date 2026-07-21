# 七天冻结实验队列

本队列是 `../PROGRESS-CN.md` 第 3 节的可执行映射。硬上限为服务器释放且 clean-window sentinel PASS 后 `168 h`；目标执行 `126 h`，预留 `42 h` 给高方差补跑、runner 修复和缺字段重跑。历史 22/26 项全量队列仅保留在 `../work/remote-audit/`，不得自动触发。

## 复用原则

- 20 个旧证据源：`REUSE=5 / FIX=6 / RERUN=9`。复用 bounded correctness、C2 机制证据、W6 layout facts、可修 raw、truth 和通过兼容性 gate 的 immutable stores。
- 不重复旧 SF100 九点探索矩阵；正式 P32 固定 `naive/schema/B64/B256/B1024/semantic` 六点。
- 旧 performance 图不能整张升级为正式图；只补 matched timing、资源 telemetry、独立 repeats 和缺失 correctness/provenance。
- 所有正式点先执行 3 个独立进程 run；仅当 QPS CV >3% 或 P99 CV >5% 时补到 5。import/dynamic/compaction 固定至少 3 个独立 run。

## 执行顺序

| 顺序 | 任务 | 大纲映射 | 预算 | 放行与完成条件 |
|---:|---|---|---:|---|
| 0 | `P00/P01/P02A/P03`：同步、hash、commit、共享 truth/ID bridge、collector/feature switches、SF1 correctness、setup manifest | E00/E09 前置 | 12 h | 当前负载可执行；correctness 结果标 `performance_eligible=false` |
| 1 | `P02B`：sentinel + cross-system shared-truth gate | E00/E01 前置 | 2 h | clean window；query count/order/hash 与 digest 全部一致 |
| 2 | `P10/P11`：SemL0 四配置 + 五外部系统，SF10 1700-query matched comparison | E01 | 12 h | 同 truth/order/cache/API boundary；n=3 起跑；0 mismatch |
| 3 | `P20/P21/P31`：SF10 A0--A6 三类 workload、SF30 A0/A2/A4/A6 代表复验；资源 collector | E03 + E04/SF10 | 14 h | 单变量开关；CPU/PSS/disk/I/O/digest 齐全 |
| 4 | `P32`：SF100 六个 budget 点 | E04/SF100 | 40 h | uniform 六点；核心四点补 Zipf/shift mixed/compaction；import×3、query n=3 起跑 |
| 5 | `P40/P41/P42`：proxy/full-read 校准、real SF30 C2、四 arms fixed-trace dynamic | E05 | 14 h | 四 arms×30 min×3；同 base/trace；full execution 与 proxy 分栏 |
| 6 | `P51/P52/P53`：预注册分层代表 workload/property/two-hop/RW/shift cells | E06 | 8 h | 每个维度至少一项正式证据，不跑完整笛卡尔积 |
| 7 | `P60/P61/P62`：四规模×四 variants；1/4/8/16/32 concurrency | E07/E08 | 10 h | 复用已验证 stores；统一 query count、cpuset/quota 和 stream |
| 8 | `P70`：3 seeds×至少 20,000 ops differential safety | E09 | 5 h | false negative=0；失败保留并 shrink trace |
| 9 | normalize、统计、CI、缺字段与 claim-to-evidence 审计 | 全部 | 9 h | raw/manifest/hash/digest/telemetry/normalized 全部齐全 |

## 统一停止规则

1. 任一 `digest_pass=false`、`mismatch_count>0`、truth/order/hash 不一致，立即停止该配置 timing。
2. 当前共享重负载下只执行同步、parser/hash/manifest、工程和 correctness-only；不得生成正式 latency/QPS/CPU/RSS/I/O 结论。
3. 正式性能阶段不得并行运行两个任务，也不得边 import 边测另一个系统 latency。
4. P32 运行中 `/data` 可用空间触及 180 GiB、sentinel 失稳或外部 I/O 干扰时，保留当前 attempt 并安全停止。
5. 每个 repeat/variant/arm 完成即保存 raw、telemetry、manifest 和 SHA-256；缺任一项不得标 `PASS`。
6. 42 h 缓冲使用顺序：correctness 修复 > 缺字段补跑 > 高方差补到 5 runs > 语义等价 gate 已通过时的可选 E02；不得用来扩大 workload/variant 网格。

每项的实时状态、实际 wall time 和剩余 ETA 只写入 `../PROGRESS-CN.md`；底层 runner/store 历史与审计上界见 `../work/remote-audit/RERUN-CANDIDATES.tsv`。
