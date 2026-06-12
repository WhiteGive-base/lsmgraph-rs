# SemL0 Linux 补跑状态记录（2026-06-12）

> 只记录 Linux 端 `/data/WorkSpace/lsmgraph-rs` 的当前事实；本文件用于衔接补跑，不替代原计划 `baseline/seml0-linux-rerun-plan-20260611-cn.md`。

## 资源约束

- 机器内存：约 503 GiB，swap=0。
- 单个 SF100 LSM import 已观测峰值：约 206-209 GiB RSS。
- SF100 full-compact 旧失败峰值：472,694,124 KB RSS，signal 9。
- 当前并发策略：同一时间只允许 1 个 SF100 级 import/compact；可并发 SF10、P6 synthetic runner、只读审计和脚本整理。

## P0 naive@s5000

状态：完成。

证据：

- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-bench.json`
- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-stats.json`
- `baseline/sf100-results-s5000.tsv`
- `baseline/sf100-strong-baseline-traces/summary-sf100.tsv`

锁定数字：

- naive@s5000 read_bytes=27,318,428,688（26052.88 MiB / 25.44 GiB）
- candidate_l0_segments=49,257,601
- L0 files=1,703
- avg_us=5382.8
- import_s=3801.7

主文同步：

- `baseline/seml0-linux-main-paper-draft-cn-20260611.md` 已补充 naive@s5000 的事实源、RQ2 表格行和 10.1 证据锁定数字。

## P1 SF100 correctness compare

状态：完成，采用 CSR sampled compare。不要使用 `SNB_SKIP_SEM_INDEX=1 neighbor-compare` 的旧结果作为证据；该路径曾返回空邻居。

有效证据：

- `remote-logs/qslsm-sf100-correctness-parallel-20260611-215300/csr-compare-schema-vs-edge-type-only-s100.json`
- `remote-logs/qslsm-sf100-correctness-parallel-20260611-215300/csr-compare-schema-vs-budg-b64-s100.json`
- `remote-logs/qslsm-sf100-correctness-csr-20260611/csr-compare-schema-vs-budg-b256-s100.json`
- `remote-logs/qslsm-sf100-correctness-csr-20260611/csr-compare-schema-vs-semantic-s100.json`

四组结果均为 checked=900、mismatches=0、left_edges_total=right_edges_total=62431。

## P2 维护代价补表

状态：完成。

runner：

- `baseline/run_sf100_maintenance_table_20260612.sh`
- 输出：`remote-logs/qslsm-sf100-maintenance-table-20260612/maintenance-table.tsv`
- 临时 store：`store/qslsm-sf100-maintenance-table-20260612`

当前策略：串行跑 `schema edge-type-only semantic budg-b64 budg-b256 budg-b1024`，逐个记录 import/store/MANIFEST/L0/max RSS 后删除临时变体 store。

当前进度：

- schema 已完成：store_bytes=141,242,590,040，manifest_bytes=2,364,030，l0_files=3,444，elapsed_wall=1:20:15，max_rss_kb=217,254,732。
- edge-type-only 已完成：store_bytes=141,242,628,041，manifest_bytes=2,368,319，l0_files=3,446，elapsed_wall=1:18:41，max_rss_kb=217,319,028。
- semantic 已完成：store_bytes=141,245,314,584，manifest_bytes=4,518,158，l0_files=6,615，elapsed_wall=1:19:08，max_rss_kb=216,672,136。
- budg-b64 已完成：store_bytes=141,242,986,434，manifest_bytes=2,393,463，l0_files=3,483，elapsed_wall=1:18:05，max_rss_kb=216,854,320。
- budg-b256 已完成：store_bytes=141,243,140,540，manifest_bytes=2,524,145，l0_files=3,675，elapsed_wall=1:17:38，max_rss_kb=216,335,536。
- budg-b1024 已完成：store_bytes=141,243,805,064，manifest_bytes=3,052,991，l0_files=4,451，elapsed_wall=1:21:27，max_rss_kb=215,418,948。

校验：

- `maintenance-table.tsv` 共 6 行，variant 顺序为 schema/edge-type-only/semantic/budg-b64/budg-b256/budg-b1024。
- 所有 exit_status=0，store_bytes/manifest_bytes/l0_files/max_rss_kb 均为正。
- 临时 store 已清理到 4K，`remote-logs/qslsm-sf100-maintenance-table-20260612/DONE` 已生成。

## P3 reader over-read

状态：走 caveat 路线，暂不修 precise offset read。

事实：

- SF100 schema read_bytes=156,981,696，candidate_l0_segments=5,929,197。
- SF100 semantic read_bytes=10,479,735,312，candidate_l0_segments=7,782,877。
- SF100 budg-b1024 read_bytes=734,072,656，candidate_l0_segments=7,676,875。
- schema/edge-type/budg-b64/budg-b256 的 read_bytes 约 146-150 MiB，candidate L0 约 5.9-6.5M。

