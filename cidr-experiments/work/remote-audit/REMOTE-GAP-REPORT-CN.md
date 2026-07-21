# SemL0 CIDR 六组图远端证据缺口报告

审计时间：2026-07-21 00:29–00:43（Asia/Shanghai）  
远端仓库：`/data/WorkSpace/lsmgraph-rs`  
审计方式：严格只读；未编译、未运行 benchmark、未改动远端文件。

## 1. 结论先行

当前远端保存了大量可解析 raw，足以恢复历史结果、生成诊断图，并显著减少新实验的探索成本；但按新实验大纲要求，没有一组图能够仅靠旧 raw 直接成为完整的正式证据。主要原因不是“没有数字”，而是正式可比性链条缺项：run commit、硬件、统一 query list、独立 repeats、correctness digest 和进程级资源采集没有同时成立。

本报告将论文图合并为六组：

1. G1：Fig A + Table B，matched typed-neighbor 外部系统比较；
2. G2：Fig B，query-control staircase 消融；
3. G3：Fig C，budget–performance–resource Pareto；
4. G4：Fig D + Fig E，compaction retention 与动态事件时间线；
5. G5：Fig F，property/degree/2-hop/workload-shift coverage；
6. G6：Fig G + Fig H，data-size 与 concurrency scaling。

整体判定：

| 图组 | 可直接解析旧 raw | 只修 parser 能否形成正式图 | 必须重跑 | 当前最高可用级别 |
|---|---|---|---|---|
| G1 外部比较 | 部分 | 否 | 是，且需先统一 truth/ID | 历史参考/正确性 sanity |
| G2 核心消融 | W6/E11 很丰富 | 否 | 是，需增加独立组件开关 | layout ablation，不是 staircase |
| G3 Budget Pareto | W6 SF10/SF100 很强 | 只能形成 import-only provisional 图 | 是，补 query/mixed/compaction 资源 | 可用的 import 权衡证据 |
| G4 Compaction | RQ3/C2/W9 raw 完整 | 可恢复 provisional 图和时间线 | 是，补固定 trace、repeats、资源 | 单次机制证据 |
| G5 Workload coverage | W7/W8/W9/W13 可解析 | 可形成不完整 heatmap | 是，补缺失维度和统一 query gate | coverage sanity |
| G6 Scaling | W6 SF10/SF100 + 旧 E11 | 否 | 是，统一四 scale；并发几乎全重跑 | 两点 diagnostic |

## 2. 当前远端环境与为什么不能立即做正式 timing

审计时状态：

- branch：`codex/sf100-basegraph-bench`
- HEAD：`3d09ecb98b066e77839f9b09995f4b9e0e196e7c`
- HEAD 日期：2026-07-11，`cidr-r4-current-source-baseline`
- dirty 主要位于 CIDR 文稿和未跟踪打包文件，当前 `src/` 未显示修改；正式运行前仍应冻结 clean run commit。
- 当前机器：`finbench`，Intel Xeon 6982P-C，128 logical CPU，1 socket/64 core，2 NUMA nodes，495 GiB RAM，无 swap。
- `/data`：`/dev/nvme1n1p1`，2.0 TiB，总可用约 968 GiB。
- 审计时 load average 约 `2.19/2.22/2.24`，但另一个用户有持续约 34 小时以上的 Python 数据生成任务：主进程约 62% CPU、约 58–61 GiB RSS，并有多个 worker（其中至少约 13 GiB、8.5 GiB RSS）。低 load average 不能排除 page-cache、内存带宽和 I/O 干扰。

历史机器并非当前机器环境：

- LiveGraph `run-config.json` 明确记录旧环境为 Xeon Platinum 8163、64 vCPU、503 GiB、`/dev/vdb1`。
- W13 和 E9 的 `run.meta` 也记录 `/dev/vdb1`；当前已经变为 NVMe 设备。
- 因此旧外部系统、旧 SemL0 与当前新跑数字不能直接拼成同一正式表。

当前共享负载下禁止采集的正式指标包括：latency/QPS/tail、CPU/op、peak/steady RSS/PSS、load/build time、physical I/O、compaction wall time、并发 scaling。可以立即进行的工作见第 10 节。

## 3. 跨 raw 的 provenance 与 correctness 核查

### 3.1 代表性 run 的完整性

