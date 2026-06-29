# 00 摘要 中文版

SemL0 的摘要主线是五句：

1. LSM 图存储写入友好，但读路径对 query semantics 无感，会探测本可证明无关的 L0 或跨层 segments。
2. SemL0 把 property-graph query signature 变成 lifecycle control signal，flush 时生成 per-segment semantic pruning surface，read 时 exact-proof pruning。
3. Naive compaction 会把 semantic partitions 混成 mixed segments，侵蚀 surface；semantic-aware compaction 可保留或重建 surface，并报告 rewrite cost。
4. 在 tombstones、degree change、additive schema evolution、snapshot-visible deltas 下，SemL0 conservative-correct，保持 no-false-negative invariant。
5. LDBC SNB up to SF100 显示 candidate/read bytes 降低且 correctness mismatch=0；budgeted/schema variants 的内存与 naive 接近，full semantic 是 unbudgeted stress point；latency 只作为 supporting，重点报告 retention 与 write-amplification cost。