解释边界：

- 主读放大指标使用 candidate_l0_segments、latency、L0 files、store/import。
- `read_bytes` 在含 degree 维度的大规模变体上受 metadata cache 抖动和 offset/body 读粒度影响；SF100 degree-heavy 变体需单独标注 caveat。
- 后续如果要降低审稿风险，再做 precise offset read 或稀疏 offset index，并重跑 semantic/budg-b1024。

## P4 LiveGraph 外部 baseline

状态：SF10 pipeline 已完成；SF100 stream pipeline 的 scan/convert 已完成，但 LiveGraph driver 因多天级尾部风险由用户确认终止，按 scalability/time boundary 记录。

已完成准备：

- `scan --dump-edges <path>` 已加入并通过 release build。
- `baseline/convert_livegraph_edges.py` 已部署。
- `baseline/run_livegraph_sf10_pipeline_20260612.sh` 已部署。
- LiveGraph driver 需要 `LD_LIBRARY_PATH=/data/WorkSpace/lsmgraph-rs/deps/LiveGraph/build`。
- `baseline/external-drivers/livegraph_driver_stream` 已新增并通过 tiny smoke；它流式读取 dense edge list，避免旧 driver 把全部边和 per-edge source pool 常驻内存。

SF10 输出：

- `remote-logs/livegraph-sf10-20260612/`
- `remote-logs/livegraph-sf10-20260612/livegraph-sf10.json`
- `remote-logs/livegraph-sf10-20260612/livegraph-footprint.tsv`
- `remote-logs/livegraph-sf10-20260612/livegraph.stderr`

SF10 校验结果：

- scan_edges=355,185,382，dense_edges=355,185,382，LiveGraph edge_count=355,185,382。
- vertex_count=29,987,835。
- load_s=1502.59，driver wall=27:17.02。
- peak_rss_kb=47,908,592。
- LiveGraph footprint：block=37,580,963,840 bytes，wal=1,073,741,824 bytes。

约束：SF100 LiveGraph driver 运行期间不再叠加其他 SF100 级 import/compact/外部 baseline 任务；当前资源余量主要留给 LiveGraph block mmap、page cache 和写回。

SF100 资源复评（P2 完成后，2026-06-12 10:52）：

- 当前 MemAvailable=449 GiB，`/data` free=354 GiB。
- SF10 实测：raw=14 GiB，dense=6.2 GiB，LiveGraph block=35 GiB，wal=1 GiB，driver peak_rss_kb=47,908,592。
- 直接按 10x 边量外推，SF100 raw+dense+block/wal 需要约 550 GiB 级磁盘峰值；即使删除 P4 SF10 中间目录（约 53 GiB），`/data` 也只有约 407 GiB 空闲，不足以安全启动。
- stream driver 可降低 driver 的常驻边表内存，但不能消除 LiveGraph block/wal 磁盘峰值；正式 SF100 需要先释放更多磁盘，或进一步改造成无 dense/raw 中间文件的流式流程。
- `baseline/run_livegraph_sf100_stream_pipeline_20260612.sh` 已准备并通过 `bash -n`；默认资源门限 `MIN_FREE_GIB_START=500`。
- dry-run `livegraph-sf100-stream-20260612-dryrun` 在 start resource gate 按预期拒绝：disk_free=354GiB < required 500GiB，未启动 scan。

SF100 正式运行（用户清理磁盘后，2026-06-12）：