| raw | run commit | hardware | query list | repeats | digest/correctness | 判定 |
|---|---|---|---|---|---|---|
| W6 SF10 `remote-logs/w6-sf10-priority-20260709` | 未记录 | 未记录 | 9 types × 1000；sample plan 存在，SHA-256=`61f3bd…24d0c` | `--repeats 3`，同一进程内 | bench `emit_result_digests=false`；另有 neighbor-compare，0 mismatch | raw 可用，正式 provenance/repeats 不足 |
| W6 SF100 `remote-logs/w6-sf100-matrix-20260613-132325` | 未记录 | 未记录 | 9 types × 5000；SHA-256=`810800…fc78` | 同一进程内 3 次 | neighbor-compare 0 mismatch | raw 可用；与 SF10 query 数不同 |
| LiveGraph SF10 | SemL0 repo `c2aa16…` 且 dirty；LiveGraph `eea5a4…` | 完整记录，但为旧 64-vCPU/`vdb1` | 性能为 34 types、32140 ops；digest driver 为独立采样流程 | 1 个正式目录 | digest 32140 checked、0 mismatch，但不是性能 driver 的同一 query stream | correctness 可用，正式 matched timing 不可用 |
| Aster/Neo4j/TuGraph/Nebula SF10 | 外部版本大部分有记录；SemL0 run SHA/逐系统机器 manifest 不完整 | 逐系统 hardware manifest 信息不齐；不能证明与 LiveGraph 或当前环境相同 | 共用 `truth-s50-seed42.tsv`，1700 queries，SHA-256=`876ec4…c788` | 各 1 个 run | 各 1700 checked、0 mismatch | 共享 truth 正确性可用；独立 repeats 不足 |
| W8 SF30 | 未记录 | 未记录 | edge type 1、5000 samples；`sample_plan_in=null`，sampled sources 只嵌在 JSON | 同一进程内 3 次，warmup 1 | 无版本化 digest | raw 可用，需提取/锁定 query list 后重跑 |
| W7 SF30-derived | 未记录 | 未记录 | runner 从 SF30 扫描得到 64 hot sources/phase；没有外置 trace hash | 1 次 | GO gate，不是逐查询 digest | 机制 sanity |
| W9 SF30 | 未记录 | 未记录 | 默认 CSV source pool；实际查询顺序未持久化 | 每 variant 1 次 | 无 digest；compaction 被关闭 | mixed-load sanity，不是 lifecycle 证据 |
| RQ3 formal | 未记录 | 未记录 | source pool 固定，但 `Lcg::new(unix_ms() ^ …)`；三个 arm 顺序执行，实际 query sequence 不同且 seed 未记录 | 每 arm 1 次 | 无逐查询 digest | 重要但必须固定 trace 重跑 |
| C2 SF30 real/proxy | 未记录 | 未记录 | 40 个 metadata partitions | 每策略 1 次 | retention gate；不是 full neighbor digest | retention/write proxy 可用 |
| E9 SF10 | `e3c178…`，dirty | `/dev/vdb1`，其余硬件不完整 | LDBC driver smoke，190 completed ops | 4 threads 仅 1 次 | errors=0，无完整 oracle digest | smoke only |
| W13 | `324a1e…`，dirty | `/dev/vdb1`，未记 CPU | 10 个命名测试 | 各 1 次 | 10/10 assertions pass | correctness 单测证据，不是性能数据 |

### 3.2 repeats 的语义

W6/W8 的 `--repeats 3` 是一个进程、一个 store、一个采样计划内的重复，不等同于大纲要求的独立 process runs。外部 baseline、W7、W9、RQ3、C2 基本都只有 `r1` 或每 arm/variant 单次。正式短查询应做 5 个独立 process run；长时 import/dynamic/compaction 至少 3 个独立 run。

## 4. G1：Matched typed-neighbor 外部系统比较

### 已有可用 raw

- SemL0 W6 SF10：schema、naive、KV-LSM、edge-type-only、semantic、B64/B256/B1024、oracle 的 latency、candidate、read bytes、store size 和 neighbor-compare。
- Aster、Neo4j、TuGraph、NebulaGraph：共享 34 types × 50 sources 的 `truth-s50-seed42.tsv`，1700 条查询，count/hash digest 均为 0 mismatch。
- LiveGraph：SF10 load、RSS、disk、平均延迟和独立 digest raw；版本、binary/source hash 和旧硬件记录较完整。
- 外部 SF10 store 仍在：Aster 约 9.7 GiB、TuGraph 14 GiB、Neo4j 24 GiB、NebulaGraph 44 GiB。LiveGraph aggregate 记录 disk 36 GiB，但 store 目录本身已清空/仅留 marker。

