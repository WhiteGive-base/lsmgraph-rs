# Table C2 Schema 中文版

每行代表一个 `(scale, policy)` 的 L1→L2 compaction。

核心列：

- `exact_surface_ratio_before/after`：merge input/output 中 topology-Exact edges 占比。
- `pruning_retention`：after / before。
- `mixed_ratio_after`：merge 后 mixed surface 占比。
- `read_bytes_before/after`：controlled workload 下的 read bytes。
- `read_amp_proxy.weighted_read_amp_vs_exact_bytes`：real-SF30 metadata replay 下的 weighted candidate-byte read-amp，不含 body decode。
- `read_amp_proxy.avg_candidate_segments_per_query`：每个 typed-neighbor partition 平均 candidate segment 数。
- `read_amp_proxy.candidate_bytes_total`：40 个 typed partitions 的 candidate bytes 总和。
- `output_segments`：输出 segment 数。
- `logical_update_bytes`：被 merge 覆盖的 logical edge payload。
- `write_amp`：`rewrite_bytes / logical_update_bytes`。
- `correctness_mismatches`：synthetic controlled rows 中必须为 0。

解读重点：headline 是 `exact_surface_ratio` / `pruning_retention`，不是单纯 segment count。