- run_id：`livegraph-sf100-stream-20260612`。
- 启动资源门限通过：start resource check 记录 MemAvailable=449 GiB，`/data` free=692 GiB。
- `scan --dump-edges` 已完成：`scan.json` 记录 snapshot=3,570,968,680，directed_edges=3,570,968,680。
- `convert_livegraph_edges.py` 已完成：dense edge list `edges-dense.txt` 为 72,750,684,584 bytes；`convert-summary.json` 记录 vertex_count=282,637,871，edge_count=3,570,968,680；raw edge list 已在转换后删除。
- LiveGraph driver 启动前资源检查：MemAvailable=448 GiB，`/data` free=624 GiB。
- 2026-06-12 19:42 观测：driver 已运行 03:57:29，RSS/VmHWM=320,137,040 KB，`MemAvailable` 约 430 GiB，`/data` free=337 GiB；`livegraph-block` 约 293 GiB，`livegraph-wal` 1.0 GiB，输出目录约 356 GiB。
- 同次观测：dense 输入 fd 偏移为 59,045,077,421 / 72,750,684,584 bytes，仍在 load 阶段；`livegraph-sf100-stream.json`、`livegraph-footprint.tsv`、`DONE` 尚未生成。
- 19:31 到 19:42 连续采样显示输入偏移只前进约 7.6 MB，但 write_bytes 增加约 0.89 GB，CPU 仍约 105%；判断为 LiveGraph SF100 batch-load 后段插入/写回退化，不是 OOM 或磁盘不足。若继续等待，需要按长跑/过夜任务处理。
- 2026-06-12 20:08 观测：driver 已运行 04:23:18，RSS/VmHWM=320,151,956 KB，`MemAvailable` 约 430 GiB，`/data` free=337 GiB；dense 输入 fd 偏移为 59,059,641,019 / 72,750,684,584 bytes，`livegraph-block` 精确大小为 314,606,354,432 bytes。
- 2026-06-12 20:22 短窗口采样：driver 已运行 04:37:57，RSS/VmHWM=320,159,188 KB，dense 输入 fd 偏移为 59,066,693,470 / 72,750,684,584 bytes；20:16-20:22 期间 fd 偏移前进约 2.7 MB，write_bytes 增加约 342 MB，进程仍为 `R` 且 CPU 约 105%。
- 耗时估算：LiveGraph `BlockManager` 按 1GB 粒度 `ftruncate` + mmap 扩展，`livegraph-block` 文件 size/mtime 只能作为写回活动信号，不能精确代表 edge-load 进度。按近期 write_bytes 口径，剩余 driver 时间可能仍是 10-16 小时；按 fd 输入口径，若尾部持续当前极慢速度，则存在多天级尾部风险。当前策略是保留运行并继续监控完成/失败信号，不并发启动其他 SF100 级任务。
- 2026-06-12 20:41-20:44 复评：dense fd 偏移约 59.08 GB / 72.75 GB，剩余输入约 13.67 GB；最近多个窗口 fd 推进速度稳定在约 0.46-0.56 MB/min。按 fd 口径线性外推，剩余时间约 17-21 天。该估算比 block/write_bytes 口径更可信，因为 fd 偏移直接反映 driver 还没有读入的 dense edge list。
- 当时建议止损边界：若后续短窗口采样没有明显恢复到 GB/min 或至少数十 MB/min 量级，应把 SF100 LiveGraph 记录为 external baseline 的 scalability/time boundary，而不是无条件等待数周；终止或清理 `remote-logs/livegraph-sf100-stream-20260612/` 需要用户确认。
- 2026-06-12 20:51 复查：driver 已运行 05:06:17，RSS/VmHWM=320,172,728 KB，dense fd 偏移为 59,079,708,969 / 72,750,684,584 bytes；20:44-20:51 期间仍只有约 2.9 MB 前进，未见速度恢复。
- 2026-06-12 20:54：用户确认终止 PID 3275257。先发送 SIGTERM，8 秒后进程仍存在，再发送 SIGKILL；20:55 复查无 `livegraph_driver_stream` / wrapper 残留进程。
- `livegraph.stderr` 记录：Command terminated by signal 15，driver wall=5:09:56，max RSS=320,174,300 KB，file system outputs=640,927,128 blocks。未生成 `livegraph-sf100-stream.json`、`livegraph-footprint.tsv` 或 `DONE`。
- 保留现场文件未清理：`edges-dense.txt` 约 68 GiB，`livegraph-block` 约 293 GiB，`livegraph-wal` 1.0 GiB，输出目录约 356 GiB。若要释放磁盘，需要单独确认清理策略。
- 结论：SF100 LiveGraph 不能作为完成数值进入主表；可报告为“同一边集已完成 scan/convert，LiveGraph SF100 batch-load 在 320 GB RSS 下出现 17-21 天级尾部，按 external scalability/time boundary 处理”。SF10 LiveGraph 数值仍可作为通过 reproducibility gate 的外部 baseline。
- 2026-06-12 21:13 清理完成（用户批准）：已删除 `edges-dense.txt`、`livegraph-block`、`livegraph-wal`，`/data` 从 337 GiB 回升至 692 GiB 可用；scan/convert/launcher/stderr 等证据文件保留。终止存档与论文可用结论见 `baseline/livegraph-sf100-attempt-20260612-cn.md`。SF100 级任务阻塞解除。

## P5 end-to-end LDBC

状态：部分完成；BaseGraph SF10 server-path smoke 已通过，但 schema vs budg-b64 layout 对照未完成。

当前发现：

- `remote-logs/e9-ldbc-end-to-end-20260606-*` 下的 summary TSV 均只有表头，没有 case rows。
- 空表根因之一：`scripts/experiments/active/run-e9-ldbc-end-to-end-driver.sh` 旧版把相对 `OUT_DIR` 传给 high-thread 脚本；后者切到 Java driver 目录后，driver/progress 日志路径失效。2026-06-12 已修为进入 driver 前规范成绝对路径。
- `snb-server` 当前打开 `DynamicGraphView`，读路径是 BaseGraph CSR + delta；代码注释显示 legacy LSM fallback 已移除。
- 现有 LSM storage-bench store（schema/budg-b64）和 LDBC server 需要的 `base_graph/catalog.json` store 不是同一种 data_dir。