### parser 可修复

- Neo4j SF1 同时存在旧 `FAILED` 和新 `DONE`；parser 应以 result/status manifest 和时间为准，不能仅按 marker 判失败。
- 四个共享 truth 系统可重新统一归一化字段和 correctness gate。
- LiveGraph 可恢复 weighted average，但现有 raw 没有足够的统一 per-query latency 来补全正式全局 P50/P95/P99。

### 必须重跑

- SemL0 必须直接消费与四系统完全相同的 truth。现有 SemL0 用 original encoded ID/FNV EdgeRecord digest，外部 truth 用 dense ID/count+sum+xor；`convert_livegraph_edges.py` 未保存 dense↔original ID map。
- LiveGraph 性能 driver 必须改为消费同一 truth；当前性能 driver 与 digest driver 的采样算法分别是 pool shuffle 与 reservoir，不能视为相同 query list。
- 当前 priority 表将 SemL0 9000 ops、LiveGraph 32140 ops、其他系统 1700 ops 并排，不能作为 matched 主表。
- 所有 timing 必须在同一当前机器、同一 query order、同一 warmup/cache 规则下做独立 repeats。明确来自旧 64-vCPU/`vdb1` 的 LiveGraph 数字，以及硬件记录不完整的其他旧系统数字，都只能保留为历史参考。

结论：G1 的 digest 基础较好，但正式图必须重跑；先做 ID map/truth bridge 是硬依赖。

## 5. G2：Query-control staircase 消融

### 已有可用 raw

- W6 SF10/SF100 是目前质量最高的 layout matrix raw，包含固定 sample plan、3 个 in-process repeats、sentinel、neighbor-compare 和细粒度读放大指标。
- 旧 E11 SF30/SF100 还包含 label-only、degree-only、full-semantic、benefit-scored、full-compact、LSMGraph-style 等更多 layout。

### parser 可修复

- 可从 W6 直接生成“布局/预算变体消融”诊断图。
- 可以重算 candidate/op、read bytes/op、L0 files 和 store overhead；无需跑 benchmark。

### 必须重跑

大纲 A0–A6 要隔离 admission、semantic routing、degree promotion、feedback priority、semantic compaction。当前 layout enum 将多个机制绑在一起，旧 raw 不能用 parser 拆开因果贡献。必须先增加明确 feature/config switches，再在一个冻结 commit 上重跑。

其他限制：

- 旧 E11 与新 W6 的实现行为和内存曲线明显不同，不能拼成同一 staircase。
- W6 bench 没有 result digest，只能引用独立 neighbor-compare gate。
- 当前仅保留 W6 SF10 schema/naive store（各约 14 GiB）；其他 SF10 variant 和 W6 SF100 variant 已由 runner 清理，重跑需重新 import。

结论：旧 raw 可作设计探索和 appendix layout ablation，不能替代正式组件 staircase。

## 6. G3：Budget–performance–resource Pareto

### 已有可用 raw

W6 SF10/SF100 已包含 schema、B64、B256、B1024、full semantic 的：

- latency、candidate、read bytes、L0 files、store bytes；
- import `/usr/bin/time -v` 的 wall/user/sys、CPU%、max RSS、FS inputs/outputs；
- SF100 的粗粒度 `resource-monitor.tsv`，记录每 5 分钟 MemAvailable、磁盘剩余和总 store size。

这些 raw 足以生成一张明确标注为 **import-only / historical hardware** 的 provisional Pareto。SF10 全矩阵旧运行约 50 分钟；SF100 全九变体旧运行约 30 小时 53 分钟。

### parser 可修复

`baseline/render_priority_eval_supplement_20260709.py` 对 elapsed 的正则存在确定性错误：实际 `3:19.49` 被解析成 `19.49`。所有 SF10 import stderr 均保留完整 time-v 文本，修 parser 后无需重跑即可恢复正确秒数。CPU、RSS、FS 字段本身可直接重解析。

### 不能靠 parser 补出的字段

- query/mixed/compaction 阶段的进程 CPU 和 RSS/PSS；
- semantic-index/catalog 的内存占用；
- payload、metadata、catalog、sidecar、manifest、WAL 分项磁盘；
- temporary disk peak、cold open/rebuild、actual process I/O；
- uniform/Zipf/shift 三种 workload 下的同一资源曲线。

`resource-monitor.tsv` 只有全机 MemAvailable/磁盘，不是被测 PID telemetry，不能据此计算 CPU/op 或 steady RSS。

