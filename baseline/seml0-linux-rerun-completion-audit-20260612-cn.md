# SemL0 Linux 补跑完成性审计（2026-06-12）

> 对照 `baseline/seml0-linux-rerun-plan-20260611-cn.md`，只使用 Linux 端 `/data/WorkSpace/lsmgraph-rs` 的当前事实。

## 总结

- P0 naive@s5000：完成，主稿已同步。
- P1 SF100 correctness compare：完成，采用 CSR sampled compare，4 组均 mismatch=0。
- P2 维护代价补表：完成最低可接受版本，已覆盖 import/store/MANIFEST/L0/RSS；flush/update/concurrent tail 仍是额外强版本缺口。
- P3 reader over-read：完成 caveat 路线，暂不修 precise offset read。
- P4 LiveGraph 外部 baseline：SF10 完成；SF100 scan/convert 完成，但 driver 因 17-21 天级尾部经确认终止，记录为 scalability/time boundary。
- P5 end-to-end LDBC：部分完成；BaseGraph SF10 server-path smoke 通过。schema vs budg-b64 LSM layout end-to-end 仍需 server path 改造，当前按架构边界记录。
- P6 sustained feedback run：完成，30 分钟 trace 已生成并写入主稿。
- P7 full-compact@SF100：按资源/实现边界记录；旧 run max RSS=472,694,124 KB 且 signal 9，当前无安全重跑入口。

## 逐项证据

### P0 naive@s5000

证据：

- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-bench.json`
- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-stats.json`
- `baseline/sf100-results-s5000.tsv`

锁定数字：

- read_bytes=27,318,428,688（26,052.88 MiB / 25.44 GiB）
- candidate_l0_segments=49,257,601
- L0 files=1,703
- avg_us=5382.8
- import_s=3801.7

状态：完成。

### P1 SF100 correctness compare

有效证据：

- `remote-logs/qslsm-sf100-correctness-parallel-20260611-215300/csr-compare-schema-vs-edge-type-only-s100.json`
- `remote-logs/qslsm-sf100-correctness-parallel-20260611-215300/csr-compare-schema-vs-budg-b64-s100.json`
- `remote-logs/qslsm-sf100-correctness-csr-20260611/csr-compare-schema-vs-budg-b256-s100.json`
- `remote-logs/qslsm-sf100-correctness-csr-20260611/csr-compare-schema-vs-semantic-s100.json`

结果：4 组均 checked=900、mismatches=0、left_edges_total=right_edges_total=62,431。

状态：完成。

### P2 维护代价补表

证据：

- `remote-logs/qslsm-sf100-maintenance-table-20260612/maintenance-table.tsv`
- `remote-logs/qslsm-sf100-maintenance-table-20260612/DONE`

覆盖字段：store_bytes、manifest_bytes、l0_files、elapsed_wall、max_rss_kb、exit_status。

状态：完成最低可接受版本。强版本中的 flush time、compaction rewrite bytes、update throughput、concurrent tail/stall proxy 仍需新实验。

### P3 reader over-read

路线：采用 caveat，不修 precise offset read。

事实边界：

- schema/edge-type/budg-b64/budg-b256 的 read_bytes 约 146-150 MiB。
- semantic 和 budg-b1024 的 read_bytes 受 metadata cache 抖动和 offset/body 读粒度影响。
- 主读放大指标改用 candidate_l0_segments、latency、L0 files、store/import。

状态：完成当前写作路线。

### P4 LiveGraph 外部 baseline

SF10 证据：

- `remote-logs/livegraph-sf10-20260612/livegraph-sf10.json`
- `remote-logs/livegraph-sf10-20260612/livegraph-footprint.tsv`
- `remote-logs/livegraph-sf10-20260612/livegraph.stderr`

SF10 结果：

- scan_edges=dense_edges=LiveGraph edge_count=355,185,382
- vertex_count=29,987,835
- load_s=1502.59
- driver wall=27:17.02
- peak_rss_kb=47,908,592
- block=37,580,963,840 bytes，wal=1,073,741,824 bytes

SF100 证据：

- `remote-logs/livegraph-sf100-stream-20260612/scan.json`
- `remote-logs/livegraph-sf100-stream-20260612/convert-summary.json`
- `remote-logs/livegraph-sf100-stream-20260612/livegraph.stderr`

SF100 结果/边界：

- scan directed_edges=3,570,968,680
- convert vertex_count=282,637,871，edge_count=3,570,968,680
- dense edge list=72,750,684,584 bytes
- driver 在 load 阶段出现 17-21 天级尾部，max RSS=320,174,300 KB，wall=5:09:56 后经确认终止
- 未生成 `livegraph-sf100-stream.json`、`livegraph-footprint.tsv` 或 `DONE`

状态：SF10 完成；SF100 记录为 external scalability/time boundary。

### P5 end-to-end LDBC

已完成证据：

- `remote-logs/e9-ldbc-sf10-smoke-20260612-abs/summary/ldbc-end-to-end-summary.tsv`

结果：

- case=sf10-tc4
- driver_status=0
- operation_count=190
- qps=572.29
- queries=136
- updates=74
- errors=0
- slow_requests=0

边界：

- 当前 `snb-server` 使用 BaseGraph CSR + delta 的 `DynamicGraphView`。
- 现有 schema/budg-b64 LSM storage-bench store 与 LDBC server 需要的 `base_graph/catalog.json` store 不是同一 data_dir 结构。
- 因此不能声称完成 schema vs budg-b64 LSM layout end-to-end 对照。

状态：部分完成并记录架构边界。完成原目标需要新增可切换 LSM delta/layout server path，或另行定义 typed-neighbor-heavy subset scope。

### P6 sustained feedback run

证据：

- `remote-logs/p6-sustained-feedback-20260612/sustained.tsv`
- `remote-logs/p6-sustained-feedback-20260612/sustained.json`

结果：

- checkpoint_count=7
- selected_ranges_changed=true
- feedback_after_max_avg_candidate_l0_segments=0.0
- no_feedback_max_avg_candidate_l0_segments=3.0
- compactions=7
- compaction_input_bytes=2056
- compaction_output_bytes=1448
- io_write_bytes=1448

状态：完成。

### P7 full-compact@SF100

证据：

- `baseline/semL0-ablation-results/traces/e11-sf100-full_compact-20260608/time.log`
- `baseline/semL0-ablation-results/traces/e11-sf100-full_compact-20260608/import.stderr`

边界：

- 旧 SF100 full-compact 在 import 后触发 full compact，被 signal 9 终止。
- max RSS=472,694,124 KB。
- 当前 `compact` CLI 仍是普通 L0->L1/partition/range compact；未发现可验证的 `SNB_COMPACT_BUCKETS` stream/bucket full-compact 入口。

状态：按资源/实现边界记录，不重跑。

## 结论

当前补跑计划中，能够安全补跑且有明确现有入口的项目已经完成或形成证据边界。剩余不闭环项不是“还没排队运行”，而是需要新的实现或明确取舍：

- P5 schema vs budg-b64 LDBC end-to-end：需要 server path 支持 LSM delta/layout store，或正式缩窄为 typed-neighbor-heavy subset。
- P7 stream full-compact@SF100：需要可验证的 stream/bucket full-compact 实现入口；否则 472 GB RSS 历史结果已超过当前无 swap 机器安全边界。
- P4 LiveGraph SF100 完成数值：当前 run 已按用户确认终止并记为 external scalability/time boundary。