已补 smoke：

- `remote-logs/e9-ldbc-sf10-smoke-20260612-abs/summary/ldbc-end-to-end-summary.tsv`
- case=sf10-tc4，driver_status=0，operation_count=190，qps=572.29，queries=136，updates=74，errors=0，slow_requests=0。

边界：

- 不能用当前 LDBC server 直接声称 schema vs budg-b64 的 LSM layout end-to-end 对照。
- SF10 BaseGraph LDBC smoke 可作为 server-path 证据，但这不是 P5 的 schema/budg-b64 完成证据。
- 若要完成 P5 原目标，需要给 server path 增加可切换的 LSM delta/layout store，或改成明确的 typed-neighbor-heavy subset 并在文档中说明 scope。

## P6 sustained feedback run

状态：完成。

新增 runner：

- `src/bin/p3_feedback_sustained.rs`
- bin：`target/release/p3-feedback-sustained`

smoke 证据：

- `remote-logs/p6-sustained-feedback-smoke-20260612/sustained.tsv`
- `remote-logs/p6-sustained-feedback-smoke-20260612/sustained.json`

正式输出：

- `remote-logs/p6-sustained-feedback-20260612/sustained.tsv`
- `remote-logs/p6-sustained-feedback-20260612/sustained.json`

设计：

- 30 分钟，5 分钟 checkpoint。
- A/B/C 三个热点阶段。
- feedback 与 no-feedback 并行 synthetic store。
- 每个 checkpoint 记录 candidate L0、latency、L0 files、selected range、compaction input/output bytes、IO write bytes、write blocking proxy。

正式结果：

- checkpoint_count=7（0/5/10/15/20/25/30 分钟）。
- selected_ranges_changed=true，A/B/C 三个热点阶段均被选中。
- feedback_after_max_avg_candidate_l0_segments=0.0。
- no_feedback_max_avg_candidate_l0_segments=3.0。
- compactions=7，compaction_input_bytes=2056，compaction_output_bytes=1448，io_write_bytes=1448。

## P7 full-compact@SF100

状态：当前按边界记录，不立即重跑。

证据：

- `baseline/semL0-ablation-results/traces/e11-sf100-full_compact-20260608/time.log`
- `baseline/semL0-ablation-results/traces/e11-sf100-full_compact-20260608/import.stderr`

旧结果：

- import 完成后触发 FullCompact post-import L0->L1 compaction。
- 进程被 signal 9 终止。
- max RSS=472,694,124 KB。
- 当前代码中未找到 `SNB_COMPACT_BUCKETS` 的实际实现入口，`compact` CLI 仍是普通 L0->L1/partition/range compact。

结论边界：

- SF100 full-compact 缺失是极端写优化 baseline 的实现/资源边界，不是 SemL0 layout 失败。
- P4 LiveGraph SF100 driver 已终止并释放 RSS，但 SF100 full-compact 旧证据显示 max RSS=472,694,124 KB 且 signal 9；机器无 swap，当前仍不安全。除非先实现可验证的 stream/bucket full-compact 入口并单独做资源门限，否则不启动 SF100 full-compact 重跑。

## W2 C10 profile / engine freeze prep

状态：完成，等待 commit/tag freeze 后由 W6 单独会话发射。

新增证据：

- `baseline/w2-c10-profile-20260613-cn.md`
- `remote-logs/w2-c10-20260613/sf30-stage-summary.tsv`
- `remote-logs/w6-sf1-dryrun-20260613/DONE`

关键结果：

- CSR probe 插桩进入 bench JSON：setup / bloom / offset / body / total，以及 bloom-negative / offset-miss / body-hit。
- SF30 schema import 19:04.36，MaxRSS 2,314,300 KB；budg-b64 import 19:11.32，MaxRSS 2,500,980 KB；final flush 1.6/1.7s。
- SF30 fixed overhead 未低于 50us/probe：schema 671.06us，budg-b64 690.35us；主要开销在 offset lookup。
- 4KB offset-window 优化尝试未达保留线且增加 read_bytes，已回滚。
- W6 runner `baseline/run_w6_sf100_matrix_20260613.sh` SF1 dry-run 全绿：9 变体、3 repeats、A/B/A sentinel、8 个 compare 均 checked=180/mismatches=0；oracle bench 写出 `oracle_index_stats.entry_count=11330230`。

W6 放行边界：

- 本状态不代表已启动 W6/SF100；runner 默认拒绝 SF100，必须显式 `W6_ALLOW_SF100=1`。
- W6 应单独会话启动，继续遵守单 SF100 任务原则、资源轮询和 abort 条件。