结论：修 parser 可立即得到 import-only 图；完整 Fig C 仍需在当前机器重跑资源采集。

## 7. G4：Compaction retention 与动态时间线

### 已有可用 raw

- RQ3 formal：`feedback/full/none` 三 arm，各 1800 秒；JSONL 含 6 个 checkpoint、QPS/P50/P99、candidate L0、L0 files、compaction count/input/output、writer latency。三个 21 GiB mutated store 仍保留。
- C2 SF30 real：semantic retention 1.0、naive 0.0，write amp 1.24 vs 1.07；`store/c2-sf30-base` 约 116 GiB 可作为重新验证基础。
- C2 SF30 proxy：40 partitions 的 candidate-byte proxy 1.0× vs 6.52×。
- W9：schema/B64/semantic 的 30 分钟 checkpoint 时间线可解析。

### parser 可修复

- RQ3 summarizer 在写 `DONE` 之前运行，因此 `summary.md` 错写 `partial-running`。raw 三 arm 均有 `done` event，目录也有 `DONE`；只需重新运行 summarizer 即可修状态并生成 provisional Fig D/E。
- W9 timeline 可重新绘制，但必须显著标注 compaction disabled。

### 必须重跑

- W9 runner 默认 `COMPACT_EVERY_SECS=0`，start event 也记录 feedback/full compaction 均为 0；rewrite=0 是配置结果，不是低维护成本。
- RQ3 query RNG 使用 wall-clock seed，并未记录 seed；三个 arm 顺序运行，因此不是相同 query sequence。
- RQ3 每 arm 只有一次，无 digest、无 run commit/hardware、无 PID CPU/RSS/temporary disk。
- RQ3 的 `full` 是 blind full compaction，C2 的 naive/semantic 又是另一协议；尚未形成大纲要求的 `capacity-naive / semantic-static / semantic-feedback` 同协议四策略比较。
- C2 real read-amp 是 metadata proxy，没有 full body decode 或 end-to-end latency。

结论：parser 可以恢复一张很有价值的 provisional 时间线；正式 Fig D/E 必须用固定 trace、固定 seed、3 个独立 runs 和进程 telemetry 重跑。

## 8. G5：Workload coverage matrix

### 已有可用 raw

- W8 SF30：schema/B64/semantic，property required/presence/equality/absent-default 和 two-hop；edge type 1，5000 samples，warmup 1，3 个 in-process repeats。
- W7 SF30-derived：feedback-only、static-budgeted、no-feedback 的 A→B hotspot shift。
- W9 SF30：一个 200 query/s + 200 write/s 配置的 mixed workload。
- W13：10 个 schema epoch/alias/drop/encoding/reopen/compaction correctness tests，10/10 PASS。

### parser 可修复

- W8 输出被 runner 的 start/done 文本包裹，但 JSON 主体完整，现有 summarizer 已能剥离。可生成 property/2-hop provisional heatmap。
- W13 顶层 `baseline/w13-schema-evolution-summary-20260614.md` 仍指向旧 `...0004`；当前 raw `...20260615-143649/summary.md` 才是对应本次运行的正确 summary，可修引用而无需重跑。
- 可从 W8 JSON 提取 embedded sampled sources 并生成 query-list hash，但这只能补 provenance，不能创造缺失的独立 repeats/digest。

### 必须重跑或补实现

- W8 `sample_plan_in=null`，没有版本化 query list，也没有 digest。
- W8 只覆盖 edge type 1，没有 rare/medium/frequent、degree low/medium/high 分层。
- read/write 只覆盖一个比例；uniform/burst/hotspot、多个 ratios、snapshot/tombstone/version states 未组成矩阵。
- property equality 仍应单列 prototype，不与 production path 混合宣称。
- W7 是最多扫描 2M rows 的 SF30-derived workload，不是 full-store production trace，且仅单次。

结论：旧 raw 能支持 coverage sanity 和 provisional heatmap，但正式 Fig F 应在 SF10 做完整矩阵、SF30 做代表性 cell 复验。

## 9. G6：Data-size 与 concurrency scaling

### 已有可用 raw

- W6 SF10/SF100 是同 runner 家族，已有 schema/B64/semantic/naive 的两点数据和 `candidate_per_op`。
- 旧 E11 有 SF30/SF100 多 layout，早期 SF1 也有结果，但版本、query protocol 和硬件不能与 W6 拼接。
- E9 唯一成功端到端 smoke：SF10、4 threads、190 completed ops、572.29 QPS、单次。早期 4/8/16 尝试的 summary 只有表头或 driver failure，不能形成 curve。

