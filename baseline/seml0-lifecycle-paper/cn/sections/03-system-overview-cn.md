# 03 系统概览 中文版

SemL0 是四个 loop：

1. **Write / flush**：写出 CSR-like L0 segments，并生成 semantic metadata。
2. **Read / prune**：`GraphAccessSignature` 与 segment metadata 比较，能证明 disjoint 就剪枝，否则读。
3. **Feedback / compaction**：根据 query counts、candidate fanout、read/rewrite proxies 选择 hot partitions，运行 semantic-aware merge。
4. **Schema / snapshot safety**：按 per-segment epoch 解释 metadata，不确定就 conservative read。

核心数据结构：`SegmentSemanticState`，由 topology / degree / property / tombstone / schema 五个正交维度组成。