### parser 可修复

- W6 可形成 SF10→SF100 两点 diagnostic，并按 op 归一化。
- E9 parser 可稳定恢复唯一 4-thread 点；失败目录可登记为 failed attempts，不能当缺失值插值。

### 必须重跑

- SF10 每 type 1000 samples，SF100 每 type 5000，不能把两点直接称为统一 query-count 的完整 scaling curve。
- 正式 Fig G 需要 SF1/SF10/SF30/SF100 同一 query generator、同一 query 数和相同分层比例，至少 4 点。
- 正式 Fig H 需要 1/4/8/16/32、统一 cpuset/CPU quota、固定 operation stream、server+driver 资源和独立 repeats。
- E12 runner 仍引用旧 E11 store tag，并把同一 store 同时传给 SF10/SF30；需修复后才能作为 scaling runner。

存储与成本约束：

- inputs：SF1 0.9 GiB、SF10 9.6 GiB、SF30 30 GiB、SF100 98 GiB；
- base stores：SF30 21 GiB、SF100 66 GiB；
- 一个完整 SF100 layout store 约 134–146 GiB；保留 schema+naive+一个当前 variant 时瞬态约 415 GiB。
- 旧完整 SF100 九变体矩阵实测约 31 小时。当前 968 GiB 空间足够做“顺序运行并归档 raw”的精选变体，但不足以无约束并存多套完整矩阵。

结论：G6 data scaling 和 concurrency 都必须正式重跑；两点 W6 和单点 E9 只能用于校准 runner 与估算成本。

## 10. 当前共享负载下可做与不可做

### 可立即做，不受共享负载影响

- 重解析 W6 `/usr/bin/time -v`，修 elapsed；
- 重跑 RQ3/W8/W9/W13/external summarizer 或 normalizer；
- JSON schema 校验、DONE/result 一致性校验、SHA-256、legacy registry；
- 生成 dense↔original ID map 和共享 truth（应避免全量高 I/O 时段，先做代码/小数据验证）；
- runner `--dry-run`、config expansion 和 manifest 测试；
- 只读生成 provisional tables/figures。

### 科学结论不受负载影响，但当前仍需显式授权后才能启动

- SF1 单元/差分 correctness smoke；
- SF1 query digest smoke；
- 小规模 parser fixture 和 manifest gate。

这些 smoke 只能产出 PASS/FAIL，不得报告性能；应 `nice`/限核，并避开另一个用户的 I/O 峰值。本任务明确“暂不启动 benchmark”，所以本次没有执行。

### 当前绝对不应启动

- 任意正式 latency/QPS/tail run；
- import/load/CPU/RSS/disk 正式采集；
- SF30/SF100 compaction、mixed、scale；
- 外部 DB fresh load；
- concurrency scaling。

## 11. 推荐依赖顺序

1. P0 parser-only：修 W6 elapsed、RQ3 status、W8 wrapper、W13 pointer、Neo4j stale marker。
2. P0 protocol：冻结 manifest schema；生成 ID map、shared truth 和 query-list SHA；修 LiveGraph/SemL0 truth consumer。
3. P0 correctness：仅在获得启动授权后做 SF1 digest gate；不采 timing。
4. P1 G1：共享负载释放后，先做 SF10 matched external。
5. P1 G2/G3：实现组件开关和 PID telemetry，做 SF10 staircase + budget/resource；两组共享 import/store。
6. P1 G4：固定 mixed trace 后做三次 30 分钟 lifecycle；再决定是否补 C2 full-read。
7. P2 G5：SF10 全 coverage、SF30 代表 cell。
8. P2 G6：先 SF1/10/30，最后只对选定 variant 做 SF100；并发必须等 E9/E12 runner 修好。

详细候选、runner/store、时长、磁盘和阻塞项见同目录 `RERUN-CANDIDATES.tsv`。

## 12. 审计边界

- 本报告判断的是 raw 是否足以支撑新六组图，不重新评判算法实现正确性。
- 运行目录进入 Git 的 commit 只代表“artifact 被提交的 commit”，不等于实际运行 binary commit；没有 `run.meta` 时一律记为 unknown。
- 旧 raw 可以保留为历史参考或 appendix，但不得与新硬件正式 run 无标注聚合。
- 本次未修改远端、未清理任何 store、未运行 benchmark。
